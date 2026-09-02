#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#
"""Deliberately failing scenarios for the per-test cluster lifecycle.

Validation branch only, never to be merged.  Run with

    ./test.py --mode=<mode> --markers lifecycle_check [--save-log-on-success]

and inspect testlog/<mode>/: every test here fails on purpose and must leave
testlog/<mode>/failed_test/<test>.<mode>.<run>/ with the servers' logs (look
for the "lifecycle-check" line each test writes into scylla.log), a
stacktrace.txt, and found_errors.txt where noted; no testlog/<mode>/scylla-*
directory may survive a *passed* test unless --save-log-on-success is set.
"""

import asyncio
from urllib.parse import quote

import pytest

from test.pylib.scylla_cluster_manager import ScyllaClusterManager

pytestmark = pytest.mark.lifecycle_check


async def scylla_log(manager: ScyllaClusterManager, ip: str, message: str, level: str = "info") -> None:
    """Write a line into the server's own log through the REST API."""
    await manager.api.client.post(f"/system/log?message={quote(message)}&level={level}", host=ip)


async def test_call_failure_keeps_evidence(manager: ScyllaClusterManager) -> None:
    """Fails in the call phase.  Expect failed_test/<test>/ with scylla-*.log
    containing 'lifecycle-check: call failure' and a stacktrace.txt; the
    cluster's data dir is gone."""
    server = await manager.server_add()
    await scylla_log(manager, server.ip_addr, "lifecycle-check: call failure")
    pytest.fail("deliberate call-phase failure")


@pytest.mark.check_nodes_for_errors
async def test_found_errors_fail_the_test(manager: ScyllaClusterManager) -> None:
    """Passes, then fails in teardown: an ERROR line in the server log.  Expect
    failed_test/<test>/found_errors.txt naming it, plus the server log."""
    server = await manager.server_add()
    await scylla_log(manager, server.ip_addr, "lifecycle-check: deliberate error line", level="error")


@pytest.mark.slow
async def test_leaked_task_fails_the_test(manager: ScyllaClusterManager) -> None:
    """Passes, then fails in teardown after the 120s drain: a graceful stop
    left hanging on a paused server.  Expect failed_test/<test>/ with the
    server log (gathered right before the teardown failure)."""
    server = await manager.server_add()
    await scylla_log(manager, server.ip_addr, "lifecycle-check: leaked task")
    await manager.server_pause(server.server_id)
    # SIGTERM is queued behind SIGSTOP, so this never completes on its own;
    # the caller side is dropped with the test's loop, the operation stays.
    asyncio.ensure_future(manager.server_stop_gracefully(server.server_id, timeout=600))


@pytest.fixture
async def broken_setup(manager: ScyllaClusterManager) -> None:
    server = await manager.server_add()
    await scylla_log(manager, server.ip_addr, "lifecycle-check: setup failure")
    raise RuntimeError("deliberate setup failure")


async def test_setup_failure_keeps_evidence(broken_setup) -> None:
    """Errors in setup, after the cluster has a server.  Expect
    failed_test/<test>/ with the server log copied by makereport (no manager
    teardown runs for this one) and a stacktrace.txt."""
