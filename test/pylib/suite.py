#
# Copyright (C) 2025-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

from __future__ import annotations

import argparse
import collections
import logging
import os
import pathlib
import shutil
import sys
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import colorama
import universalasync
import yaml

from test import TEST_DIR, TEST_RUNNER, path_to
from test.pylib.artifact_registry import ArtifactRegistry
from test.pylib.host_registry import HostRegistry
from test.pylib.ldap_server import start_ldap
from test.pylib.minio_server import MinioServer
from test.pylib.pool import Pool
from test.pylib.resource_gather import setup_cgroup
from test.pylib.s3_proxy import S3ProxyServer
from test.pylib.s3_server_mock import MockS3Server
from test.pylib.scylla_cluster import ScyllaCluster, ScyllaServer, merge_cmdline_options, get_current_version_description, get_scylla_executable
from test.pylib.util import LogPrefixAdapter, get_xdist_worker_id

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from typing import Any, Union


TEST_CONFIG_FILENAME = "test_config.yaml"
PYTEST_TESTS_LOGS_FOLDER = "pytest_tests_logs"

output_is_a_tty = sys.stdout.isatty()


def create_formatter(*decorators) -> Callable[[Any], str]:
    """Return a function which decorates its argument with the given
    color/style if stdout is a tty, and leaves intact otherwise."""
    def color(arg: Any) -> str:
        return "".join(decorators) + str(arg) + colorama.Style.RESET_ALL

    def nocolor(arg: Any) -> str:
        return str(arg)
    return color if output_is_a_tty else nocolor


class palette:
    """Color palette for formatting terminal output"""
    fail = create_formatter(colorama.Fore.RED, colorama.Style.BRIGHT)
    diff_in = create_formatter(colorama.Fore.GREEN)
    diff_out = create_formatter(colorama.Fore.RED)
    diff_mark = create_formatter(colorama.Fore.MAGENTA)


