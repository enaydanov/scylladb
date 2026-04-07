#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

"""Unit tests for test.py — the thin wrapper around pytest.

Functions tested:
- ThreadsCalculator.__init__ / get_number_of_threads
- run_pytest (argument assembly)
"""

import argparse
import importlib.util
import math
import pathlib
import sys
from unittest.mock import MagicMock, patch

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
run_pytest_fn = _test_py.run_pytest


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
        coverage=False,
        coverage_modes=None,
        byte_limit=42,
        exe_path=False,
        exe_url=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


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
# run_pytest — argument assembly
# ---------------------------------------------------------------------------


class TestRunPytest:
    """Characterize run_pytest argument assembly."""

    @patch("test_py.pytest")
    def test_basic_args_always_present(self, mock_pytest, tmp_path):
        """Core arguments (color, repeat, mode flags, -n, --maxfail) are always present."""
        mock_pytest.main.return_value = 0

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
        """When list_tests is True, returns 0 (the pytest.main exit code)."""
        mock_pytest.main.return_value = 0
        options = _make_run_pytest_options(tmp_path, list_tests=True)
        exit_code = run_pytest_fn(options)
        assert exit_code == 0

    @patch("test_py.pytest")
    def test_verbose_adds_v_flag(self, mock_pytest, tmp_path):
        """When verbose=True, -v is added to pytest args."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(tmp_path, verbose=True)
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert "-v" in args

    @patch("test_py.pytest")
    def test_quiet_adds_no_sugar(self, mock_pytest, tmp_path):
        """When quiet=True, --quiet and -p no:sugar are added."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(tmp_path, quiet=True)
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert "--quiet" in args
        idx = args.index("-p")
        assert args[idx + 1] == "no:sugar"

    @patch("test_py.pytest")
    def test_skip_patterns_become_k_expression(self, mock_pytest, tmp_path):
        """--skip patterns are translated to a -k=not ... expression."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(tmp_path, skip_patterns=["foo", "bar"])
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        k_args = [a for a in args if isinstance(a, str) and a.startswith("-k=")]
        assert len(k_args) == 1
        assert k_args[0] == "-k=not foo and not bar"

    @patch("test_py.pytest")
    def test_no_matching_files_skips_execution(self, mock_pytest, tmp_path):
        """When name filters match no pytest directories, execution is skipped."""
        options = _make_run_pytest_options(
            tmp_path,
            name=["nonexistent/path/test_foo.py"],
        )
        exit_code = run_pytest_fn(options)
        assert exit_code == 0
        mock_pytest.main.assert_not_called()

    @patch("test_py.pytest")
    def test_returns_pytest_exit_code(self, mock_pytest, tmp_path):
        """run_pytest returns the exit code from pytest.main."""
        mock_pytest.main.return_value = 1

        options = _make_run_pytest_options(tmp_path)
        exit_code = run_pytest_fn(options)
        assert exit_code == 1

    @patch("test_py.pytest")
    def test_returns_zero_on_success(self, mock_pytest, tmp_path):
        """run_pytest returns 0 when pytest.main succeeds."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(tmp_path)
        exit_code = run_pytest_fn(options)
        assert exit_code == 0

    @patch("test_py.pytest")
    def test_coverage_forwarded_with_modes(self, mock_pytest, tmp_path):
        """When coverage=True, --coverage and --coverage-mode flags are forwarded."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(
            tmp_path, coverage=True, coverage_modes=["dev", "debug"],
        )
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert "--coverage" in args
        assert "--coverage-mode=dev" in args
        assert "--coverage-mode=debug" in args

    @patch("test_py.pytest")
    def test_coverage_disabled_not_forwarded(self, mock_pytest, tmp_path):
        """When coverage=False, --coverage flag is absent from pytest args."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(tmp_path, coverage=False)
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert "--coverage" not in args
        assert not any(a.startswith("--coverage-mode") for a in args if isinstance(a, str))

    @patch("test_py.pytest")
    def test_byte_limit_always_forwarded(self, mock_pytest, tmp_path):
        """--byte-limit is always forwarded with its value."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(tmp_path, byte_limit=1337)
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert "--byte-limit=1337" in args

    @patch("test_py.pytest")
    def test_exe_path_forwarded_when_set(self, mock_pytest, tmp_path):
        """When exe_path is set, --exe-path is forwarded."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(tmp_path, exe_path="/usr/bin/scylla")
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert "--exe-path=/usr/bin/scylla" in args

    @patch("test_py.pytest")
    def test_exe_path_not_forwarded_when_false(self, mock_pytest, tmp_path):
        """When exe_path is False (default), --exe-path is absent."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(tmp_path, exe_path=False)
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert not any(a.startswith("--exe-path") for a in args if isinstance(a, str))

    @patch("test_py.pytest")
    def test_exe_url_forwarded_when_set(self, mock_pytest, tmp_path):
        """When exe_url is set, --exe-url is forwarded."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(tmp_path, exe_url="https://example.com/scylla.tar")
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert "--exe-url=https://example.com/scylla.tar" in args

    @patch("test_py.pytest")
    def test_exe_url_not_forwarded_when_false(self, mock_pytest, tmp_path):
        """When exe_url is False (default), --exe-url is absent."""
        mock_pytest.main.return_value = 0

        options = _make_run_pytest_options(tmp_path, exe_url=False)
        run_pytest_fn(options)

        args = mock_pytest.main.call_args.kwargs["args"]
        assert not any(a.startswith("--exe-url") for a in args if isinstance(a, str))
