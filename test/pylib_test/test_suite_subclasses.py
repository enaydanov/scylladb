#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

"""Unit tests for Test.run_ctx() cluster lifecycle management."""

import argparse
import os
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from test.pylib.suite.base import TestSuite


# ===================================================================

# PythonTest.run_ctx()
# ===================================================================


class TestRunCtx:
    """Tests for Test.run_ctx() cluster lifecycle management."""

    def _make_suite(self, tmp_path, mock_options, cfg=None, mode="dev"):
        from test.pylib.suite import TestSuite

        suite_dir = tmp_path / "suite"
        suite_dir.mkdir(exist_ok=True)
        if cfg is None:
            cfg = {}
        with (
            patch("test.pylib.suite.path_to", return_value="/dummy/scylla"),
            patch("test.pylib.suite.Pool"),
        ):
            return TestSuite(str(suite_dir), cfg, mock_options, mode)

    def _make_test(self, tmp_path, mock_options, cfg=None, shortname="test_foo", mode="dev"):
        from test.pylib.suite import Test

        suite = self._make_suite(tmp_path, mock_options, cfg, mode)
        test_no = suite.next_id((shortname, suite.suite_key))
        return Test(test_no, shortname, suite)

    def _mock_cluster(self):
        """Create a mock cluster with the interface run_ctx() expects.

        Uses a spec class so that hasattr() behaves realistically —
        MagicMock without spec auto-creates every attribute, which
        would defeat the ``not hasattr(cluster, 'prepare_cql_executed')``
        guard in run_ctx().
        """
        class _ClusterSpec:
            endpoint = None
            server_log_filename = None
            is_dirty = False
            running = {}
            before_test = None
            after_test = None
            take_log_savepoint = None

        cluster = MagicMock(spec=_ClusterSpec)
        cluster.endpoint.return_value = "192.168.1.1"
        cluster.server_log_filename.return_value = "/tmp/scylla.log"
        cluster.is_dirty = False
        # prepare_cql needs cluster.running with a control_connection
        mock_server = MagicMock()
        cluster.running = {0: mock_server}
        return cluster

    def _mock_pool(self, cluster):
        """Create an async mock pool that yields the given cluster."""
        pool = AsyncMock()
        pool.get.return_value = cluster
        return pool

    @pytest.mark.asyncio
    async def test_run_ctx_happy_path(self, tmp_path, mock_options):
        """Normal entry and exit: cluster leased, used, returned clean."""
        test = self._make_test(tmp_path, mock_options)
        cluster = self._mock_cluster()
        pool = self._mock_pool(cluster)
        test.suite.clusters = pool

        async with test.run_ctx() as yielded_cluster:
            pass

        assert yielded_cluster is cluster
        cluster.before_test.assert_called_once_with(test.uname)
        cluster.after_test.assert_called_once_with(test.uname, True)
        pool.put.assert_awaited_once_with(cluster, is_dirty=False)

    @pytest.mark.asyncio
    async def test_run_ctx_dirty_cluster(self, tmp_path, mock_options):
        """When test is in dirties_cluster, cluster is returned as dirty."""
        test = self._make_test(tmp_path, mock_options, shortname="test_foo")
        test.suite.dirties_cluster = {"test_foo"}
        cluster = self._mock_cluster()
        pool = self._mock_pool(cluster)
        test.suite.clusters = pool

        async with test.run_ctx():
            pass

        assert cluster.is_dirty is True
        pool.put.assert_awaited_once_with(cluster, is_dirty=True)

    @pytest.mark.asyncio
    async def test_run_ctx_exception_marks_failure(self, tmp_path, mock_options):
        """An exception inside the context sets success=False and dirties cluster."""
        test = self._make_test(tmp_path, mock_options)
        cluster = self._mock_cluster()
        pool = self._mock_pool(cluster)
        test.suite.clusters = pool

        with pytest.raises(RuntimeError, match="boom"):
            async with test.run_ctx():
                raise RuntimeError("boom")

        assert test.success is False
        assert cluster.is_dirty is True
        pool.put.assert_awaited_once_with(cluster, is_dirty=True)

    @pytest.mark.asyncio
    async def test_run_ctx_before_test_failure(self, tmp_path, mock_options):
        """If before_test() raises, is_before_test_ok stays False and cluster is dirty."""
        test = self._make_test(tmp_path, mock_options)
        cluster = self._mock_cluster()
        cluster.before_test.side_effect = RuntimeError("start failed")
        pool = self._mock_pool(cluster)
        test.suite.clusters = pool

        with pytest.raises(RuntimeError, match="start failed"):
            async with test.run_ctx():
                pass  # pragma: no cover — never reached

        assert test.success is False
        assert cluster.is_dirty is True
        pool.put.assert_awaited_once_with(cluster, is_dirty=True)

    @pytest.mark.asyncio
    async def test_run_ctx_prepare_cql_executes_statements(self, tmp_path, mock_options):
        """When suite cfg has prepare_cql, statements are executed on control_connection."""
        cfg = {"prepare_cql": ["CREATE KEYSPACE ks", "USE ks"]}
        test = self._make_test(tmp_path, mock_options, cfg=cfg)
        cluster = self._mock_cluster()
        pool = self._mock_pool(cluster)
        test.suite.clusters = pool
        cc = cluster.running[0].control_connection

        async with test.run_ctx():
            pass

        assert cc.execute.call_count == 2
        cc.execute.assert_any_call("CREATE KEYSPACE ks")
        cc.execute.assert_any_call("USE ks")
        assert cluster.prepare_cql_executed is True

    @pytest.mark.asyncio
    async def test_run_ctx_prepare_cql_runs_once(self, tmp_path, mock_options):
        """Second call to run_ctx skips prepare_cql if already executed."""
        cfg = {"prepare_cql": ["CREATE KEYSPACE ks"]}
        test = self._make_test(tmp_path, mock_options, cfg=cfg)
        cluster = self._mock_cluster()
        cluster.prepare_cql_executed = True  # simulate prior execution
        pool = self._mock_pool(cluster)
        test.suite.clusters = pool
        cc = cluster.running[0].control_connection

        async with test.run_ctx():
            pass

        cc.execute.assert_not_called()
