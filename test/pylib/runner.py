#
# Copyright (C) 2025-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import platform
import random
import shutil
import sys
import threading
import time
from argparse import BooleanOptionalAction
from collections import defaultdict
from itertools import chain, count, product
from functools import cache, cached_property
from pathlib import Path
from random import randint
from typing import TYPE_CHECKING, Callable

import pytest
import xdist
import universalasync
import yaml
from _pytest.junitxml import xml_key


from test import ALL_MODES, DEBUG_MODES, TEST_RUNNER, TOP_SRC_DIR, HOST_ID
from test.pylib.skip_reason_plugin import skip_marker
from test.pylib.scylla_cluster import get_scylla_executable, merge_cmdline_options
from test.pylib.suite import (
    Test,
    TestSuite,
)
from test.pylib.artifact_registry import ArtifactRegistry
from test.pylib.host_registry import HostRegistry
from test.pylib.ldap_server import start_ldap
from test.pylib.minio_server import MinioServer
from test.pylib.resource_gather import setup_cgroup
from test.pylib.s3_proxy import S3ProxyServer
from test.pylib.s3_server_mock import MockS3Server
from test.pylib.util import LogPrefixAdapter, get_modes_to_run, scale_timeout_by_mode

from datetime import datetime

import psutil

from test.pylib.db.model import SystemResourceMetric
from test.pylib.db.writer import DEFAULT_DB_NAME, SYSTEM_RESOURCE_METRICS_TABLE, SQLiteWriter

if TYPE_CHECKING:
    from collections.abc import Generator

    import _pytest.nodes
    import _pytest.scope

    from pytest import Parser


TEST_CONFIG_FILENAME = "test_config.yaml"
PYTEST_LOG_FOLDER = "pytest_log"
PYTEST_TESTS_LOGS_FOLDER = "pytest_tests_logs"

REPEATING_FILES = pytest.StashKey[set[pathlib.Path]]()
BUILD_MODE = pytest.StashKey[str]()
RUN_ID = pytest.StashKey[int]()
PYTEST_LOG_FILE = pytest.StashKey[str]()

EXIT_MAXFAIL_REACHED = 11

logger = logging.getLogger(__name__)

# Store pytest config globally so we can access it in hooks that only receive report
_pytest_config: pytest.Config | None = None

# Resource watcher state (thread-based replacement for test.py's asyncio watcher)
_resource_watcher_stop: threading.Event | None = None
_resource_watcher_thread: threading.Thread | None = None


def _resource_monitor_loop(stop_event: threading.Event, tmpdir: pathlib.Path) -> None:
    """Poll system CPU and memory every 2 seconds, writing to SQLite.

    Runs in a daemon thread started by _start_resource_watcher().
    """
    from test import HOST_ID as _host_id

    sqlite_writer = SQLiteWriter(tmpdir / DEFAULT_DB_NAME)
    while not stop_event.wait(timeout=2.0):
        record = SystemResourceMetric(
            host_id=_host_id,
            cpu=psutil.cpu_percent(interval=0.1),
            memory=psutil.virtual_memory().percent,
            timestamp=datetime.now(),
        )
        sqlite_writer.write_row(record, SYSTEM_RESOURCE_METRICS_TABLE)


def _start_resource_watcher(tmpdir: pathlib.Path) -> None:
    """Start the background resource monitoring thread."""
    global _resource_watcher_stop, _resource_watcher_thread
    _resource_watcher_stop = threading.Event()
    _resource_watcher_thread = threading.Thread(
        target=_resource_monitor_loop,
        args=(_resource_watcher_stop, tmpdir),
        daemon=True,
        name="resource-watcher",
    )
    _resource_watcher_thread.start()


