#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

"""Unit tests for test.py — Phase 3 characterization tests.

These tests capture the current behavior of test.py functions that Phase 3
will modify or remove.  They pass against the pre-Phase-3 code and serve
as a safety net for the migration.

Functions tested:
- ThreadsCalculator.__init__ / get_number_of_threads
- setup_signal_handlers
- run_pytest (argument assembly and XML parsing)
- print_summary
- open_log
"""

import argparse
import asyncio
import importlib.util
import math
import pathlib
import signal
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from xml.etree.ElementTree import Element

import pytest


# ---------------------------------------------------------------------------
# Import test.py via importlib (it cannot be imported as "test" because that
# name is taken by the test/ package).
# ---------------------------------------------------------------------------

_test_py_path = str(pathlib.Path(__file__).parents[2] / "test.py")
_spec = importlib.util.spec_from_file_location("test_py", _test_py_path)
_test_py = importlib.util.module_from_spec(_spec)
sys.modules["test_py"] = _test_py
_spec.loader.exec_module(_test_py)

ThreadsCalculator = _test_py.ThreadsCalculator
setup_signal_handlers = _test_py.setup_signal_handlers
run_pytest_fn = _test_py.run_pytest
print_summary_fn = _test_py.print_summary
open_log_fn = _test_py.open_log


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sysconf_for_mem(sys_mem_bytes: int, page_size: int = 4096):
    """Return a side_effect function for os.sysconf that yields *sys_mem_bytes*."""
    phys_pages = sys_mem_bytes // page_size

    def _sysconf(name):
        if name == "SC_PAGE_SIZE":
            return page_size
        if name == "SC_PHYS_PAGES":
            return phys_pages
        raise ValueError(f"unexpected sysconf key: {name}")

    return _sysconf


