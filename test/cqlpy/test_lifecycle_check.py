#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#
"""Deliberately failing cqlpy test: validation branch only, never to be merged.

The module-scoped scylla_cluster serves this whole module; expect
testlog/<mode>/failed_test/<test>/ with the module cluster's server log
(containing this test's cql_test_connection markers) and a stacktrace.txt.
"""

import pytest

pytestmark = pytest.mark.lifecycle_check


def test_deliberate_failure(cql):
    assert cql.execute("SELECT now() FROM system.local").one() is not None
    pytest.fail("deliberate cqlpy failure")
