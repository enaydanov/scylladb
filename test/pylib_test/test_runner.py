#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

"""Unit tests for test/pylib/runner.py — pytest plugin behavior.

These tests verify:
- Phase 2 changes: the removal of --test-py-init and the change of
  testpy_test_fixture_scope to use TEST_RUNNER instead.
- Phase 3 changes: the thread-based resource watcher moved from test.py.
"""

import os
import threading
from unittest.mock import MagicMock, patch, call

import pytest

from test.pylib.runner import (
    _resource_monitor_loop,
    _start_resource_watcher,
    _stop_resource_watcher,
    pytest_addoption,
    pytest_configure,
    pytest_runtest_makereport,
    pytest_sessionfinish,
    pytest_sessionstart,
    testpy_test_fixture_scope,
)


# ---------------------------------------------------------------------------
# testpy_test_fixture_scope — dynamic scope based on TEST_RUNNER
# ---------------------------------------------------------------------------


class TestFixtureScope:
    """Verify testpy_test_fixture_scope returns the correct scope for each execution mode."""

    def test_pytest_runner_returns_module(self):
        """When TEST_RUNNER is 'pytest' (test.py and bare pytest), scope is 'module'."""
        config = MagicMock()
        with patch("test.pylib.runner.TEST_RUNNER", "pytest"):
            assert testpy_test_fixture_scope("some_fixture", config) == "module"

    def test_runpy_runner_returns_session(self):
        """When TEST_RUNNER is 'runpy' (run.py scripts), scope is 'session'."""
        config = MagicMock()
        with patch("test.pylib.runner.TEST_RUNNER", "runpy"):
            assert testpy_test_fixture_scope("some_fixture", config) == "session"

    def test_config_arg_is_unused(self):
        """The config argument is accepted but the scope depends only on TEST_RUNNER."""
        with patch("test.pylib.runner.TEST_RUNNER", "pytest"):
            assert testpy_test_fixture_scope("x", None) == "module"

    def test_not_collected_as_test(self):
        """testpy_test_fixture_scope has __test__ = False to prevent pytest collection."""
        assert testpy_test_fixture_scope.__test__ is False


# ---------------------------------------------------------------------------
# pytest_addoption — --test-py-init is removed
# ---------------------------------------------------------------------------


class TestAddoption:
    """Verify that pytest_addoption no longer registers --test-py-init."""

    def test_test_py_init_not_registered(self):
        """pytest_addoption does not register --test-py-init."""
        parser = MagicMock()
        pytest_addoption(parser)
        registered = {name for call in parser.addoption.call_args_list for name in call.args}
        assert "--test-py-init" not in registered


# ---------------------------------------------------------------------------
# Hook behavior — --test-py-init guards removed
# ---------------------------------------------------------------------------


class TestHookBehavior:
    """Verify that runner.py hooks no longer gate on --test-py-init."""

    @patch("test.pylib.runner.prepare_environment")
    @patch("test.pylib.runner.TestSuite")
    @patch("test.pylib.runner.init_testsuite_globals")
    @patch("test.pylib.runner.xdist")
    @patch("test.pylib.runner.TEST_RUNNER", "pytest")
    @patch("test.pylib.runner.get_modes_to_run", return_value=["debug"])
    def test_sessionstart_proceeds_for_pytest_runner(
        self, mock_get_modes, mock_xdist, mock_init, mock_ts, mock_prep
    ):
        """pytest_sessionstart initializes the environment for the pytest runner."""
        mock_xdist.is_xdist_worker.return_value = False
        session = MagicMock()
        session.config.getoption.side_effect = lambda opt: {
            "--collect-only": False,
            "--tmpdir": "/tmp/test",
            "--gather-metrics": False,
            "--save-log-on-success": False,
            "--byte-limit": 100,
        }[opt]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TESTPY_PREPARED_ENVIRONMENT", None)
            pytest_sessionstart(session)
        mock_init.assert_called_once()
        mock_prep.assert_called_once()

    @patch("test.pylib.runner.init_testsuite_globals")
    @patch("test.pylib.runner.TEST_RUNNER", "runpy")
    def test_sessionstart_skips_for_runpy(self, mock_init):
        """pytest_sessionstart still returns early when TEST_RUNNER is 'runpy'."""
        session = MagicMock()
        pytest_sessionstart(session)
        mock_init.assert_not_called()

    @patch("test.pylib.runner.xdist")
    def test_sessionfinish_always_runs(self, mock_xdist):
        """pytest_sessionfinish no longer gates on --test-py-init."""
        mock_xdist.is_xdist_worker.return_value = True
        session = MagicMock()
        pytest_sessionfinish(session)
        mock_xdist.is_xdist_worker.assert_called_once()

    @patch("test.pylib.runner._pytest_config")
    @patch("test.pylib.runner.get_modes_to_run", return_value=["debug"])
    @patch("test.pylib.runner.logging")
    def test_configure_always_sets_up_logging(self, mock_logging, _, __, tmp_path):
        """pytest_configure always sets up logging (no --test-py-init guard)."""
        config = MagicMock()
        config.getoption.side_effect = lambda opt: {
            "--tmpdir": str(tmp_path),
            "--save-log-on-success": False,
            "--exe-url": False,
            "--exe-path": False,
            "--repeat": "1",
            "--run_id": None,
        }[opt]
        config.getini.return_value = "%(message)s"
        config.stash = {}
        with patch.dict(os.environ, {}, clear=False):
            pytest_configure(config)
        mock_logging.basicConfig.assert_called()

    @patch("test.pylib.runner._pytest_config")
    def test_makereport_always_processes(self, mock_config):
        """pytest_runtest_makereport always processes results (no --test-py-init guard)."""
        rep = MagicMock()
        rep.failed = False
        outcome = MagicMock()
        outcome.get_result.return_value = rep
        mock_config.getoption.side_effect = lambda opt: {
            "--tmpdir": "/tmp/test",
            "--save-log-on-success": False,
        }[opt]
        gen = pytest_runtest_makereport(MagicMock(), MagicMock())
        next(gen)
        try:
            gen.send(outcome)
        except StopIteration:
            pass
        outcome.get_result.assert_called_once()