class TestSuite:
    """A test suite is a folder with tests of the same type.
    E.g. it can be unit tests, boost tests, or CQL tests."""

    # All existing test suites, one suite per path/mode.

    suites: dict[str, TestSuite] = {}

    artifacts: ArtifactRegistry
    hosts: HostRegistry

    _next_id = collections.defaultdict(int) # (test_key -> id)

    def __init__(self, path: str, cfg: dict, options: argparse.Namespace, mode: str) -> None:
        self.suite_path = pathlib.Path(path)
        self.log_dir = pathlib.Path(options.tmpdir) / mode
        self.name = str(self.suite_path.name)
        self.cfg = cfg
        self.options = options
        self.mode = mode
        self.suite_key = os.path.join(path, mode)
        # environment variables that should be the base of all processes running in this suit
        self.base_env = {}
        if self.need_coverage():
            # Set the coverage data from each instrumented object to use the same file (and merged into it with locking)
            # as long as we don't need test specific coverage data, this looks sufficient. The benefit of doing this in
            # this way is that the storage will not be bloated with coverage files (each can weigh 10s of MBs so for several
            # thousands of tests it can easily reach 10 of GBs)
            # ref: https://clang.llvm.org/docs/SourceBasedCodeCoverage.html#running-the-instrumented-program
            self.base_env["LLVM_PROFILE_FILE"] = str(self.log_dir / "coverage" / self.name / "%m.profraw")

        self.scylla_exe = path_to(self.mode, "scylla")

        cluster_cfg = self.cfg.get("cluster", {"initial_size": 1})
        cluster_size = cluster_cfg["initial_size"]
        env_pool_size = os.getenv("CLUSTER_POOL_SIZE")
        if options.cluster_pool_size is not None:
            pool_size = options.cluster_pool_size
        elif env_pool_size is not None:
            pool_size = int(env_pool_size)
        else:
            pool_size = cfg.get("pool_size", 2)
        self.dirties_cluster = set(cfg.get("dirties_cluster", []))

        self.create_cluster = self.get_cluster_factory(cluster_size, options)
        async def recycle_cluster(cluster: ScyllaCluster) -> None:
            """When a dirty cluster is returned to the cluster pool,
               stop it and release the used IPs. We don't necessarily uninstall() it yet,
               which would delete the log file and directory - we might want to preserve
               these if it came from a failed test.
            """
            for srv in cluster.servers.values():
                if srv.log_file is not None:
                    srv.log_file.close()
                srv.maintenance_socket_dir.cleanup()
            await cluster.stop()
            # Close API client to release connector resources
            if cluster.api is not None:
                cluster.api.close()
                cluster.api = None
            await cluster.release_ips()

        self.clusters = Pool(pool_size, self.create_cluster, recycle_cluster)


    # Generate a unique ID for `--repeat`ed tests
    # We want these tests to have different XML IDs so test result
    # processors (Jenkins) don't merge results for different iterations of
    # the same test. We also don't want the ids to be too random, because then
    # there is no correlation between test identifiers across multiple
    # runs of test.py, and so it's hard to understand failure trends. The
    # compromise is for next_id() results to be unique only within a particular
    # test case. That is, we'll have a.1, a.2, a.3, b.1, b.2, b.3 rather than
    # a.1 a.2 a.3 b.4 b.5 b.6.
    def next_id(self, test_key) -> int:
        if getattr(self.options, "run_id", None):
            TestSuite._next_id[test_key] = self.options.run_id
        else:
            TestSuite._next_id[test_key] += 1
        return TestSuite._next_id[test_key]


    @staticmethod
    def load_cfg(path: pathlib.Path) -> dict:
        with path.open(encoding='utf-8') as cfg_file:
            cfg = yaml.safe_load(cfg_file.read())
            if not isinstance(cfg, dict):
                raise RuntimeError(f"Failed to load tests config file {path}")
            return cfg

    @staticmethod
    def opt_create(config: pathlib.Path, options: argparse.Namespace, mode: str) -> 'TestSuite':
        """Create a TestSuite for the given config file.
        Ensures there is only one suite instance per path."""
        path = str(config.parent)
        suite_key = os.path.join(path, mode)
        suite = TestSuite.suites.get(suite_key)
        if not suite:
            cfg = TestSuite.load_cfg(config)
            suite = TestSuite(path, cfg, options, mode)
            assert suite is not None
            TestSuite.suites[suite_key] = suite
        return suite

    def get_cluster_factory(self, cluster_size: int, options: argparse.Namespace) -> Callable[..., Awaitable]:
        def create_server(create_cfg: ScyllaCluster.CreateServerParams):
            cmdline_options = self.cfg.get("extra_scylla_cmdline_options", [])
            if type(cmdline_options) == str:
                cmdline_options = [cmdline_options]
            cmdline_options = merge_cmdline_options(cmdline_options, create_cfg.cmdline_from_test)
            cmdline_options = merge_cmdline_options(cmdline_options, options.extra_scylla_cmdline_options.split())
            # There are multiple sources of config options, with increasing priority
            # (if two sources provide the same config option, the higher priority one wins):
            # 1. the defaults
            # 2. suite-specific config options (in "extra_scylla_config_options")
            # 3. config options from tests (when servers are added during a test)
            default_config_options = \
                {"authenticator": "PasswordAuthenticator",
                 "authorizer": "CassandraAuthorizer"}
            default_config_options["tablets_initial_scale_factor"] = 4 if self.mode == "release" else 2
            config_options = default_config_options | \
                             self.cfg.get("extra_scylla_config_options", {}) | \
                             create_cfg.config_from_test

            server = ScyllaServer(
                mode=self.mode,
                version=(create_cfg.version or get_current_version_description(self.scylla_exe)),
                vardir=self.log_dir,
                logger=create_cfg.logger,
                cluster_name=create_cfg.cluster_name,
                ip_addr=create_cfg.ip_addr,
                seeds=create_cfg.seeds,
                cmdline_options=cmdline_options,
                config_options=config_options,
                property_file=create_cfg.property_file,
                append_env=self.base_env,
                server_encryption=create_cfg.server_encryption)

            return server

        async def create_cluster(logger: Union[logging.Logger, logging.LoggerAdapter]) -> ScyllaCluster:
            cluster = ScyllaCluster(logger, self.hosts, cluster_size, create_server)

            async def stop() -> None:
                await cluster.stop()

            # Suite artifacts are removed when
            # the entire suite ends successfully.
            self.artifacts.add_suite_artifact(self, stop)
            if not self.options.save_log_on_success:
                # If a test fails, we might want to keep the data dirs.
                async def uninstall() -> None:
                    await cluster.uninstall()

                self.artifacts.add_suite_artifact(self, uninstall)
            self.artifacts.add_exit_artifact(self, stop)

            await cluster.install_and_start()
            # If cluster failed to start, raise the exception immediately
            # so the pool doesn't return a broken cluster to tests
            if cluster.start_exception is not None:
                # Clean up the broken cluster before raising
                try:
                    await cluster.stop()
                    if cluster.api is not None:
                        await cluster.api.close()
                        cluster.api = None
                    await cluster.release_ips()
                except:
                    pass  # Ignore cleanup errors
                raise cluster.start_exception
            return cluster

        return create_cluster


    def need_coverage(self):
        return self.options.coverage and (self.mode in self.options.coverage_modes) and bool(self.cfg.get("coverage",True))


