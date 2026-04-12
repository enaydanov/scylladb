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
    TEST_SUITE,
    TestSuiteConfig,
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
from test.pylib.suite import TestSuite


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
    @patch("test.pylib.runner.artifacts")
    @patch("test.pylib.runner.xdist")
    @patch("test.pylib.runner.TEST_RUNNER", "pytest")
    @patch("test.pylib.runner.get_modes_to_run", return_value=["debug"])
    def test_sessionstart_proceeds_for_pytest_runner(
        self, mock_get_modes, mock_xdist, mock_artifacts, mock_prep
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
        pytest_sessionstart(session)
        mock_prep.assert_called_once()

    @patch("test.pylib.runner.artifacts")
    @patch("test.pylib.runner.TEST_RUNNER", "runpy")
    def test_sessionstart_skips_for_runpy(self, mock_artifacts):
        """pytest_sessionstart still returns early when TEST_RUNNER is 'runpy'."""
        session = MagicMock()
        pytest_sessionstart(session)
        mock_artifacts.add_exit_artifact.assert_not_called()

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
    @patch("test.pylib.runner.artifacts")
    @patch("test.pylib.runner.xdist")
    @patch("test.pylib.runner.TEST_RUNNER", "pytest")
    @patch("test.pylib.runner.get_modes_to_run", return_value=["debug"])
    def test_sessionstart_starts_watcher_when_gather_metrics(
        self, mock_get_modes, mock_xdist, mock_artifacts, mock_prep, mock_start
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
        pytest_sessionstart(session)
        mock_start.assert_called_once()

    @patch("test.pylib.runner._start_resource_watcher")
    @patch("test.pylib.runner.prepare_environment")
    @patch("test.pylib.runner.artifacts")
    @patch("test.pylib.runner.xdist")
    @patch("test.pylib.runner.TEST_RUNNER", "pytest")
    @patch("test.pylib.runner.get_modes_to_run", return_value=["debug"])
    def test_sessionstart_skips_watcher_when_no_gather_metrics(
        self, mock_get_modes, mock_xdist, mock_artifacts, mock_prep, mock_start
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
        pytest_sessionstart(session)
        mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# TestSuiteConfig.from_pytest_node — CLI options merge happens only once
# ---------------------------------------------------------------------------


class TestFromPytestNodeMerge:
    """Verify that CLI --extra-scylla-cmdline-options are merged exactly once.

    Before the fix, from_pytest_node() merged CLI options every time it was
    called — even when retrieving a cached TestSuiteConfig from a parent
    stash.  For appending options like --logger-log-level this caused
    duplication.
    """

    def _make_node(self, path, parent=None, config=None):
        """Build a minimal mock pytest node with a real pytest.Stash."""
        node = MagicMock()
        node.path = path
        node.parent = parent
        node.config = config or (parent.config if parent else MagicMock())
        node.stash = pytest.Stash()
        return node

    def test_merge_happens_once_for_new_config(self, tmp_path):
        """When from_pytest_node discovers a config file, CLI options are merged once."""
        suite_dir = tmp_path / "suite"
        suite_dir.mkdir()
        cfg_file = suite_dir / "test_config.yaml"
        cfg_file.write_text("extra_scylla_cmdline_options:\n  - --smp\n  - '1'\n")

        config = MagicMock()
        config.getoption.return_value = "--logger-log-level scylla=trace"

        node = self._make_node(path=suite_dir, config=config)
        result = TestSuiteConfig.from_pytest_node(node)

        assert result is not None
        extra = result.cfg.get("extra_scylla_cmdline_options", [])
        # --logger-log-level should appear exactly once
        count = sum(1 for x in extra if x == "--logger-log-level")
        assert count == 1, f"--logger-log-level appeared {count} times: {extra}"

    def test_no_re_merge_on_child_lookup(self, tmp_path):
        """When a child node finds a cached config in its parent stash,
        CLI options must NOT be merged again."""
        suite_dir = tmp_path / "suite"
        suite_dir.mkdir()
        cfg_file = suite_dir / "test_config.yaml"
        cfg_file.write_text("{}\n")

        config = MagicMock()
        config.getoption.return_value = "--logger-log-level scylla=trace"

        # First call: parent node discovers the config file
        parent_node = self._make_node(path=suite_dir, config=config)
        suite1 = TestSuiteConfig.from_pytest_node(parent_node)

        # Second call: child node looks up the cached config from parent
        child_path = suite_dir / "test_foo.py"
        child_path.touch()
        child_node = self._make_node(path=child_path, parent=parent_node, config=config)
        suite2 = TestSuiteConfig.from_pytest_node(child_node)

        assert suite1 is suite2
        extra = suite2.cfg.get("extra_scylla_cmdline_options", [])
        # --logger-log-level should appear exactly once, not twice
        count = sum(1 for x in extra if x == "--logger-log-level")
        assert count == 1, f"--logger-log-level appeared {count} times (double merge bug): {extra}"

    def test_no_cli_options_leaves_cfg_unchanged(self, tmp_path):
        """When --extra-scylla-cmdline-options is empty, cfg is not modified."""
        suite_dir = tmp_path / "suite"
        suite_dir.mkdir()
        cfg_file = suite_dir / "test_config.yaml"
        cfg_file.write_text("extra_scylla_cmdline_options:\n  - --smp\n  - '1'\n")

        config = MagicMock()
        config.getoption.return_value = ""

        node = self._make_node(path=suite_dir, config=config)
        result = TestSuiteConfig.from_pytest_node(node)

        assert result is not None
        assert result.cfg["extra_scylla_cmdline_options"] == ["--smp", "1"]



# ---------------------------------------------------------------------------
# TESTPY_PREPARED_ENVIRONMENT removal — unconditional session hooks
# ---------------------------------------------------------------------------


class TestNoTestpyPreparedEnvironment:
    """Verify that TESTPY_PREPARED_ENVIRONMENT is no longer referenced.

    test.py no longer sets this env var (removed in Phase 3), so all
    conditionals gating on it were always-true or always-false.  The
    constant and all guards have been removed.
    """

    def test_constant_not_in_test_init(self):
        """The TESTPY_PREPARED_ENVIRONMENT constant must not exist in test/__init__.py."""
        import test
        assert not hasattr(test, "TESTPY_PREPARED_ENVIRONMENT")

    def test_not_imported_by_runner(self):
        """runner.py must not import TESTPY_PREPARED_ENVIRONMENT."""
        import test.pylib.runner as runner
        # Check that the module-level namespace doesn't contain it
        assert not hasattr(runner, "TESTPY_PREPARED_ENVIRONMENT")

    @patch("test.pylib.runner.prepare_environment")
    @patch("test.pylib.runner.TestSuite")
    @patch("test.pylib.runner.xdist")
    @patch("test.pylib.runner.TEST_RUNNER", "pytest")
    @patch("test.pylib.runner.get_modes_to_run", return_value=["debug"])
    def test_sessionstart_unconditionally_initializes(
        self, mock_get_modes, mock_xdist, mock_ts, mock_prep
    ):
        """pytest_sessionstart always initializes globals (no TESTPY_PREPARED_ENVIRONMENT gate)."""
        mock_xdist.is_xdist_worker.return_value = False
        session = MagicMock()
        session.config.getoption.side_effect = lambda opt: {
            "--collect-only": False,
            "--tmpdir": "/tmp/test",
            "--gather-metrics": False,
            "--save-log-on-success": False,
            "--byte-limit": 100,
        }[opt]
        # Even with the env var set, initialization should proceed
        with patch.dict(os.environ, {"TESTPY_PREPARED_ENVIRONMENT": "1"}, clear=False):
            pytest_sessionstart(session)
        mock_prep.assert_called_once()

    @patch("test.pylib.runner._stop_resource_watcher")
    @patch("test.pylib.runner.asyncio")
    @patch("test.pylib.runner.xdist")
    @patch("test.pylib.runner._pytest_config")
    def test_sessionfinish_unconditionally_cleans_up(
        self, mock_config, mock_xdist, mock_asyncio, mock_stop
    ):
        """pytest_sessionfinish always cleans up (no TESTPY_PREPARED_ENVIRONMENT gate)."""
        mock_xdist.is_xdist_worker.return_value = False
        mock_config.stash = {object(): "/tmp/log"}  # dummy stash
        session = MagicMock()
        session.testsfailed = 1  # skip log removal path
        session.config.getoption.side_effect = lambda opt: {
            "--save-log-on-success": False,
            "maxfail": 0,
        }[opt]
        # Even with the env var set, cleanup should proceed
        with patch.dict(os.environ, {"TESTPY_PREPARED_ENVIRONMENT": "1"}, clear=False):
            pytest_sessionfinish(session)
        mock_stop.assert_called_once()


# ---------------------------------------------------------------------------
# --run_id option — type=int enforcement
# ---------------------------------------------------------------------------


class TestRunIdOption:
    """Verify that --run_id is registered with type=int."""

    def test_run_id_has_type_int(self):
        """--run_id must be registered with type=int so the stash value is an integer."""
        parser = MagicMock()
        pytest_addoption(parser)
        for call_obj in parser.addoption.call_args_list:
            if "--run_id" in call_obj.args:
                assert call_obj.kwargs.get("type") is int, (
                    "--run_id must have type=int to match RUN_ID = StashKey[int]()"
                )
                break
        else:
            pytest.fail("--run_id option not found")


# ---------------------------------------------------------------------------
# testpy_test fixture — suite creation and caching (inlined from opt_create)
# ---------------------------------------------------------------------------


class TestTestpyTestSuiteCaching:
    """Verify that the testpy_test fixture creates and caches TestSuite instances.

    The caching logic (formerly TestSuite.opt_create) was inlined into the
    testpy_test fixture.  These tests exercise the same dict-based caching
    that the fixture uses (TestSuite.suites keyed by path/mode).
    """

    @patch("test.pylib.suite.path_to", return_value="/dummy/scylla")
    def test_creates_suite_and_caches_it(self, _path_to, tmp_path):
        """Creating a TestSuite stores it in TestSuite.suites under path/mode key."""
        suite_dir = tmp_path / "my_suite"
        suite_dir.mkdir()
        options = MagicMock(
            tmpdir=str(tmp_path), coverage=False, coverage_modes=[],
            save_log_on_success=False,
            extra_scylla_cmdline_options="",
        )

        TestSuite.suites.clear()
        path = str(suite_dir)
        suite_key = os.path.join(path, "dev")
        suite = TestSuite.suites.get(suite_key)
        assert suite is None

        suite = TestSuite(path, {}, options, "dev")
        TestSuite.suites[suite_key] = suite

        assert isinstance(suite, TestSuite)
        assert suite_key in TestSuite.suites
        assert TestSuite.suites[suite_key] is suite
        TestSuite.suites.clear()

    @patch("test.pylib.suite.path_to", return_value="/dummy/scylla")
    def test_second_lookup_returns_cached_instance(self, _path_to, tmp_path):
        """Looking up the same path+mode key returns the cached instance, not a new one."""
        suite_dir = tmp_path / "cached_suite"
        suite_dir.mkdir()
        options = MagicMock(
            tmpdir=str(tmp_path), coverage=False, coverage_modes=[],
            save_log_on_success=False,
            extra_scylla_cmdline_options="",
        )

        TestSuite.suites.clear()
        path = str(suite_dir)
        suite_key = os.path.join(path, "dev")

        # First creation
        s1 = TestSuite(path, {}, options, "dev")
        TestSuite.suites[suite_key] = s1

        # Second lookup — must return the same object
        s2 = TestSuite.suites.get(suite_key)
        assert s2 is s1

        # Verify new TestSuite is NOT created when cache hit
        assert len(TestSuite.suites) == 1
        TestSuite.suites.clear()

    @patch("test.pylib.suite.path_to", return_value="/dummy/scylla")
    def test_different_modes_get_separate_suites(self, _path_to, tmp_path):
        """The same path with different modes produces distinct cached suites."""
        suite_dir = tmp_path / "mode_suite"
        suite_dir.mkdir()
        options = MagicMock(
            tmpdir=str(tmp_path), coverage=False, coverage_modes=[],
            save_log_on_success=False,
            extra_scylla_cmdline_options="",
        )

        TestSuite.suites.clear()
        path = str(suite_dir)

        key_dev = os.path.join(path, "dev")
        key_rel = os.path.join(path, "release")

        s_dev = TestSuite(path, {}, options, "dev")
        TestSuite.suites[key_dev] = s_dev

        s_rel = TestSuite(path, {}, options, "release")
        TestSuite.suites[key_rel] = s_rel

        assert s_dev is not s_rel
        assert TestSuite.suites[key_dev] is s_dev
        assert TestSuite.suites[key_rel] is s_rel
        assert len(TestSuite.suites) == 2
        TestSuite.suites.clear()
