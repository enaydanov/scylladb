#
# Copyright (C) 2025-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

from __future__ import annotations

from test.pylib.suite.python import  PythonTest, PythonTestSuite


class TopologyTestSuite(PythonTestSuite):
    """A collection of Python pytests against Scylla instances dealing with topology changes.
       Instead of using a single Scylla cluster directly, there is a cluster manager handling
       the lifecycle of clusters and bringing up new ones as needed. The cluster health checks
       are done per test case.
    """

    async def add_test(self, shortname: str, casename: str) -> None:
        """Add test to suite"""
        test = TopologyTest(self.next_id((shortname, 'topology', self.mode)), shortname, casename, self)
        self.tests.append(test)

class TopologyTest(PythonTest):
    """Run a pytest collection of cases against Scylla clusters handling topology changes"""
    status: bool

    def __init__(self, test_no: int, shortname: str, casename: str, suite) -> None:
        super().__init__(test_no, shortname, casename, suite)