class Test:
    """Run a pytest collection of cases against a standalone Scylla"""
    def __init__(self, test_no: int, shortname: str, suite) -> None:
        self.id = test_no
        self.args: List[str] = []
        # Arguments which are required by a program regardless of additional test specific arguments
        self.core_args : List[str] = []
        self.valid_exit_codes = [0]
        # Name within the suite
        self.shortname = shortname
        self.mode = suite.mode
        self.suite = suite
        # Unique file name, which is also readable by human, as filename prefix
        self.uname = f"{self.suite.name}.{self.shortname.replace('/', '_')}.{self.id}"
        if xdist_worker_id := get_xdist_worker_id():
            self.uname = f"{xdist_worker_id}.{self.uname}"
        self.success = False
        self.time_start: float = 0
        self.time_end: float = 0
        self.server_address: str | None = None
        self.is_before_test_ok = False
        self.is_after_test_ok = False

    @asynccontextmanager
    async def run_ctx(self) -> AsyncGenerator[None]:
        """A test's setup/teardown context manager.

        Leases a ScyllaDB cluster from the pool and sets server_address.
        The cluster is returned to the pool after the test finishes.
        If the test fails, the cluster is marked as dirty.
        """
        loggerPrefix = self.mode + '/' + self.uname
        logger = LogPrefixAdapter(logging.getLogger(loggerPrefix), {'prefix': loggerPrefix})
        name = os.path.join(self.suite.name, self.shortname.split('.')[0])
        server_log_filename = None
        cluster = None
        try:
            cluster = await self.suite.clusters.get(logger)
            cluster.before_test(self.uname)
            prepare_cql = self.suite.cfg.get("prepare_cql", None)
            if prepare_cql and not hasattr(cluster, 'prepare_cql_executed'):
                cc = next(iter(cluster.running.values())).control_connection
                if not isinstance(prepare_cql, collections.abc.Iterable):
                    prepare_cql = [prepare_cql]
                for stmt in prepare_cql:
                    cc.execute(stmt)
                cluster.prepare_cql_executed = True
            logger.info("Leasing Scylla cluster %s for test %s", cluster, self.uname)
            self.server_address = cluster.endpoint()
            server_log_filename = cluster.server_log_filename()
            self.is_before_test_ok = True
            cluster.take_log_savepoint()

            yield cluster

            if self.shortname in self.suite.dirties_cluster:
                cluster.is_dirty = True
            cluster.after_test(self.uname, self.success)
            self.is_after_test_ok = True
        except Exception as e:
            if not self.is_before_test_ok:
                print(f"Test {name} pre-check failed: {str(e)}\ncheck server logs: {server_log_filename}")
                logger.info(f"Discarding cluster after failed start for test %s...", name)
            elif not self.is_after_test_ok:
                print(f"Test {name} post-check failed: {str(e)}\ncheck server logs: {server_log_filename}")
                logger.info(f"Discarding cluster after failed test %s...", name)
            self.success = False
            if cluster is not None:
                cluster.is_dirty = True
            raise
        finally:
            if cluster is not None:
                await self.suite.clusters.put(cluster, is_dirty=cluster.is_dirty)
                logger.info("Test %s %s", self.uname, "succeeded" if self.success else "failed ")


def init_testsuite_globals() -> None:
    """Create global objects required for a test run."""

    TestSuite.artifacts = ArtifactRegistry()
    TestSuite.hosts = HostRegistry()


def prepare_dir(dirname: pathlib.Path, pattern: str, save_log_on_success: bool) -> None:
    # Ensure the dir exists.
    dirname.mkdir(parents=True, exist_ok=True)

    if not save_log_on_success:
        # Remove old artifacts.
        if pattern == '*':
            shutil.rmtree(dirname, ignore_errors=True)
        else:
            for p in dirname.glob(pattern):
                p.unlink()