# ---------------------------------------------------------------------------
# Resource watcher — thread-based system resource monitoring
# ---------------------------------------------------------------------------


class TestResourceWatcher:
    """Verify the thread-based resource watcher lifecycle."""

    @patch("test.pylib.runner.SQLiteWriter")
    @patch("test.pylib.runner.psutil")
    def test_monitor_loop_writes_metrics(self, mock_psutil, mock_writer_cls, tmp_path):
        """_resource_monitor_loop writes SystemResourceMetric records to SQLite.

        The loop calls stop_event.wait(timeout=2.0); when wait returns False
        (timeout expired, not stopped), it writes a metric.  We mock the stop
        event's wait method to return False once (triggering one write), then
        True (stopping the loop), avoiding the 2-second real wait.
        """
        mock_psutil.cpu_percent.return_value = 42.5
        mock_psutil.virtual_memory.return_value = MagicMock(percent=65.0)
        mock_writer = MagicMock()
        mock_writer_cls.return_value = mock_writer

        stop_event = MagicMock()
        # First call: not stopped (write happens), second call: stopped (loop exits)
        stop_event.wait.side_effect = [False, True]

        _resource_monitor_loop(stop_event, tmp_path)

        assert mock_writer.write_row.call_count == 1
        record = mock_writer.write_row.call_args_list[0].args[0]
        assert record.cpu == 42.5
        assert record.memory == 65.0

    @patch("test.pylib.runner._resource_monitor_loop")
    def test_start_creates_and_starts_thread(self, mock_loop, tmp_path):
        """_start_resource_watcher creates a daemon thread and starts it."""
        import test.pylib.runner as runner

        _start_resource_watcher(tmp_path)

        try:
            assert runner._resource_watcher_thread is not None
            assert runner._resource_watcher_stop is not None
            assert runner._resource_watcher_thread.daemon is True
            assert runner._resource_watcher_thread.name == "resource-watcher"
        finally:
            _stop_resource_watcher()

    @patch("test.pylib.runner._resource_monitor_loop")
    def test_stop_sets_event_and_joins(self, mock_loop, tmp_path):
        """_stop_resource_watcher sets the stop event and joins the thread."""
        import test.pylib.runner as runner

        _start_resource_watcher(tmp_path)
        thread = runner._resource_watcher_thread
        stop_event = runner._resource_watcher_stop

        _stop_resource_watcher()

        assert stop_event.is_set()
        assert not thread.is_alive()
        assert runner._resource_watcher_thread is None
        assert runner._resource_watcher_stop is None

    def test_stop_is_safe_when_not_started(self):
        """_stop_resource_watcher is a no-op when no watcher is running."""
        import test.pylib.runner as runner

        runner._resource_watcher_stop = None
        runner._resource_watcher_thread = None
        _stop_resource_watcher()  # should not raise

    @patch("test.pylib.runner._start_resource_watcher")
    @patch("test.pylib.runner.prepare_environment")
    @patch("test.pylib.runner.TestSuite")
    @patch("test.pylib.runner.init_testsuite_globals")
    @patch("test.pylib.runner.xdist")
    @patch("test.pylib.runner.TEST_RUNNER", "pytest")
    @patch("test.pylib.runner.get_modes_to_run", return_value=["debug"])
    def test_sessionstart_starts_watcher_when_gather_metrics(
        self, mock_get_modes, mock_xdist, mock_init, mock_ts, mock_prep, mock_start
    ):
        """pytest_sessionstart starts the resource watcher when --gather-metrics is True."""
        mock_xdist.is_xdist_worker.return_value = False
        session = MagicMock()
        session.config.getoption.side_effect = lambda opt: {
            "--collect-only": False,
            "--tmpdir": "/tmp/test",
            "--gather-metrics": True,
            "--save-log-on-success": False,
            "--byte-limit": 100,
        }[opt]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TESTPY_PREPARED_ENVIRONMENT", None)
            pytest_sessionstart(session)
        mock_start.assert_called_once()

    @patch("test.pylib.runner._start_resource_watcher")
    @patch("test.pylib.runner.prepare_environment")
    @patch("test.pylib.runner.TestSuite")
    @patch("test.pylib.runner.init_testsuite_globals")
    @patch("test.pylib.runner.xdist")
    @patch("test.pylib.runner.TEST_RUNNER", "pytest")
    @patch("test.pylib.runner.get_modes_to_run", return_value=["debug"])
    def test_sessionstart_skips_watcher_when_no_gather_metrics(
        self, mock_get_modes, mock_xdist, mock_init, mock_ts, mock_prep, mock_start
    ):
        """pytest_sessionstart does not start the resource watcher when --gather-metrics is False."""
        mock_xdist.is_xdist_worker.return_value = False
        session = MagicMock()
        session.config.getoption.side_effect = lambda opt: {
            "--collect-only": False,
            "--tmpdir": "/tmp/test",
            "--gather-metrics": False,
            "--save-log-on-success": False,
            "--byte-limit": 100,
        }[opt]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TESTPY_PREPARED_ENVIRONMENT", None)
            pytest_sessionstart(session)
        mock_start.assert_not_called()