def _stop_resource_watcher() -> None:
    """Stop the background resource monitoring thread."""
    global _resource_watcher_stop, _resource_watcher_thread
    if _resource_watcher_stop is not None:
        _resource_watcher_stop.set()
    if _resource_watcher_thread is not None:
        _resource_watcher_thread.join(timeout=5.0)
    _resource_watcher_stop = None
    _resource_watcher_thread = None


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption('--mode', choices=ALL_MODES, action="append", dest="modes",
                     help="Run only tests for given build mode(s)")
    parser.addoption('--tmpdir', action='store', default=str(TOP_SRC_DIR / 'testlog'),
                     help='Path to temporary test data and log files.  The data is further segregated per build mode.')
    parser.addoption('--run_id', action='store', default=None, help='Run id for the test run')
    parser.addoption('--byte-limit', action="store", default=randint(0, 2000), type=int,
                     help="Specific byte limit for failure injection (random by default)")
    parser.addoption("--gather-metrics", action=BooleanOptionalAction, default=False,
                     help='Switch on gathering cgroup metrics')
    parser.addoption('--random-seed', action="store",
                     help="Random number generator seed to be used by boost tests")

    # Options for compatibility with test.py
    parser.addoption('--save-log-on-success', default=False,
                     dest="save_log_on_success", action="store_true",
                     help="Save test log output on success and skip cleanup before the run.")
    parser.addoption('--coverage', action='store_true', default=False,
                     help="When running code instrumented with coverage support"
                          "Will route the profiles to `tmpdir`/mode/coverage/`suite` and post process them in order to generate "
                          "lcov file per suite, lcov file per mode, and an lcov file for the entire run, "
                          "The lcov files can eventually be used for generating coverage reports")
    parser.addoption("--coverage-mode", action='append', type=str, dest="coverage_modes",
                     help="Collect and process coverage only for the modes specified. implies: --coverage, default: All built modes")
    parser.addoption("--cluster-pool-size", type=int,
                     help="Set the cluster pool size for test suites.  Alternatively environment variable "
                          "CLUSTER_POOL_SIZE can be used to achieve the same")
    parser.addoption("--extra-scylla-cmdline-options", default='',
                     help="Passing extra scylla cmdline options for all tests.  Options should be space separated:"
                          " '--logger-log-level raft=trace --default-log-level error'")
    parser.addoption('--x-log2-compaction-groups', action="store", default="0", type=int,
                     help="Controls number of compaction groups to be used by Scylla tests. Value of 3 implies 8 groups.")
    parser.addoption('--repeat', action="store", default="1", type=int,
                     help="number of times to repeat test execution")

    parser.addoption('--exe-path', default=False,
                     dest="exe_path", action="store",
                     help="Path to the executable to run. Not working with `mode`")
    parser.addoption('--exe-url', default=False,
                     dest="exe_url", action="store",
                     help="URL to download the relocatable executable. Not working with `mode`")

def testpy_test_fixture_scope(fixture_name: str, config: pytest.Config) -> _pytest.scope._ScopeName:
    """Dynamic scope for fixtures which rely on a current test.py suite/test.

    Returns "module" for the pytest runner (both test.py and bare pytest), where each
    module needs its own Test instance tied to its test_config.yaml and build mode.
    Returns "session" for run.py scripts, which start a single Scylla instance for
    the entire test session, so fixtures like host and cql should be session-scoped.
    """
    if TEST_RUNNER == "runpy":
        return "session"
    return "module"

testpy_test_fixture_scope.__test__ = False


@pytest.fixture(scope=testpy_test_fixture_scope, autouse=True)
def build_mode(request: pytest.FixtureRequest) -> str:
    params_stash = get_params_stash(node=request.node)
    if params_stash is None:
        return request.config.build_modes[0]
    return params_stash[BUILD_MODE]


@pytest.fixture(scope=testpy_test_fixture_scope)
def scale_timeout(build_mode: str) -> Callable[[int | float], int | float]:
    def scale_timeout_inner(timeout: int | float) -> int | float:
        return scale_timeout_by_mode(build_mode, timeout)

    return scale_timeout_inner


@pytest.fixture(scope=testpy_test_fixture_scope)
async def testpy_test(request: pytest.FixtureRequest, build_mode: str) -> Test | None:
    """Create an instance of Test class for the current test module."""

    if request.scope == "module":
        suite_config = request.node.stash[TEST_SUITE]
        options = request.config.option
        suite = TestSuite.opt_create(suite_config=suite_config, options=options, mode=build_mode)
        if getattr(options, "exe_path", False):
            suite.scylla_exe = options.exe_path
        elif getattr(options, "exe_url", False):
            suite.scylla_exe = await get_scylla_executable(options.exe_url)
        shortname = str(request.path.relative_to(suite.suite_path).with_suffix(""))
        return Test(shortname, suite)
    return None

@pytest.fixture(scope="function")
def scylla_binary(testpy_test) -> Path:
    return testpy_test.suite.scylla_exe


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        modify_pytest_item(item=item)

    suites_order = defaultdict(count().__next__)  # number suites in order of appearance

    def sort_key(item: pytest.Item) -> tuple[int, bool]:
        suite = item.stash[TEST_SUITE]
        return suites_order[suite], suite and item.path.stem not in suite.cfg.get("run_first", [])

    items.sort(key=sort_key)



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