def _make_run_pytest_options(tmp_path, **overrides):
    """Build a minimal argparse.Namespace accepted by run_pytest."""
    defaults = dict(
        tmpdir=str(tmp_path),
        name=None,
        repeat=1,
        modes=["dev"],
        list_tests=False,
        jobs=4,
        max_failures=0,
        verbose=False,
        quiet=False,
        pytest_arg=None,
        random_seed=None,
        gather_metrics=False,
        timeout=3600,
        session_timeout=24000,
        skip_patterns=None,
        k=None,
        extra_scylla_cmdline_options="",
        save_log_on_success=False,
        markers=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _build_junit_xml(total_tests: int, test_cases: list[dict]) -> Element:
    """Build a mock JUnit XML tree for run_pytest XML-parsing tests.

    Each *test_cases* dict has keys ``classname``, ``name``, and optionally
    ``failure`` or ``error`` (values are message strings).
    """
    root = Element("testsuites")
    suite = Element("testsuite", attrib={"tests": str(total_tests)})
    root.append(suite)
    for tc in test_cases:
        tc_elem = Element("testcase", attrib={
            "classname": tc["classname"],
            "name": tc["name"],
        })
        if "failure" in tc:
            tc_elem.append(Element("failure", attrib={"message": tc["failure"]}))
        if "error" in tc:
            tc_elem.append(Element("error", attrib={"message": tc["error"]}))
        suite.append(tc_elem)
    return root


# ---------------------------------------------------------------------------
# ThreadsCalculator
# ---------------------------------------------------------------------------


class TestThreadsCalculator:
    """Characterize ThreadsCalculator memory/CPU job computation."""

    @patch("os.sysconf")
    def test_debug_mode_uses_higher_memory_and_cpus(self, mock_sysconf):
        """Debug mode applies debug_test_memory_multiplier and uses more CPUs per job.

        With 64 GB RAM and default parameters:
          test_mem  = min(64G/8, 5G) * 1.5 = 7.5 GB
          reserve   = 5 GB
          available = 59 GB
          jobs_mem  = int(59G / 7.5G) = 7
        """
        mock_sysconf.side_effect = _sysconf_for_mem(int(64e9))
        tc = ThreadsCalculator(["debug"])
        assert tc.cpus_per_test_job == 1.5
        assert tc.default_num_jobs_mem == 7

    @patch("os.sysconf")
    def test_release_mode_uses_lower_memory_cap(self, mock_sysconf):
        """Non-debug mode uses non_debug_max_test_memory (4 GB) and 1.0 CPUs per job.

        With 64 GB RAM:
          test_mem  = min(64G/8, 4G) = 4 GB
          reserve   = 5 GB
          available = 59 GB
          jobs_mem  = int(59G / 4G) = 14
        """
        mock_sysconf.side_effect = _sysconf_for_mem(int(64e9))
        tc = ThreadsCalculator(["release"])
        assert tc.cpus_per_test_job == 1.0
        assert tc.default_num_jobs_mem == 14

    @patch("os.sysconf")
    def test_max_test_memory_only_affects_debug_mode(self, mock_sysconf):
        """max_test_memory parameter only affects debug mode.

        In non-debug mode, non_debug_max_test_memory is used instead.
        This is because lines 88-90 (the first test_mem calculation) are
        dead code — they are unconditionally overwritten by lines 97-102.
        """
        mock_sysconf.side_effect = _sysconf_for_mem(int(64e9))

        # Changing max_test_memory does NOT affect release mode
        tc_default = ThreadsCalculator(["release"])
        tc_custom = ThreadsCalculator(["release"], max_test_memory=1e9)
        assert tc_default.default_num_jobs_mem == tc_custom.default_num_jobs_mem

        # But it DOES affect debug mode
        tc_debug_default = ThreadsCalculator(["debug"])
        tc_debug_custom = ThreadsCalculator(["debug"], max_test_memory=2e9)
        assert tc_debug_custom.default_num_jobs_mem != tc_debug_default.default_num_jobs_mem

    @patch("os.sysconf")
    def test_minimum_one_job_with_tiny_memory(self, mock_sysconf):
        """Even with memory too small for a single test, at least 1 job is returned.

        With 1 GB RAM the reserve (5 GB) exceeds system memory, so
        available_mem = 0 and the floor of max(1, ...) kicks in.
        """
        mock_sysconf.side_effect = _sysconf_for_mem(int(1e9))
        tc = ThreadsCalculator(["release"])
        assert tc.default_num_jobs_mem == 1

    @patch("os.sysconf")
    def test_get_number_of_threads_returns_minimum_of_mem_and_cpu(self, mock_sysconf):
        """get_number_of_threads returns min(memory-based, CPU-based) job count."""
        mock_sysconf.side_effect = _sysconf_for_mem(int(64e9))
        tc = ThreadsCalculator(["debug"])

        # 16 CPUs: cpu-based = ceil(16/1.5) = 11 -> min(7, 11) = 7 (mem-limited)
        assert tc.get_number_of_threads(16) == 7

        # 4 CPUs: cpu-based = ceil(4/1.5) = 3 -> min(7, 3) = 3 (cpu-limited)
        assert tc.get_number_of_threads(4) == 3

    @patch("os.sysconf")
    def test_custom_parameters_override_defaults(self, mock_sysconf):
        """Custom constructor parameters change the computation."""
        mock_sysconf.side_effect = _sysconf_for_mem(int(32e9))
        tc = ThreadsCalculator(
            ["release"],
            non_debug_cpus_per_test_job=2.0,
            non_debug_max_test_memory=2e9,
        )
        assert tc.cpus_per_test_job == 2.0
        # test_mem = min(32G/8, 2G) = 2 GB, reserve = 5 GB, available = 27 GB
        # jobs_mem = int(27G / 2G) = 13
        assert tc.default_num_jobs_mem == 13
        # 8 CPUs: cpu-based = ceil(8/2.0) = 4 -> min(13, 4) = 4
        assert tc.get_number_of_threads(8) == 4


# ---------------------------------------------------------------------------
# setup_signal_handlers
# ---------------------------------------------------------------------------


class TestSetupSignalHandlers:
    """Characterize signal handler registration and the closure bug."""

    def test_registers_sigint_and_sigterm(self):
        """setup_signal_handlers registers handlers for both SIGINT and SIGTERM."""
        loop = MagicMock()
        signaled = MagicMock()
        setup_signal_handlers(loop, signaled)

        registered = [c.args[0] for c in loop.add_signal_handler.call_args_list]
        assert signal.SIGINT in registered
        assert signal.SIGTERM in registered
        assert len(registered) == 2

    def test_closure_captures_final_loop_variable(self):
        """Both handlers pass SIGTERM to shutdown() due to late-binding closure.

        The for-loop variable ``signo`` is captured by reference in the lambda.
        After the loop completes, ``signo`` is bound to ``signal.SIGTERM`` (the
        last iteration value), so the SIGINT handler also invokes
        ``shutdown(loop, signal.SIGTERM, signaled)`` instead of SIGINT.
        """
        loop = MagicMock()
        signaled = asyncio.Event()
        setup_signal_handlers(loop, signaled)

        callbacks = {
            c.args[0]: c.args[1]
            for c in loop.add_signal_handler.call_args_list
        }

        async def invoke_sigint_handler():
            callbacks[signal.SIGINT]()
            await asyncio.sleep(0)  # let the created task run

        asyncio.run(invoke_sigint_handler())

        # Bug: should be SIGINT, but the closure captured the loop variable
        assert signaled.signo == signal.SIGTERM


# ---------------------------------------------------------------------------
# run_pytest — argument assembly and XML parsing
# ---------------------------------------------------------------------------


class TestRunPytest:
    """Characterize run_pytest argument assembly and JUnit XML parsing."""

    @patch("test_py.ET")
    @patch("test_py.pytest")
    def test_basic_args_always_present(self, mock_pytest, mock_et, tmp_path):
        """Core arguments (color, repeat, mode flags, -n, --maxfail) are always present."""
        mock_pytest.main.return_value = 0
        mock_suite = MagicMock()
        mock_suite.get.return_value = "0"
        mock_suite.findall.return_value = []
        mock_et.parse.return_value.getroot.return_value.find.return_value = mock_suite

        options = _make_run_pytest_options(tmp_path, modes=["dev", "debug"])
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert "--color=yes" in args
        assert "--repeat=1" in args
        assert "--mode=dev" in args
        assert "--mode=debug" in args
        assert any(a.startswith("-n") for a in args)
        assert any(a.startswith("--maxfail=") for a in args)

    @patch("test_py.pytest")
    def test_list_tests_returns_early(self, mock_pytest, tmp_path):
        """When list_tests is True, returns (0, []) without parsing XML."""
        mock_pytest.main.return_value = 0
        options = _make_run_pytest_options(tmp_path, list_tests=True)
        total, failed = run_pytest_fn(options)
        assert total == 0
        assert failed == []

    @patch("test_py.ET")
    @patch("test_py.pytest")
    def test_verbose_adds_v_flag(self, mock_pytest, mock_et, tmp_path):
        """When verbose=True, -v is added to pytest args."""
        mock_pytest.main.return_value = 0
        mock_suite = MagicMock()
        mock_suite.get.return_value = "0"
        mock_suite.findall.return_value = []
        mock_et.parse.return_value.getroot.return_value.find.return_value = mock_suite

        options = _make_run_pytest_options(tmp_path, verbose=True)
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert "-v" in args

    @patch("test_py.ET")
    @patch("test_py.pytest")
    def test_quiet_adds_no_sugar(self, mock_pytest, mock_et, tmp_path):
        """When quiet=True, --quiet and -p no:sugar are added."""
        mock_pytest.main.return_value = 0
        mock_suite = MagicMock()
        mock_suite.get.return_value = "0"
        mock_suite.findall.return_value = []
        mock_et.parse.return_value.getroot.return_value.find.return_value = mock_suite

        options = _make_run_pytest_options(tmp_path, quiet=True)
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert "--quiet" in args
        idx = args.index("-p")
        assert args[idx + 1] == "no:sugar"

    @patch("test_py.ET")
    @patch("test_py.pytest")
    def test_skip_patterns_become_k_expression(self, mock_pytest, mock_et, tmp_path):
        """--skip patterns are translated to a -k=not ... expression."""
        mock_pytest.main.return_value = 0
        mock_suite = MagicMock()
        mock_suite.get.return_value = "0"
        mock_suite.findall.return_value = []
        mock_et.parse.return_value.getroot.return_value.find.return_value = mock_suite

        options = _make_run_pytest_options(tmp_path, skip_patterns=["foo", "bar"])
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        k_args = [a for a in args if isinstance(a, str) and a.startswith("-k=")]
        assert len(k_args) == 1
        assert k_args[0] == "-k=not foo and not bar"

    @patch("test_py.ET")
    @patch("test_py.pytest")
    def test_failed_tests_extracted_from_xml(self, mock_pytest, mock_et, tmp_path):
        """Failed test cases are extracted from JUnit XML into SimpleNamespace objects."""
        mock_pytest.main.return_value = 0
        xml_root = _build_junit_xml(3, [
            {"classname": "cql.test_types", "name": "test_int.dev",
             "failure": "assert failed"},
            {"classname": "cql.test_types", "name": "test_text.dev"},  # passing
            {"classname": "boost.sstable_test.cc", "name": "test_read.dev",
             "error": "segfault"},
        ])
        mock_et.parse.return_value.getroot.return_value = xml_root

        options = _make_run_pytest_options(tmp_path)
        total, failed = run_pytest_fn(options)

        assert total == 3
        assert len(failed) == 2
        # Python test: classname parts -> path
        assert failed[0].name == "test/cql/test_types.py::test_int"
        # C++ test: classname ending in .cc
        assert failed[1].name == "test/boost/sstable_test.cc::test_read"

    @patch("test_py.pytest")
    def test_no_matching_files_skips_execution(self, mock_pytest, tmp_path):
        """When name filters match no pytest directories, execution is skipped."""
        options = _make_run_pytest_options(
            tmp_path,
            name=["nonexistent/path/test_foo.py"],
        )
        total, failed = run_pytest_fn(options)
        assert total == 0
        assert failed == []
        mock_pytest.main.assert_not_called()


# ---------------------------------------------------------------------------
# print_summary
# ---------------------------------------------------------------------------


class TestPrintSummary:
    """Characterize print_summary output for various scenarios."""

    @patch("test_py.multiprocessing")
    @patch("test_py.time")
    @patch("test_py.resource")
    def test_prints_cpu_utilization(self, mock_resource, mock_time, mock_mp, capsys):
        """CPU utilization line is always printed.

        cpu_used = ru_stime + ru_utime = 200
        cpu_available = (monotonic - launch_time) * cpu_count = 100 * 8 = 800
        utilization = 200 / 800 = 25.0%
        """
        rusage = MagicMock()
        rusage.ru_stime = 50.0
        rusage.ru_utime = 150.0
        mock_resource.getrusage.return_value = rusage
        mock_time.monotonic.return_value = 100.0
        mock_mp.cpu_count.return_value = 8

        with patch.object(_test_py, "launch_time", 0.0):
            print_summary_fn(argparse.Namespace(), [], 10)

        output = capsys.readouterr().out
        assert "25.0%" in output

    @patch("test_py.multiprocessing")
    @patch("test_py.time")
    @patch("test_py.resource")
    def test_prints_failure_summary(self, mock_resource, mock_time, mock_mp, capsys):
        """When tests fail, their names and count are printed."""
        rusage = MagicMock()
        rusage.ru_stime = 0.0
        rusage.ru_utime = 0.0
        mock_resource.getrusage.return_value = rusage
        mock_time.monotonic.return_value = 10.0
        mock_mp.cpu_count.return_value = 1

        failures = [
            SimpleNamespace(name="test/cql/test_types.py::test_int"),
            SimpleNamespace(name="test/boost/sstable_test.cc::test_read"),
        ]
        with patch.object(_test_py, "launch_time", 0.0):
            print_summary_fn(argparse.Namespace(), failures, 5)

        output = capsys.readouterr().out
        assert "test/cql/test_types.py::test_int" in output
        assert "test/boost/sstable_test.cc::test_read" in output
        assert "2 of the total 5 tests failed" in output

    @patch("test_py.multiprocessing")
    @patch("test_py.time")
    @patch("test_py.resource")
    @patch("test_py.palette")
    def test_zero_tests_prints_warning(
        self, mock_palette, mock_resource, mock_time, mock_mp, capsys,
    ):
        """When total_tests is 0, a 'No tests were run' warning is printed."""
        rusage = MagicMock()
        rusage.ru_stime = 0.0
        rusage.ru_utime = 0.0
        mock_resource.getrusage.return_value = rusage
        mock_time.monotonic.return_value = 1.0
        mock_mp.cpu_count.return_value = 1
        mock_palette.warn.side_effect = lambda msg: msg  # pass through

        with patch.object(_test_py, "launch_time", 0.0):
            print_summary_fn(argparse.Namespace(), [], 0)

        mock_palette.warn.assert_called_once()
        warn_msg = mock_palette.warn.call_args.args[0]
        assert "No tests were run" in warn_msg


# ---------------------------------------------------------------------------
# open_log
# ---------------------------------------------------------------------------


class TestOpenLog:
    """Characterize open_log directory creation and logging setup."""

    @patch("test_py.logging")
    @patch.object(pathlib.Path, "mkdir")
    def test_creates_directory_and_configures_logging(self, mock_mkdir, mock_logging):
        """open_log creates the tmpdir and configures logging.basicConfig."""
        open_log_fn("/tmp/test_logs", "test.log", "DEBUG")

        mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)
        mock_logging.basicConfig.assert_called_once()
        kwargs = mock_logging.basicConfig.call_args.kwargs
        assert kwargs["filename"] == "/tmp/test_logs/test.log"
        assert kwargs["filemode"] == "w"
        assert kwargs["level"] == "DEBUG"
        assert "asctime" in kwargs["format"]
        assert kwargs["datefmt"] == "%H:%M:%S"

    @patch("test_py.logging")
    @patch.object(pathlib.Path, "mkdir")
    def test_logs_startup_command(self, mock_mkdir, mock_logging):
        """open_log logs the command line via logging.critical."""
        open_log_fn("/tmp/test_logs", "test.log", "INFO")

        mock_logging.critical.assert_called_once()
        fmt_string = mock_logging.critical.call_args.args[0]
        assert "Started" in fmt_string