@universalasync.async_to_sync_wraps
async def prepare_environment(tempdir_base: pathlib.Path, modes: list[str], gather_metrics: bool, save_log_on_success: bool,
                        toxiproxy_byte_limit: int) -> None:
    prepare_dirs(tempdir_base, modes, gather_metrics, save_log_on_success=save_log_on_success)
    await start_3rd_party_services(tempdir_base=tempdir_base, toxiproxy_byte_limit=toxiproxy_byte_limit)

def prepare_dirs(tempdir_base: pathlib.Path, modes: list[str], gather_metrics: bool, save_log_on_success: bool = False) -> None:
    setup_cgroup(gather_metrics)
    prepare_dir(tempdir_base, "*.log", save_log_on_success)
    prepare_dir(tempdir_base/ PYTEST_TESTS_LOGS_FOLDER, "*.log", save_log_on_success)
    for directory in ['report', 'ldap_instances']:
        full_path_directory = tempdir_base / directory
        prepare_dir(full_path_directory, '*', save_log_on_success)
    for mode in modes:
        prepare_dir(tempdir_base / mode, "*.log", save_log_on_success)
        prepare_dir(tempdir_base / mode, "*.reject", save_log_on_success)
        prepare_dir(tempdir_base / mode / "xml", "*.xml", save_log_on_success)
        prepare_dir(tempdir_base / mode / "failed_test", "*", save_log_on_success)
        prepare_dir(tempdir_base / mode / "allure", "*.xml", save_log_on_success)
        if TEST_RUNNER != "pytest":
            prepare_dir(tempdir_base / mode / "pytest", "*", save_log_on_success)


@universalasync.async_to_sync_wraps
async def start_3rd_party_services(tempdir_base: pathlib.Path, toxiproxy_byte_limit: int):
    hosts = HostRegistry()

    finalize = start_ldap(
        host=await hosts.lease_host(),
        port=5000,
        instance_root=tempdir_base / 'ldap_instances',
        toxiproxy_byte_limit=toxiproxy_byte_limit)
    async def make_async_finalize():
        finalize()

    TestSuite.artifacts.add_exit_artifact(None, make_async_finalize)
    ms = MinioServer(
        tempdir_base=str(tempdir_base),
        address=await hosts.lease_host(),
        logger=LogPrefixAdapter(logger=logging.getLogger("minio"), extra={"prefix": "minio"}),
    )
    await ms.start()
    TestSuite.artifacts.add_exit_artifact(None, ms.stop)

    TestSuite.artifacts.add_exit_artifact(None, hosts.cleanup)

    mock_s3_server = MockS3Server(
        host=await hosts.lease_host(),
        port=2012,
        logger=LogPrefixAdapter(logger=logging.getLogger("s3_mock"), extra={"prefix": "s3_mock"}),
    )
    await mock_s3_server.start()
    TestSuite.artifacts.add_exit_artifact(None, mock_s3_server.stop)

    minio_uri = f"http://{os.environ[ms.ENV_ADDRESS]}:{os.environ[ms.ENV_PORT]}"
    proxy_s3_server = S3ProxyServer(
        host=await hosts.lease_host(),
        port=9002,
        minio_uri=minio_uri,
        max_retries=3,
        seed=int(time.time()),
        logger=LogPrefixAdapter(logger=logging.getLogger("s3_proxy"), extra={"prefix": "s3_proxy"}),
    )
    await proxy_s3_server.start()
    TestSuite.artifacts.add_exit_artifact(None, proxy_s3_server.stop)

def find_suite_config(path: pathlib.Path, config_filename: str) -> pathlib.Path:
    for directory in (path.joinpath("_") if path.is_dir() else path).absolute().relative_to(TEST_DIR).parents:
        suite_config = TEST_DIR / directory / config_filename
        if suite_config.exists():
            return suite_config
    raise FileNotFoundError(f"Unable to find a suite config file ({config_filename}) related to {path}")


async def get_testpy_test(path: pathlib.Path, options: argparse.Namespace, mode: str) -> Test:
    """Create an instance of Test class for the path provided."""
    suite_config = find_suite_config(path=path, config_filename=TEST_CONFIG_FILENAME)
    suite = TestSuite.opt_create(config=suite_config, options=options, mode=mode)
    if getattr(options, "exe_path", False):
        suite.scylla_exe = options.exe_path
    elif getattr(options, "exe_url", False):
        suite.scylla_exe = await get_scylla_executable(options.exe_url)
    shortname = str(path.relative_to(suite.suite_path).with_suffix(""))
    test_no = suite.next_id((shortname, suite.suite_key))
    return Test(test_no, shortname, suite)
