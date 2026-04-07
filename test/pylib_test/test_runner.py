#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

"""Unit tests for test/pylib/runner.py — pytest plugin behavior.

These tests characterize the current behavior of runner.py, specifically
the --test-py-init option and the testpy_test_fixture_scope function.
"""

import os
from unittest.mock import MagicMock, patch

from test.pylib.runner import (
    pytest_addoption,
    pytest_configure,
    pytest_runtest_makereport,
    pytest_sessionfinish,
    pytest_sessionstart,
    testpy_test_fixture_scope,
)


# ---------------------------------------------------------------------------
# testpy_test_fixture_scope — dynamic scope based on --test-py-init
# ---------------------------------------------------------------------------


class TestFixtureScope:
    """Verify testpy_test_fixture_scope returns the correct scope based on --test-py-init."""

    def test_test_py_init_true_returns_module(self):
        """When --test-py-init is passed (test.py / bare pytest --test-py-init), scope is 'module'."""
        config = MagicMock()
        config.option.test_py_init = True
        assert testpy_test_fixture_scope("some_fixture", config) == "module"

    def test_test_py_init_false_returns_session(self):
        """When --test-py-init is not passed, scope is 'session'."""
        config = MagicMock()
        config.option.test_py_init = False
        assert testpy_test_fixture_scope("some_fixture", config) == "session"

    def test_test_py_init_missing_returns_session(self):
        """When config.option has no test_py_init attribute, scope is 'session'."""
        config = MagicMock()
        config.option = MagicMock(spec=[])
        assert testpy_test_fixture_scope("some_fixture", config) == "session"

    def test_not_collected_as_test(self):
        """testpy_test_fixture_scope has __test__ = False to prevent pytest collection."""
        assert testpy_test_fixture_scope.__test__ is False


# ---------------------------------------------------------------------------
# pytest_addoption — --test-py-init is registered
# ---------------------------------------------------------------------------


class TestAddoption:
    """Verify that pytest_addoption registers --test-py-init."""

    def test_test_py_init_is_registered(self):
        """pytest_addoption registers --test-py-init on the parser."""
        parser = MagicMock()
        pytest_addoption(parser)
        registered = {name for call in parser.addoption.call_args_list for name in call.args}
        assert "--test-py-init" in registered

    def test_test_py_init_defaults_to_false(self):
        """--test-py-init defaults to False with store_true action."""
        parser = MagicMock()
        pytest_addoption(parser)
        for call in parser.addoption.call_args_list:
            if "--test-py-init" in call.args:
                assert call.kwargs.get("default") is False
                assert call.kwargs.get("action") == "store_true"
                break
        else:
            raise AssertionError("--test-py-init not found in addoption calls")


# ---------------------------------------------------------------------------
# Hook behavior — --test-py-init guards
# ---------------------------------------------------------------------------


class TestHookBehavior:
    """Verify that runner.py hooks gate on --test-py-init."""

    @patch("test.pylib.runner.init_testsuite_globals")
    @patch("test.pylib.runner.TEST_RUNNER", "pytest")
    def test_sessionstart_skips_without_test_py_init(self, mock_init):
        """pytest_sessionstart returns early when --test-py-init is False."""
        session = MagicMock()
        session.config.getoption.side_effect = lambda opt: {
            "--collect-only": False,
            "--test-py-init": False,
        }[opt]
        pytest_sessionstart(session)
        mock_init.assert_not_called()

    @patch("test.pylib.runner.prepare_environment")
    @patch("test.pylib.runner.TestSuite")
    @patch("test.pylib.runner.init_testsuite_globals")
    @patch("test.pylib.runner.xdist")
    @patch("test.pylib.runner.TEST_RUNNER", "pytest")
    @patch("test.pylib.runner.get_modes_to_run", return_value=["debug"])
    def test_sessionstart_proceeds_with_test_py_init(
        self, mock_get_modes, mock_xdist, mock_init, mock_ts, mock_prep
    ):
        """pytest_sessionstart initializes the environment when --test-py-init is True."""
        mock_xdist.is_xdist_worker.return_value = False
        session = MagicMock()
        session.config.getoption.side_effect = lambda opt: {
            "--collect-only": False,
            "--test-py-init": True,
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
        """pytest_sessionstart returns early when TEST_RUNNER is 'runpy'."""
        session = MagicMock()
        pytest_sessionstart(session)
        mock_init.assert_not_called()

    @patch("test.pylib.runner.xdist")
    def test_sessionfinish_skips_without_test_py_init(self, mock_xdist):
        """pytest_sessionfinish returns early when --test-py-init is False."""
        session = MagicMock()
        session.config.getoption.return_value = False
        pytest_sessionfinish(session)
        mock_xdist.is_xdist_worker.assert_not_called()

    @patch("test.pylib.runner._pytest_config")
    @patch("test.pylib.runner.get_modes_to_run", return_value=["debug"])
    @patch("test.pylib.runner.logging")
    def test_configure_skips_logging_without_test_py_init(self, mock_logging, _, __):
        """pytest_configure skips logging setup when --test-py-init is False."""
        config = MagicMock()
        config.getoption.side_effect = lambda opt: {
            "--test-py-init": False,
            "--exe-url": False,
            "--exe-path": False,
            "--repeat": "1",
            "--run_id": None,
        }[opt]
        with patch.dict(os.environ, {}, clear=False):
            pytest_configure(config)
        mock_logging.basicConfig.assert_not_called()

    @patch("test.pylib.runner._pytest_config")
    def test_makereport_skips_without_test_py_init(self, mock_config):
        """pytest_runtest_makereport does not process results when --test-py-init is False."""
        mock_config.getoption.return_value = False
        gen = pytest_runtest_makereport(MagicMock(), MagicMock())
        next(gen)
        outcome = MagicMock()
        try:
            gen.send(outcome)
        except StopIteration:
            pass
        outcome.get_result.assert_not_called()
