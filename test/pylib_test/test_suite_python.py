#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

"""Unit tests for test/pylib/suite/python.py and cql_approval.py.

Covers PythonTestSuite pool_size resolution, PythonTest._prepare_pytest_params(),
and CQLApprovalTestSuite pattern/extension overrides.
"""

import argparse
import os
import pathlib
from unittest.mock import patch, MagicMock

import pytest

from test.pylib.suite.base import TestSuite


# ===================================================================
# PythonTestSuite.__init__ — pool_size resolution
# ===================================================================


class TestPythonTestSuitePoolSize:
    """Tests for the 4-tier pool_size priority in PythonTestSuite.__init__."""

    def _make(
        self,
        tmp_path,
        mock_options,
        cfg_pool=None,
        env_pool=None,
        opt_pool=None,
        mode="dev",
    ):
        from test.pylib.suite.python import PythonTestSuite

        suite_dir = tmp_path / "pool_suite"
        suite_dir.mkdir(exist_ok=True)
        cfg = {"type": "Python"}
        if cfg_pool is not None:
            cfg["pool_size"] = cfg_pool
        mock_options.cluster_pool_size = opt_pool
        env = {}
        if env_pool is not None:
            env["CLUSTER_POOL_SIZE"] = str(env_pool)
        with (
            patch("test.pylib.suite.python.path_to", return_value="/dummy/scylla"),
            patch("test.pylib.suite.python.Pool") as MockPool,
            patch.dict(os.environ, env, clear=False),
        ):
            suite = PythonTestSuite(str(suite_dir), cfg, mock_options, mode)
            # Extract pool_size passed to Pool constructor
            pool_size_arg = MockPool.call_args[0][0]
            return suite, pool_size_arg

    def test_default_pool_size(self, tmp_path, mock_options):
        """Default pool_size is 2 when nothing else is set."""
        _, size = self._make(tmp_path, mock_options)
        assert size == 2

    def test_cfg_pool_size(self, tmp_path, mock_options):
        """pool_size from YAML config."""
        _, size = self._make(tmp_path, mock_options, cfg_pool=5)
        assert size == 5

    def test_env_overrides_cfg(self, tmp_path, mock_options):
        """CLUSTER_POOL_SIZE env var overrides YAML config."""
        _, size = self._make(tmp_path, mock_options, cfg_pool=5, env_pool=7)
        assert size == 7

    def test_option_overrides_env(self, tmp_path, mock_options):
        """options.cluster_pool_size overrides env var."""
        _, size = self._make(
            tmp_path, mock_options, cfg_pool=5, env_pool=7, opt_pool=10
        )
        assert size == 10

    def test_option_overrides_cfg(self, tmp_path, mock_options):
        """options.cluster_pool_size overrides YAML even without env."""
        _, size = self._make(tmp_path, mock_options, cfg_pool=5, opt_pool=3)
        assert size == 3


# ===================================================================
# PythonTest._prepare_pytest_params
# ===================================================================


