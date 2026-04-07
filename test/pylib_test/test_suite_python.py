#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

"""Unit tests for TestSuite pool_size resolution."""

import argparse
import os
import pathlib
from unittest.mock import patch


from test.pylib.suite import TestSuite


# ===================================================================
# PythonTestSuite.__init__ — pool_size resolution
# ===================================================================


class TestPoolSize:
    """Tests for the 4-tier pool_size priority in TestSuite.__init__."""

    def _make(
        self,
        tmp_path,
        mock_options,
        cfg_pool=None,
        env_pool=None,
        opt_pool=None,
        mode="dev",
    ):
        from test.pylib.suite import TestSuite

        suite_dir = tmp_path / "pool_suite"
        suite_dir.mkdir(exist_ok=True)
        cfg = {}
        if cfg_pool is not None:
            cfg["pool_size"] = cfg_pool
        mock_options.cluster_pool_size = opt_pool
        env = {}
        if env_pool is not None:
            env["CLUSTER_POOL_SIZE"] = str(env_pool)
        with (
            patch("test.pylib.suite.path_to", return_value="/dummy/scylla"),
            patch("test.pylib.suite.Pool") as MockPool,
            patch.dict(os.environ, env, clear=False),
        ):
            suite = TestSuite(str(suite_dir), cfg, mock_options, mode)
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

