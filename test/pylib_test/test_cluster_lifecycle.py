#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#

"""Tests for the guarantees of the cluster lifecycle that only show when
something has already gone wrong, and that the default test run never
exercises:

- the after-cluster cleanups of test/cluster's scylla_cluster_teardowns run
  once the cluster is recycled (SCYLLADB-2471);
- a cluster that was never recycled, or never uninstalled, is swept up at
  session finish;
- a recycled cluster's server logs outlive the report of the phase that
  recycled it, so a failure in a module's last teardown still gets them.
"""

import asyncio
import pathlib
import textwrap
from functools import partial
from types import SimpleNamespace

import pytest

from test.pylib import runner
from test.pylib.runner import CLUSTER_KEY, recycle_leftover_clusters

pytest_plugins = ["pytester"]


def test_cluster_teardowns_run_after_the_cluster_is_recycled(pytester: pytest.Pytester) -> None:
    """The real scylla_cluster/scylla_cluster_teardowns fixtures, with the
    cluster factory stubbed: the cleanups must observe a recycled cluster,
    and fire in LIFO order."""
    pytester.makeconftest(textwrap.dedent("""\
        from contextlib import asynccontextmanager
        import pytest
        # Registers the two fixtures under test with this conftest.
        from test.cluster.conftest import scylla_cluster, scylla_cluster_teardowns

        class StubCluster:
            recycled = False

            async def recycle(self):
                self.recycled = True

        @pytest.fixture
        def testpy_cluster_factory():
            @asynccontextmanager
            async def lease(node, test_name):
                cluster = StubCluster()
                try:
                    yield cluster
                finally:
                    await cluster.recycle()
            return lease

        @pytest.fixture
        def testpy_test_name():
            return "stub::test"
        """))
    pytester.makeini(
        "[pytest]\n"
        "addopts = -p no:sugar -p no:xdist\n"
        "asyncio_mode = auto\n"
        "asyncio_default_fixture_loop_scope = session\n"
    )
    pytester.makepyfile(textwrap.dedent("""\
        import pathlib

        async def test_it(scylla_cluster, scylla_cluster_teardowns):
            log = pathlib.Path("teardowns.log")

            def record(name):
                def cleanup():
                    with log.open("a") as f:
                        f.write(f"{name} recycled={scylla_cluster.recycled}\\n")
                return cleanup

            scylla_cluster_teardowns.append(record("first"))
            scylla_cluster_teardowns.append(record("second"))
        """))
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert (pytester.path / "teardowns.log").read_text().splitlines() == [
        "second recycled=True",
        "first recycled=True",
    ]


class StubCluster:
    """Records the sweep's calls; optionally fails stop() the way a wait on
    a foreign loop does."""

    def __init__(self, fail_stop: bool = False, is_recycled: bool = False) -> None:
        self.fail_stop = fail_stop
        self.is_recycled = is_recycled
        self.stops = 0
        self.recycles = 0
        self.uninstalls = 0

    async def stop(self) -> None:
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("attached to a different loop")

    async def recycle(self) -> None:
        self.recycles += 1

    def uninstall(self) -> None:
        self.uninstalls += 1


def test_leftover_clusters_are_recycled_at_session_finish(pytester: pytest.Pytester) -> None:
    """Clusters still stashed on a test item or its module get stopped,
    recycled and uninstalled exactly once; a stop() failure does not prevent the recycle;
    a recycled one is only uninstalled, and not counted; a None entry is left alone."""
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = session\n")
    item_a, item_b, item_c = pytester.getitems(textwrap.dedent("""\
        def test_a(): pass
        def test_b(): pass
        def test_c(): pass
        """))
    per_test = StubCluster()
    shared = StubCluster(fail_stop=True)
    recycled = StubCluster(is_recycled=True)
    item_a.stash[CLUSTER_KEY] = per_test
    item_a.parent.stash[CLUSTER_KEY] = shared   # the module node, shared by all items
    item_b.stash[CLUSTER_KEY] = None            # recycled and uninstalled normally: nothing to do
    item_c.stash[CLUSTER_KEY] = recycled        # recycled with no report after it

    swept = asyncio.run(recycle_leftover_clusters(SimpleNamespace(items=[item_a, item_b, item_c])))

    assert swept == 2

    assert (per_test.stops, per_test.recycles, per_test.uninstalls) == (1, 1, 1)
    assert (shared.stops, shared.recycles, shared.uninstalls) == (1, 1, 1)
    assert (recycled.stops, recycled.recycles, recycled.uninstalls) == (0, 0, 1)


