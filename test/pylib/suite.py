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
import sys
from contextlib import asynccontextmanager
from functools import cached_property
from typing import TYPE_CHECKING

import colorama
from test import path_to
from test.pylib.pool import Pool
from test.pylib.scylla_cluster import ScyllaCluster
from test.pylib.util import LogPrefixAdapter, get_xdist_worker_id

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from typing import Any, Callable

    from test.pylib.artifact_registry import ArtifactRegistry
    from test.pylib.host_registry import HostRegistry
    from test.pylib.runner import TestSuiteConfig



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
        self.dirties_cluster = set(cfg.get("dirties_cluster", []))


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
    def opt_create(suite_config: TestSuiteConfig, options: argparse.Namespace, mode: str) -> 'TestSuite':
        """Create a TestSuite for the given TestSuiteConfig.
        Ensures there is only one suite instance per path."""
        path = str(suite_config.path)
        suite_key = os.path.join(path, mode)
        suite = TestSuite.suites.get(suite_key)
        if not suite:
            suite = TestSuite(path, suite_config.cfg, options, mode)
            assert suite is not None
            TestSuite.suites[suite_key] = suite
        return suite

    @cached_property
    def clusters(self) -> Pool:
        env_pool_size = os.getenv("CLUSTER_POOL_SIZE")
        if self.options.cluster_pool_size is not None:
            pool_size = self.options.cluster_pool_size
        elif env_pool_size is not None:
            pool_size = int(env_pool_size)
        else:
            pool_size = self.cfg.get("pool_size", 2)
        return Pool(pool_size, self.create_cluster, lambda cluster: cluster.recycle())

    async def create_cluster(self, logger: logging.Logger | logging.LoggerAdapter) -> ScyllaCluster:
        cluster = ScyllaCluster(
            logger=logger,
            vardir=self.log_dir,
            host_registry=self.hosts,
            replicas=self.cfg.get("cluster", {"initial_size": 1})["initial_size"],
            mode=self.mode,
            cmdline_options=self.cfg.get("extra_scylla_cmdline_options", []),
            cmdline_options_override=self.options.extra_scylla_cmdline_options,
            config_options=self.cfg.get("extra_scylla_config_options", {}),
            append_env=self.base_env,
            scylla_exe=self.scylla_exe,
        )

        # Suite artifacts are removed when the entire suite ends successfully.
        self.artifacts.add_suite_artifact(self, cluster.stop)
        if not self.options.save_log_on_success:
            # If a test fails, we might want to keep the data dirs.
            self.artifacts.add_suite_artifact(self, cluster.uninstall)
        self.artifacts.add_exit_artifact(self, cluster.stop)

        await cluster.install_and_start()
        # If cluster failed to start, raise the exception immediately
        # so the pool doesn't return a broken cluster to tests
        if cluster.start_exception is not None:
            # Clean up the broken cluster before raising
            try:
                await cluster.stop()
                if cluster.api is not None:
                    cluster.api.close()
                    cluster.api = None
                await cluster.release_ips()
            except:
                pass  # Ignore cleanup errors
            raise cluster.start_exception
        return cluster


    def need_coverage(self):
        return self.options.coverage and (self.mode in self.options.coverage_modes) and bool(self.cfg.get("coverage",True))


class Test:
    """Run a pytest collection of cases against a standalone Scylla"""
    def __init__(self, shortname: str, suite) -> None:
        self.id = suite.next_id((shortname, suite.suite_key))
        self.args: List[str] = []
        # Arguments which are required by a program regardless of additional test specific arguments
        self.core_args : List[str] = []
        self.valid_exit_codes = [0]
        # Name within the suite
        self.shortname = shortname
        self.suite = suite
        # Unique file name, which is also readable by human, as filename prefix
        self.uname = f"{self.suite.name}.{self.shortname.replace('/', '_')}.{self.id}"
        if xdist_worker_id := get_xdist_worker_id():
            self.uname = f"{xdist_worker_id}.{self.uname}"
        self.success = False

    @asynccontextmanager
    async def run_ctx(self) -> AsyncGenerator[ScyllaCluster]:
        """A test's setup/teardown context manager.

        Leases a ScyllaDB cluster from the pool and yields it.
        The cluster is returned to the pool after the test finishes.
        If the test fails, the cluster is marked as dirty.
        """
        loggerPrefix = self.suite.mode + '/' + self.uname
        logger = LogPrefixAdapter(logging.getLogger(loggerPrefix), {'prefix': loggerPrefix})
        name = os.path.join(self.suite.name, self.shortname.split('.')[0])
        server_log_filename = None
        cluster = None
        is_before_test_ok = False
        is_after_test_ok = False
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
            server_log_filename = cluster.server_log_filename()
            is_before_test_ok = True
            cluster.take_log_savepoint()

            yield cluster

            self.success = True
            if self.shortname in self.suite.dirties_cluster:
                cluster.is_dirty = True
            cluster.after_test(self.uname, self.success)
            is_after_test_ok = True
        except Exception as e:
            if not is_before_test_ok:
                print(f"Test {name} pre-check failed: {str(e)}\ncheck server logs: {server_log_filename}")
                logger.info(f"Discarding cluster after failed start for test %s...", name)
            elif not is_after_test_ok:
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