class TestPrepareParams:
    """Tests for PythonTest._prepare_pytest_params() — core pytest arg builder."""

    def _make_test(
        self, tmp_path, mock_options, shortname="test_foo", casename=None, mode="dev"
    ):
        from test.pylib.suite.python import PythonTestSuite, PythonTest

        suite_dir = tmp_path / "param_suite"
        suite_dir.mkdir(exist_ok=True)
        cfg = {"type": "Python"}
        with (
            patch("test.pylib.suite.python.path_to", return_value="/dummy/scylla"),
            patch("test.pylib.suite.python.Pool"),
        ):
            suite = PythonTestSuite(str(suite_dir), cfg, mock_options, mode)
        test_no = suite.next_id((shortname, suite.suite_key))
        test = PythonTest(test_no, shortname, casename, suite)
        return test

    def test_basic_args(self, tmp_path, mock_options):
        test = self._make_test(tmp_path, mock_options)
        test._prepare_pytest_params(mock_options)
        args = test.args
        assert "-s" in args
        assert "-vv" in args
        assert "--log-level=DEBUG" in args
        assert any("junit_family=xunit2" in a for a in args)
        assert any(a.startswith("--mode=") for a in args)
        assert any(a.startswith("--run_id=") for a in args)
        assert any(a.startswith("--tmpdir=") for a in args)

    def test_mode_in_args(self, tmp_path, mock_options):
        test = self._make_test(tmp_path, mock_options, mode="release")
        test._prepare_pytest_params(mock_options)
        assert "--mode=release" in test.args

    def test_casename_appended(self, tmp_path, mock_options):
        test = self._make_test(
            tmp_path, mock_options, casename="TestClass::test_method"
        )
        test._prepare_pytest_params(mock_options)
        last_arg = test.args[-1]
        assert "::TestClass::test_method" in last_arg

    def test_no_casename(self, tmp_path, mock_options):
        test = self._make_test(tmp_path, mock_options, casename=None)
        test._prepare_pytest_params(mock_options)
        last_arg = test.args[-1]
        assert "::" not in last_arg

    def test_markers_option(self, tmp_path, mock_options):
        mock_options.markers = "not slow"
        test = self._make_test(tmp_path, mock_options)
        test._prepare_pytest_params(mock_options)
        assert "-m=not slow" in test.args
        # markers should also add exit code 5 to valid
        assert 5 in test.valid_exit_codes

    def test_no_markers(self, tmp_path, mock_options):
        mock_options.markers = None
        test = self._make_test(tmp_path, mock_options)
        test._prepare_pytest_params(mock_options)
        assert not any(a.startswith("-m=") for a in test.args)
        assert test.valid_exit_codes == [0]

    def test_gather_metrics(self, tmp_path, mock_options):
        mock_options.gather_metrics = True
        test = self._make_test(tmp_path, mock_options)
        test._prepare_pytest_params(mock_options)
        assert "--gather-metrics" in test.args

    def test_save_log_on_success(self, tmp_path, mock_options):
        mock_options.save_log_on_success = True
        test = self._make_test(tmp_path, mock_options)
        test._prepare_pytest_params(mock_options)
        assert "--save-log-on-success" in test.args
        assert "--allure-no-capture" not in test.args

    def test_no_save_log_allure_no_capture(self, tmp_path, mock_options):
        mock_options.save_log_on_success = False
        test = self._make_test(tmp_path, mock_options)
        test._prepare_pytest_params(mock_options)
        assert "--allure-no-capture" in test.args

    def test_file_extension_py(self, tmp_path, mock_options):
        """PythonTest uses .py extension in test arg."""
        test = self._make_test(tmp_path, mock_options, shortname="test_bar")
        test._prepare_pytest_params(mock_options)
        last_arg = test.args[-1]
        assert last_arg.endswith(".py")

    def test_pytest_arg_option(self, tmp_path, mock_options):
        """Extra pytest args from --pytest-arg are split and appended."""
        mock_options.pytest_arg = "--tb=short -x"
        test = self._make_test(tmp_path, mock_options)
        test._prepare_pytest_params(mock_options)
        assert "--tb=short" in test.args
        assert "-x" in test.args


# ===================================================================
# CQLApprovalTestSuite — pattern and extension
# ===================================================================


class TestCQLApprovalTestSuite:
    """Tests for CQLApprovalTestSuite overrides."""

    def _make(self, tmp_path, mock_options, mode="dev"):
        from test.pylib.suite.cql_approval import CQLApprovalTestSuite

        suite_dir = tmp_path / "cql_suite"
        suite_dir.mkdir(exist_ok=True)
        cfg = {"type": "Approval"}
        with (
            patch("test.pylib.suite.python.path_to", return_value="/dummy/scylla"),
            patch("test.pylib.suite.python.Pool"),
        ):
            return CQLApprovalTestSuite(str(suite_dir), cfg, mock_options, mode)

    def test_pattern_is_cql_suffix(self, tmp_path, mock_options):
        suite = self._make(tmp_path, mock_options)
        assert "_test.cql" in suite.pattern

    def test_file_ext_is_cql(self, tmp_path, mock_options):
        suite = self._make(tmp_path, mock_options)
        assert suite.test_file_ext == ".cql"

    def test_inherits_python_test_suite(self, tmp_path, mock_options):
        from test.pylib.suite.python import PythonTestSuite

        suite = self._make(tmp_path, mock_options)
        assert isinstance(suite, PythonTestSuite)