# Registers the real module-scoped scylla_cluster fixture, its cluster factory
# and the report hook, with just enough of test.py's options, stash entries
# and fixtures around them to run without a suite.
HARNESS_CONFTEST = textwrap.dedent("""\
    import logging
    import pathlib
    from types import SimpleNamespace

    import pytest

    from test.pylib.runner import (BUILD_MODE, TEST_SUITE, pytest_runtest_makereport,
                                   scylla_cluster, testpy_cluster_factory)

    def pytest_addoption(parser):
        parser.addoption("--tmpdir")
        parser.addoption("--save-log-on-success", action="store_true", dest="save_log_on_success")
        parser.addoption("--coverage", action="store_true")
        parser.addoption("--extra-scylla-cmdline-options", default="")

    def pytest_collection_modifyitems(items):
        for item in items:
            item.stash[BUILD_MODE] = "dev"
            item.parent.stash[TEST_SUITE] = SimpleNamespace(cfg={})

    @pytest.fixture(scope="module")
    def build_mode():
        return "dev"

    @pytest.fixture(scope="module")
    def suite_log_dir(request):
        return pathlib.Path(request.config.getoption("--tmpdir"), "dev")

    @pytest.fixture(scope="module")
    def scylla_binary():
        return "scylla"

    @pytest.fixture(scope="module")
    def testpy_uname(request):
        return request.module.__name__

    @pytest.fixture(scope="module")
    def testpy_logger(testpy_uname):
        return logging.getLogger(testpy_uname)
    """)


class LogWritingCluster:
    """Stands in for ScyllaCluster: one server log named after the module, and a record of the runner's calls."""

    def __init__(self, events: list[str], logger, vardir: pathlib.Path, **kwargs) -> None:
        self.events = events
        self.name = logger.name
        self.log = vardir / f"{self.name}.log"
        self.servers = {}
        self.is_recycled = False

    async def add_server(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("server log\n")
        self.servers[1] = SimpleNamespace(log_filename=self.log)
        self.events.append(f"create {self.name}")

    def take_log_savepoint(self) -> None:
        pass

    async def recycle(self) -> None:
        self.is_recycled = True

    def uninstall(self) -> None:
        self.log.unlink()
        self.events.append(f"uninstall {self.name}")


def run_with_stub_clusters(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
                           **modules: str) -> tuple[pytest.RunResult, list[str], pathlib.Path]:
    """Run the modules against the harness; return the result, the cluster events and the failed_test dir."""
    events = []
    monkeypatch.setattr(runner, "ScyllaCluster", partial(LogWritingCluster, events))
    pytester.makeconftest(HARNESS_CONFTEST)
    pytester.makeini(
        "[pytest]\n"
        "addopts = -p no:sugar -p no:xdist\n"
        "asyncio_mode = auto\n"
        "asyncio_default_fixture_loop_scope = session\n"
    )
    pytester.makepyfile(**modules)
    tmpdir = pytester.path / "testlog"
    (tmpdir / "pytest_tests_logs").mkdir(parents=True)
    return pytester.runpytest(f"--tmpdir={tmpdir}"), events, tmpdir / "dev" / "failed_test"


def test_failed_teardown_keeps_the_logs_of_a_recycled_cluster(pytester: pytest.Pytester,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """A module whose last teardown fails after its cluster was recycled gets
    the server log in failed_test/; a passing module's cluster is uninstalled
    before the next module's cluster is created."""
    result, events, failed_test_dir = run_with_stub_clusters(
        pytester, monkeypatch,
        test_a="def test_a(scylla_cluster): pass\n",
        test_b=textwrap.dedent("""\
            import pytest

            @pytest.fixture(scope="module")
            def failing_teardown(scylla_cluster):
                yield
                raise RuntimeError("deliberate teardown failure")

            def test_b(failing_teardown): pass
            """),
    )

    result.assert_outcomes(passed=2, errors=1)
    assert sorted(p.name for p in failed_test_dir.iterdir()) == ["test_b"]
    assert (failed_test_dir / "test_b" / "test_b.log").read_text() == "server log\n"
    assert "deliberate teardown failure" in (failed_test_dir / "test_b" / "stacktrace.txt").read_text()
    assert events == ["create test_a", "uninstall test_a", "create test_b", "uninstall test_b"]
