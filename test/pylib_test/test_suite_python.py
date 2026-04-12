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
# TestSuite.clusters — pool_size resolution via @cached_property
# ===================================================================


class TestPoolSize:
    """Tests for the 4-tier pool_size priority in TestSuite.clusters."""

    def _make(
        self,
        tmp_path,
        mock_options,
        cfg_pool=None,
        env_pool=None,
        mode="dev",
    ):
        from test.pylib.suite import TestSuite

        suite_dir = tmp_path / "pool_suite"
        suite_dir.mkdir(exist_ok=True)
        cfg = {}
        if cfg_pool is not None:
            cfg["pool_size"] = cfg_pool
        env = {}
        if env_pool is not None:
            env["CLUSTER_POOL_SIZE"] = str(env_pool)
        with (
            patch("test.pylib.suite.Pool") as MockPool,
            patch.dict(os.environ, env, clear=False),
        ):
            suite = TestSuite(str(suite_dir), cfg, mock_options, mode)
            # Trigger the @cached_property to construct the Pool
            _ = suite.clusters
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

