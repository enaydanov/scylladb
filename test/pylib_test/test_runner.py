#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

"""Unit tests for test/pylib/runner.py — pytest plugin behavior.

These tests verify Phase 2 changes: the removal of --test-py-init and the
change of testpy_test_fixture_scope to use TEST_RUNNER instead.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from test.pylib.runner import (
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