@universalasync.async_to_sync_wraps
async def prepare_environment(tempdir_base: pathlib.Path, modes: list[str], gather_metrics: bool, save_log_on_success: bool,
                        toxiproxy_byte_limit: int) -> None:
    prepare_dirs(tempdir_base, modes, gather_metrics, save_log_on_success=save_log_on_success)
    await start_3rd_party_services(tempdir_base=tempdir_base, toxiproxy_byte_limit=toxiproxy_byte_limit)

def pytest_sessionstart(session: pytest.Session) -> None:
    # Skip initialization when only collecting tests, or when running under run.py scripts.
    if TEST_RUNNER != "pytest" or session.config.getoption("--collect-only"):
        return

    # Check if this is an xdist worker
    is_xdist_worker = xdist.is_xdist_worker(request_or_session=session)

    # Initialize globals — always needed (xdist workers run in separate processes)
    init_testsuite_globals()
    TestSuite.artifacts.add_exit_artifact(None, TestSuite.hosts.cleanup)

    # Prepare environment just once in the main pytest process (not in xdist workers)
    if not is_xdist_worker:
        temp_dir = pathlib.Path(session.config.getoption("--tmpdir")).absolute()

        prepare_environment(
            tempdir_base=temp_dir,
            modes=get_modes_to_run(session.config),
            gather_metrics=session.config.getoption("--gather-metrics"),
            save_log_on_success=session.config.getoption("--save-log-on-success"),
            toxiproxy_byte_limit=session.config.getoption("--byte-limit"),
        )

        if session.config.getoption("--gather-metrics"):
            _start_resource_watcher(temp_dir)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    """Add custom XML attributes to JUnit testcase elements.

    This hook wraps the node_reporter's to_xml method to add custom attributes
    when the XML element is created. This approach works with pytest-xdist because
    it modifies the XML element directly when it's generated, rather than trying
    to modify attrs before finalize() is called.

    Attributes added:
    - function_path: The function path of the test case (excluding parameters).

    Uses tryfirst=True to run before LogXML's hook has created the node_reporter to avoid double recording.
    """
    # Get the XML reporter
    config = _pytest_config
    if config is None:
        return

    xml = config.stash.get(xml_key, None)
    if xml is None:
        return

    node_reporter = xml.node_reporter(report)

    # Only wrap once to avoid multiple wrapping (check on the node_reporter object itself)
    if not getattr(node_reporter, '__reporter_modified', False):

        function_path = f'test/{report.nodeid.rsplit('.', 2)[0].rsplit('[', 1)[0]}'

        # Wrap the to_xml method to add custom attributes to the element
        original_to_xml = node_reporter.to_xml

        def custom_to_xml():
            """Wrapper that adds custom attributes to the testcase element."""
            element = original_to_xml()
            element.set("function_path", function_path)
            return element

        node_reporter.to_xml = custom_to_xml
        node_reporter.__reporter_modified = True


def pytest_sessionfinish(session: pytest.Session) -> None:
    is_xdist_worker = xdist.is_xdist_worker(request_or_session=session)
    # If all tests passed, remove the log file to save space and avoid confusion with logs from failed runs.
    # We check this at the end of the session to ensure that we have the complete log available for any failed tests.

    if session.testsfailed == 0 and not session.config.getoption("--save-log-on-success"):
        os.remove(_pytest_config.stash[PYTEST_LOG_FILE])

    # xdist workers should not clean up — only the main process should
    if is_xdist_worker:
        return
    _stop_resource_watcher()
    # we only clean up when running with pure pytest
    if getattr(TestSuite, "artifacts", None) is not None:
        asyncio.run(TestSuite.artifacts.cleanup_before_exit())

    # Modify exit code to reflect the number of failed tests for easier detection in CI.
    maxfail = session.config.getoption("maxfail")

    if 0 < maxfail <= session.testsfailed:
        session.exitstatus = EXIT_MAXFAIL_REACHED


def pytest_configure(config: pytest.Config) -> None:
    global _pytest_config
    _pytest_config = config

    pytest_log_dir = pathlib.Path(_pytest_config.getoption("--tmpdir")).absolute() / PYTEST_LOG_FOLDER
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    # If this is an xdist worker, set up logging to a separate file for this worker. Otherwise, set up logging for the main process.
    if worker_id is not None:
        _pytest_config.stash[PYTEST_LOG_FILE] = f"{pytest_log_dir}/pytest_{worker_id}_{HOST_ID}.log"
        logging.basicConfig(
            format=config.getini("log_file_format"),
            filename=_pytest_config.stash[PYTEST_LOG_FILE],
            level=config.getini("log_file_level"),
        )
    else:
        # For the main process, we want to clean up old logs before the run, so we create the log directory and remove any existing log files.
        pytest_log_dir.mkdir(parents=True, exist_ok=True)
        if not _pytest_config.getoption("--save-log-on-success"):
            for file in pytest_log_dir.glob("*"):
                file.unlink()

        _pytest_config.stash[PYTEST_LOG_FILE] = f"{pytest_log_dir}/pytest_main_{HOST_ID}.log"
        logging.basicConfig(
            format=config.getini("log_file_format"),
            filename=_pytest_config.stash[PYTEST_LOG_FILE],
            level=config.getini("log_file_level"),
        )

    if config.getoption("--exe-url") and config.getoption("--exe-path"):
        raise RuntimeError("Can't use --exe-url and exe-path simultaneously.")

    if  config.getoption("--exe-path") or config.getoption("--exe-url"):
            if config.getoption("--mode"):
                raise RuntimeError("Can't use --mode with --exe-path or --exe-url.")
            config.option.modes = ["custom_exe"]

    os.environ["TOPOLOGY_RANDOM_FAILURES_TEST_SHUFFLE_SEED"] = os.environ.get("TOPOLOGY_RANDOM_FAILURES_TEST_SHUFFLE_SEED", str(random.randint(0, sys.maxsize)))
    config.build_modes = get_modes_to_run(config)
    repeat = int(config.getoption("--repeat"))

    if testpy_run_id := config.getoption("--run_id"):
        if repeat != 1:
            raise RuntimeError("Can't use --run_id and --repeat simultaneously.")
        config.run_ids = (testpy_run_id,)
    else:
        config.run_ids = tuple(range(1, repeat + 1))


@pytest.hookimpl(wrapper=True)
def pytest_collect_file(file_path: pathlib.Path,
                        parent: pytest.Collector) -> Generator[None, list[pytest.Collector], list[pytest.Collector]]:
    collectors = yield

    if len(collectors) == 1 and file_path not in parent.stash.setdefault(REPEATING_FILES, set()):
        parent.stash[REPEATING_FILES].add(file_path)

        build_modes = parent.config.build_modes
        if suite_config := TestSuiteConfig.from_pytest_node(node=collectors[0]):
            build_modes = (
                mode for mode in build_modes
                if not suite_config.is_test_disabled(build_mode=mode, path=file_path)
            )
        repeats = list(product(build_modes, parent.config.run_ids))

        if not repeats:
            return []

        ihook = parent.ihook
        collectors = list(chain(collectors, chain.from_iterable(
            ihook.pytest_collect_file(file_path=file_path, parent=parent) for _ in range(1, len(repeats))
        )))
        for (build_mode, run_id), collector in zip(repeats, collectors, strict=True):
            collector.stash[BUILD_MODE] = build_mode
            collector.stash[RUN_ID] = run_id
            collector.stash[TEST_SUITE] = suite_config

        parent.stash[REPEATING_FILES].remove(file_path)

    return collectors

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # This hook is used to capture test failures and save their details to a file in the pytest_tests_logs directory.
    # We use tryfirst=True to ensure that this hook runs before any other hooks that might modify the report,
    # and we use hookwrapper=True to allow us to access the report after it has been generated by other hooks.
    outcome = yield

    rep = outcome.get_result()
    # we only look at actual failing test calls, not setup/teardown
    pytest_tests_logs = pathlib.Path(_pytest_config.getoption("--tmpdir")).absolute() / PYTEST_TESTS_LOGS_FOLDER
    if rep.failed or _pytest_config.getoption("--save-log-on-success"):
        mode = "a" if os.path.exists(pytest_tests_logs) else "w"
        with open(pytest_tests_logs/ f"{item._nodeid.replace("::", "-").replace("/", "-")}-{rep.when}-{HOST_ID}.log",mode) as f:
            f.write(rep.longreprtext + "\n")
            for section in rep.sections:
                f.write(section[0] + "\n")
                f.write(section[1] + "\n")

class TestSuiteConfig:
    def __init__(self, config_file: pathlib.Path):
        self.path = config_file.parent
        self.cfg = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    @cached_property
    def name(self) -> str:
        return self.path.name

    @cached_property
    def _run_in_specific_mode(self) -> set[str]:
        return set(chain.from_iterable(self.cfg.get(f"run_in_{build_mode}", []) for build_mode in ALL_MODES))

    @cache
    def disabled_tests(self, build_mode: str) -> set[str]:
        result = set(self.cfg.get("disable", []))
        result.update(self.cfg.get(f"skip_in_{build_mode}", []))
        if build_mode in DEBUG_MODES:
            result.update(self.cfg.get("skip_in_debug_modes", []))
        run_in_this_mode = set(self.cfg.get(f"run_in_{build_mode}", []))
        result.update(self._run_in_specific_mode - run_in_this_mode)
        return result

    def is_test_disabled(self, build_mode: str, path: pathlib.Path) -> bool:
        return str(path.relative_to(self.path).with_suffix("")) in self.disabled_tests(build_mode=build_mode)

    @classmethod
    def from_pytest_node(cls, node: _pytest.nodes.Node) -> TestSuiteConfig | None:
        config_file = node.path / TEST_CONFIG_FILENAME
        if config_file.is_file():
            suite = cls(config_file=config_file)
            extra_opts = node.config.getoption("--extra-scylla-cmdline-options")
            if extra_opts:
                extra_cmd = suite.cfg.get('extra_scylla_cmdline_options', [])
                extra_cmd = merge_cmdline_options(extra_cmd, extra_opts.split())
                suite.cfg['extra_scylla_cmdline_options'] = extra_cmd
        else:
            if node.parent is None:
                return None
            suite = node.parent.stash.get(TEST_SUITE, None)
            if suite is None:
                suite = cls.from_pytest_node(node=node.parent)
        if suite:
            node.stash[TEST_SUITE] = suite
        return suite


TEST_SUITE = pytest.StashKey[TestSuiteConfig | None]()

_STASH_KEYS_TO_COPY = BUILD_MODE, RUN_ID, TEST_SUITE


def get_params_stash(node: _pytest.nodes.Node) -> pytest.Stash | None:
    parent = node.getparent(cls=pytest.File)
    if parent is None:
        return None
    return parent.stash


def modify_pytest_item(item: pytest.Item) -> None:
    params_stash = get_params_stash(node=item)

    for key in _STASH_KEYS_TO_COPY:
        item.stash[key] = params_stash[key]

    suffix = f".{item.stash[BUILD_MODE]}.{item.stash[RUN_ID]}"

    item._nodeid = f"{item._nodeid}{suffix}"
    item.name = f"{item.name}{suffix}"
    skip_marks = [
        mark for mark in item.iter_markers("skip_mode")
        if mark.name == "skip_mode"
    ]

    for mark in skip_marks:
        def __skip_test(mode, reason, platform_key=None):
            modes = [mode] if isinstance(mode, str) else mode

            for mode in modes:
                if mode == item.stash[BUILD_MODE]:
                    if platform_key is None or platform_key in platform.platform():
                        skip_marker(item, reason, skip_type="mode")
        try:
            __skip_test(*mark.args, **mark.kwargs)
        except TypeError as e:
            raise TypeError(f"Failed to process skip_mode mark, {mark} for test {item}, error {e}")

    if (any(mark.name == "xfail" for mark in item.iter_markers("xfail"))
            and not any(mark.name == "nightly" for mark in item.iter_markers("nightly"))):
        item.add_marker(pytest.mark.nightly)

    if (any(mark.name in ("perf", "manual", "unstable") for mark in item.iter_markers())
            and not any(mark.name == "non_gating" for mark in item.iter_markers("non_gating"))):
        item.add_marker(pytest.mark.non_gating)


# Use cache to execute this function once per pytest session.
@cache
def add_host_option(parser: Parser) -> None:
    parser.addoption("--host", default="localhost",
                     help="a DB server host to connect to")


# Use cache to execute this function once per pytest session.
@cache
def add_cql_connection_options(parser: Parser) -> None:
    """Add pytest options for a CQL connection."""

    cql_options = parser.getgroup("CQL connection options")
    cql_options.addoption("--port", default="9042",
                          help="CQL port to connect to")
    cql_options.addoption("--ssl", action="store_true",
                          help="Connect to CQL via an encrypted TLSv1.2 connection", default=False)
    cql_options.addoption("--auth_username",
                          help="username for authentication", default=None)
    cql_options.addoption("--auth_password",
                          help="password for authentication", default=None)


# Use cache to execute this function once per pytest session.
@cache
def add_s3_options(parser: Parser) -> None:
    """Options for tests which use S3 server (i.e., cluster/object_store and cqlpy/test_tools.py)"""

    s3_options = parser.getgroup("S3 server settings")
    s3_options.addoption('--s3-server-address', default=None)
    s3_options.addoption('--s3-server-port', type=int, default=None)
    s3_options.addoption('--aws-access-key', default=None)
    s3_options.addoption('--aws-secret-key', default=None)
    s3_options.addoption('--aws-region', default=None)
    s3_options.addoption('--s3-server-bucket', default=None)
