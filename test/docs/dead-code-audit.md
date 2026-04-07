# Dead Code Audit: Legacy Pipeline

This document inventories all code that is reachable **only** from `test.py`'s
legacy execution pipeline and became dead code once the legacy pipeline was
removed.

> **Phase 1-4 status:** The legacy execution pipeline was removed in Phase 1.
> Dead code from the suite framework was cleaned up in Phase 4.  Items marked
> ✅ REMOVED below have been deleted.  Items marked 🔄 REMAINING are still in
> the codebase because they are shared with the pytest path.

## Critical Finding: The Legacy Pipeline Already Executes Zero Tests

No `suite.yaml` files exist in the repository.  All test suites have been
migrated to `test_config.yaml`.  The legacy discovery function `find_tests()`
(now removed) in `test.py` globbed for `SUITE_CONFIG_FILENAME`
(`"suite.yaml"`) and found nothing.  As a result:

- `TestSuite.all_tests()` returned an empty iterator.
- The async loop in `run_all_tests()` iterated zero times.
- The entire `TestSuite.run()` → `Test.run()` → `run_test()` chain never
  executed.
- `process_coverage()` iterated `TestSuite.all_tests()` and processed nothing.

**`test.py` is already just a wrapper around `run_pytest()` in practice.**

> Note: `get_testpy_test()` in `suite.py` now uses `TEST_CONFIG_FILENAME`
> directly (the `suite.yaml` fallback was removed in Phase 1).  This path is
> used by conftest fixtures via the pytest runner, not by the legacy pipeline.

---

## Methodology

Every method, function, class, and global variable was traced through two
execution paths:

1. **Pytest path**: `runner.py` plugin + conftest fixtures.  Uses the suite
   framework for configuration, lifecycle, and cluster management.  Never calls
   `TestSuite.run()` or `Test.run()`.

2. **Legacy path**: `test.py` → `find_tests()` → `TestSuite.all_tests()` →
   `TestSuite.run()` → `Test.run()` → `run_test()`.

Each symbol was classified as:

- **LEGACY-ONLY**: Only reachable from test.py's legacy pipeline.
- **SHARED**: Used by both pytest and legacy paths.
- **INTERNAL-LEGACY**: Internal helper whose only callers are legacy-only.
- **INTERNAL-SHARED**: Internal helper whose callers include shared code.
- **ALREADY-DEAD**: No callers at all, even in the legacy path.
- **PYTEST-ONLY**: Only used by the pytest pipeline.

---

## 1. `test/pylib/suite.py` (formerly `suite/base.py`)

### Module-Level Symbols

| Symbol | Category | Status | Justification |
|--------|----------|--------|---------------|
| `SUITE_CONFIG_FILENAME` | SHARED | ✅ REMOVED | Was imported by `runner.py` and `test.py`; since no `suite.yaml` exists, every reference was a dead lookup |
| `TEST_CONFIG_FILENAME` | SHARED | 🔄 REMAINING | Used in `find_suite_config()` which is called by `get_testpy_test()`, used by both paths |
| `PYTEST_TESTS_LOGS_FOLDER` | SHARED | 🔄 REMAINING | Imported by `runner.py` and used in `prepare_dirs()` |
| `output_is_a_tty` | SHARED | 🔄 REMAINING | Used by `create_formatter()` / `palette`, which are imported by `test/pylib/cql_repl.py` (pytest path) and `test.py` |
| `create_formatter()` | SHARED | 🔄 REMAINING | Used by `palette` class, imported by `test/pylib/cql_repl.py` (pytest) and `test.py` |
| `palette` class | SHARED | 🔄 REMAINING | Imported by `test.py` and `test/pylib/cql_repl.py` |
| `toxiproxy_id_gen` | ALREADY-DEAD | ✅ REMOVED | Unused global — no references anywhere |

### `TestSuite` Class

| Method / Attribute | Category | Status | Justification |
|--------------------|----------|--------|---------------|
| `suites` (class dict) | SHARED | 🔄 REMAINING | Used by `opt_create()` and `all_tests()` |
| `artifacts` (class attr) | SHARED | 🔄 REMAINING | Set by `init_testsuite_globals()`; used by `runner.py` and `test.py` |
| `hosts` (class attr) | SHARED | 🔄 REMAINING | Set by `init_testsuite_globals()`; used by `runner.py` and `test.py` |
| `FLAKY_RETRIES` | LEGACY-ONLY | ✅ REMOVED | Only used in `TestSuite.run()` |
| `_next_id` | SHARED | 🔄 REMAINING | Used by `next_id()` and `test_count()` |
| `__init__()` | SHARED | 🔄 REMAINING | Called by all subclass constructors via `opt_create()` |
| `next_id()` | SHARED | 🔄 REMAINING | Called from subclass `add_test()` methods |
| `test_count()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — zero callers remained |
| `load_cfg()` | SHARED | 🔄 REMAINING | Called by `opt_create()` |
| `opt_create()` | SHARED | 🔄 REMAINING | Called from `runner.py` via `get_testpy_test()` and from `test.py` |
| `all_tests()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — callers removed in Phase 3 |
| `pattern` (abstract property) | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — only consumed by deleted `build_test_list()` |
| `add_test()` (abstract) | SHARED | 🔄 REMAINING | Called from `get_testpy_test()` (both) and previously from `add_test_list()` (legacy, now removed) |
| `run()` | LEGACY-ONLY | ✅ REMOVED | Only called from `test.py` |
| `junit_tests()` | ALREADY-DEAD | ✅ REMOVED | No callers found anywhere |
| `boost_tests()` | ALREADY-DEAD | ✅ REMOVED | No callers found anywhere |
| `build_test_list()` | LEGACY-ONLY | ✅ REMOVED | Only called by `add_test_list()` |
| `add_test_list()` | LEGACY-ONLY | ✅ REMOVED | Only called from `test.py` |
| `need_coverage()` | SHARED | 🔄 REMAINING | Called in `__init__()` for env setup (shared); standalone call only from `test.py` |

### `Test` Class

| Method / Attribute | Category | Status | Justification |
|--------------------|----------|--------|---------------|
| `__init__()` | SHARED | 🔄 REMAINING | Called by all test subclass constructors |
| `reset()` | LEGACY-ONLY | ✅ REMOVED | Only called from `TestSuite.run()` |
| `failed` (property) | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — zero callers remained |
| `did_not_run` (property) | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — zero callers remained |
| `run()` (abstract) | LEGACY-ONLY | ✅ REMOVED | Only called from `TestSuite.run()` |
| `print_summary()` (abstract) | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — abstract constraint lifted, all subclass implementations also removed |
| `setup()` | ALREADY-DEAD | ✅ REMOVED | No callers found anywhere |
| `check_log()` | INTERNAL-LEGACY | ✅ REMOVED | Only called from `run_test()` |

### Module-Level Functions

| Function | Category | Status | Justification |
|----------|----------|--------|---------------|
| `init_testsuite_globals()` | SHARED | 🔄 REMAINING | Called from `runner.py` and `test.py` |
| `read_log()` | INTERNAL-LEGACY | ✅ REMOVED | Removed in Phase 4 — all callers (`print_summary()` methods) also removed |
| `run_test()` | LEGACY-ONLY | ✅ REMOVED | Only called from `Test.run()` implementations; 112 lines, the largest single block of dead code removed |
| `prepare_dir()` | INTERNAL-SHARED | 🔄 REMAINING | Called by `prepare_dirs()` |
| `prepare_environment()` | SHARED | 🔄 REMAINING | Called from `runner.py` and `test.py` |
| `prepare_dirs()` | INTERNAL-SHARED | 🔄 REMAINING | Called by `prepare_environment()` |
| `start_3rd_party_services()` | INTERNAL-SHARED | 🔄 REMAINING | Called by `prepare_environment()` |
| `find_suite_config()` | INTERNAL-SHARED | 🔄 REMAINING | Called by `get_testpy_test()` |
| `get_testpy_test()` | SHARED | 🔄 REMAINING | Called from `runner.py`, conftest fixtures, and indirectly from legacy discovery. Now uses only `TEST_CONFIG_FILENAME` (suite.yaml fallback removed) |

---

## 2. `test/pylib/suite.py` (formerly `suite/python.py`)

### `PythonTestSuite` Class

| Method / Attribute | Category | Status | Justification |
|--------------------|----------|--------|---------------|
| `__init__()` | SHARED | 🔄 REMAINING | Called via `opt_create()` |
| `get_cluster_factory()` | SHARED | 🔄 REMAINING | Called in `__init__()`; cluster pool used by conftest fixtures. Now in `suite.py` |
| `pattern` (property) | SHARED | 🔄 REMAINING | Required abstract property |
| `add_test()` | SHARED | 🔄 REMAINING | Called from `get_testpy_test()` |
| `run()` | LEGACY-ONLY | ✅ REMOVED | Override of `TestSuite.run()`, only called from `test.py` |

### `PythonTest` Class

| Method / Attribute | Category | Status | Justification |
|--------------------|----------|--------|---------------|
| `__init__()` | SHARED | 🔄 REMAINING | Called from `PythonTestSuite.add_test()` |
| `_prepare_pytest_params()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — only called from `run_ctx()` which now builds args directly |
| `reset()` | LEGACY-ONLY | ✅ REMOVED | Only called from `TestSuite.run()` chain |
| `print_summary()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — abstract constraint lifted |
| `run_ctx()` | SHARED | 🔄 REMAINING | Called from `test/cqlpy/conftest.py` and `test/scylla_gdb/conftest.py`; manages cluster lifecycle (lease from pool, before_test/after_test, teardown) |
| `run()` | LEGACY-ONLY | ✅ REMOVED | Only called from `TestSuite.run()` chain |

### Module-Level Functions

| Function | Category | Status | Justification |
|----------|----------|--------|---------------|
| `add_host_option()` | SHARED | ✅ MOVED | Moved to `runner.py` |
| `add_cql_connection_options()` | SHARED | ✅ MOVED | Moved to `runner.py` |
| `add_s3_options()` | SHARED | ✅ MOVED | Moved to `runner.py` |

---

## 3. `test/pylib/suite/cql_approval.py` — ✅ FILE DELETED (package flattened)

### `CQLApprovalTestSuite` Class

| Method / Attribute | Category | Status | Justification |
|--------------------|----------|--------|---------------|
| `test_file_ext` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 with the entire CQLApprovalTestSuite class |
| `pattern` (property) | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — abstract constraint lifted |

`CQLApprovalTestSuite` was an empty subclass that only overrode
`test_file_ext`.  The entire class and file were removed in Phase 4.
`test/cql/test_config.yaml` changed from `type: Approval` to `type: Python`.

---

## 4. `test/pylib/suite/topology.py` — ✅ FILE DELETED (package flattened)

### `TopologyTestSuite` Class

| Method / Attribute | Category | Status | Justification |
|--------------------|----------|--------|---------------|
| `add_test()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — empty pass-through to `PythonTestSuite.add_test()` |
| `junit_tests()` | LEGACY-ONLY | ✅ REMOVED | Overrode base method that had no callers |

### `TopologyTest` Class

| Method / Attribute | Category | Status | Justification |
|--------------------|----------|--------|---------------|
| `__init__()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 — empty pass-through to `PythonTest.__init__()` |
| `run()` | LEGACY-ONLY | ✅ REMOVED | Only called from `TestSuite.run()` chain |

Both classes were empty pass-throughs.  The entire file was removed in
Phase 4.  `test/cluster/test_config.yaml` changed from `type: Topology` to
`type: Python`.

---

## 5. `test/pylib/suite/tool.py` — ✅ FILE DELETED (package flattened)

`ToolTestSuite` and `ToolTest` had no consumers after all test directories
migrated from `type: Tool` to `type: Python`.  The `nodetool` directory was
the last to use `type: Tool`.  The file was deleted and the `__init__.py`
re-export was removed.

---

## 6. `test/pylib/suite/run.py` — ✅ FILE DELETED (package flattened)

`RunTestSuite` and `RunTest` had no consumers after all test directories
migrated from `type: Run` to `type: Python`.  Multiple directories previously
used `type: Run` (scylla_gdb, alternator, cql-pytest, rest_api, etc.).
`RunTestSuite`'s only addition over `TestSuite` was `self.scylla_exe`, which
`PythonTestSuite` already provides.  The file was deleted and the `__init__.py`
re-export was removed.

---

## 7. `test.py`

### Classes

| Item | Category | Status | Justification |
|------|----------|--------|---------------|
| `ThreadsCalculator` | SHARED | 🔄 REMAINING | Computes `-j`; value used by `run_pytest()` |
| `TabularConsoleOutput` | LEGACY-ONLY | ✅ REMOVED | Progress printing for legacy pipeline; pytest has its own reporting |

### Functions

| Function | Category | Status | Justification |
|----------|----------|--------|---------------|
| `setup_signal_handlers()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 3 — asyncio event loop no longer used |
| `parse_cmd_line()` | SHARED | 🔄 REMAINING (simplified) | Dead options removed in Phase 3: `--no-parallel-cases`, `--log-level`, `--coverage-keep-*`, `--artifacts_dir_url`, `--manual-execution`, `--skip-internet-dependent-tests` |
| `find_tests()` | LEGACY-ONLY | ✅ REMOVED | Discovered zero tests (no `suite.yaml` exists) |
| `run_pytest()` | PYTEST-ONLY | 🔄 REMAINING (simplified) | Now returns `int` (exit code) instead of `tuple[int, list]`; JUnit XML parsing removed in Phase 3 |
| `run_all_tests()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 3 — async executor wrapper no longer needed |
| `print_summary()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 3 — pytest provides its own summary |
| `open_log()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 3 — runner.py handles logging via `pytest_configure` |
| `main()` | SHARED | 🔄 REMAINING (simplified) | Phase 3: now synchronous `def` (not `async def`); calls parse_cmd_line, run_pytest, optional coverage report |
| `process_coverage()` | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 3 — 248 lines; was already non-functional (`TestSuite.all_tests()` returned nothing) |

### Legacy-Only Imports in test.py — Phase 3 Status

| Import | Status | Justification |
|--------|--------|---------------|
| `asyncio` | ✅ REMOVED | Removed in Phase 3 — `main()` is now synchronous |
| `signal` | ✅ REMOVED | Removed in Phase 3 — `setup_signal_handlers()` deleted |
| `time` | ✅ REMOVED | Removed in Phase 3 — `launch_time` and `print_summary()` deleted |
| `resource` | ✅ REMOVED | Removed in Phase 3 — `print_summary()` CPU utilization deleted |
| `xml.etree.ElementTree` | ✅ REMOVED | Removed in Phase 3 — JUnit XML parsing deleted from `run_pytest()` |
| `humanfriendly` | ✅ REMOVED | Removed in Phase 3 — `process_coverage()` deleted |
| `treelib` | ✅ REMOVED | Removed in Phase 3 — `process_coverage()` deleted |
| `itertools` | ✅ REMOVED | Removed in Phase 3 — `process_coverage()` deleted |
| `test.pylib.coverage_utils` | ✅ REMOVED | Removed in Phase 3 — `process_coverage()` deleted |
| `test.pylib.resource_gather.run_resource_watcher` | ✅ REMOVED | Removed in Phase 3 — resource watcher moved to runner.py |
| `test.pylib.util.LogPrefixAdapter` | ✅ REMOVED | Removed in Phase 3 — `process_coverage()` deleted |
| `output_is_a_tty` (from suite.base) | ✅ REMOVED (Phase 1) | Was only used by `TabularConsoleOutput` |
| `init_testsuite_globals` (from suite.base) | ✅ REMOVED | Removed in Phase 3 — runner.py handles initialization |
| `prepare_environment` (from suite.base) | ✅ REMOVED | Removed in Phase 3 — runner.py handles environment setup |
| `TestSuite` (from suite.base) | ✅ REMOVED | Removed in Phase 3 — runner.py handles artifacts/cleanup |
| `TESTPY_PREPARED_ENVIRONMENT` (from test) | ✅ REMOVED | Removed in Phase 3 — test.py no longer sets this env var |
| `SimpleNamespace` (from types) | ✅ REMOVED | Removed in Phase 3 — `run_pytest()` no longer creates SimpleNamespace objects |
| `SUITE_CONFIG_FILENAME` (from suite.base) | ✅ REMOVED (Phase 1) | Was only used by `find_tests()` |
| `glob` | ✅ REMOVED (Phase 1) | Completely unused — dead import |

---

## 8. `test/pylib/runner.py`

| Code Block | Category | Status | Justification |
|------------|----------|--------|---------------|
| `--test-py-init` option | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 2; `TESTPY_PREPARED_ENVIRONMENT` is sufficient |
| `--scylla-log-filename` option | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 |
| `print_scylla_log_filename` fixture | LEGACY-ONLY | ✅ REMOVED | Removed in Phase 4 |
| `testpy_test_fixture_scope()` | SHARED | 🔄 REMAINING | Condition changed in Phase 2 from `--test-py-init` to `TEST_RUNNER`; function kept because runpy needs `"session"` scope |
| `testpy_test` fixture | SHARED (simplifiable) | 🔄 REMAINING | Deferred to Phase 2 |
| `scylla_binary` fixture | LEGACY-ONLY | 🔄 REMAINING | Deferred to Phase 2 |
| `pytest_sessionstart` init block | LEGACY-ONLY | 🔄 REMAINING | Deferred to Phase 2 |
| `pytest_sessionfinish` cleanup | LEGACY-ONLY | 🔄 REMAINING | Deferred to Phase 2 |
| `pytest_configure` logging | LEGACY-ONLY | 🔄 REMAINING | Deferred to Phase 2 |
| `pytest_runtest_makereport` log capture | LEGACY-ONLY | 🔄 REMAINING | Deferred to Phase 2 |
| `SUITE_CONFIG_FILENAME` import and check | LEGACY-ONLY | ✅ REMOVED | Import removed; `from_pytest_node()` now checks only `TEST_CONFIG_FILENAME` |

**Total remaining**: ~120 lines (25%) are test.py-specific.  Deferred to Phase 2.

---

## 9. Conftest Files

### `test/cqlpy/conftest.py`

| Code | Category | Status |
|------|----------|--------|
| `host` fixture `else` branch (enters `testpy_test.run_ctx()`) | LEGACY-ONLY | 🔄 REMAINING (deferred to Phase 2) |
| All `scope=testpy_test_fixture_scope` references (~10 fixtures) | Scope function kept (condition changes in Phase 2) | 🔄 REMAINING |

### `test/cluster/conftest.py`

| Code | Category | Status |
|------|----------|--------|
| `manager_api_sock_path` fixture `else` branch (starts `ScyllaClusterManager`) | LEGACY-ONLY | 🔄 REMAINING (deferred to Phase 2) |
| `manager` fixture — `get_testpy_test()` call for log path computation | LEGACY-ONLY | 🔄 REMAINING (deferred to Phase 2) |

### `test/cql/conftest.py`

| Code | Category | Status |
|------|----------|--------|
| `output_path` fixture (calls `get_testpy_test()` for reject file path) | LEGACY-ONLY | 🔄 REMAINING |
| `scope=testpy_test_fixture_scope` on `keyspace` | Scope function kept (condition changes in Phase 2) | 🔄 REMAINING |

### `test/nodetool/conftest.py`

| Code | Category | Status |
|------|----------|--------|
| `server_address` fixture `testpy_test is not None` branch (host leasing) | LEGACY-ONLY | 🔄 REMAINING |

### `test/scylla_gdb/conftest.py`

| Code | Category | Status |
|------|----------|--------|
| `scylla_server` fixture (entire body) | LEGACY-ONLY | 🔄 REMAINING |

**This fixture has NO fallback**: it accesses `testpy_test.run_ctx()` without
a `None` guard.  In test.py mode, `testpy_test` returns a real `Test` instance
(module-scoped), so the fixture works.  In bare pytest mode (after Phase 2),
`testpy_test` also returns a real `Test` instance (module-scoped), so this
fixture now works there too.  In runpy mode, runner.py is not loaded as a
plugin, so this fixture is not registered.

### `test/alternator/conftest.py`, `test/rest_api/conftest.py`

Only use `testpy_test_fixture_scope` for scoping.  No `testpy_test` access in
fixture bodies.  Impact is limited to scope simplification.

### `test/broadcast_tables/conftest.py`, `test/cluster/object_store/conftest.py`

Only import utility functions (`add_host_option`, `add_cql_connection_options`,
`add_s3_options`) from `runner.py`.  No test.py-specific code.

---

## 10. Aggregate Summary

### Phase 1 Removals

| File | Items Removed | Lines Removed |
|------|---------------|---------------|
| `base.py` | `SUITE_CONFIG_FILENAME`, `FLAKY_RETRIES`, `toxiproxy_id_gen`, `TestSuite.run()`, `junit_tests()`, `boost_tests()`, `build_test_list()`, `add_test_list()`, `Test.reset()`, `Test.run()`, `Test.setup()`, `Test.check_log()`, `run_test()`, import `asyncio`, import `TOP_SRC_DIR`, import `get_resource_gather` | 241 |
| `python.py` | `PythonTestSuite.run()`, `PythonTest.reset()`, `PythonTest.run()`, imports `run_test` | 24 |
| `topology.py` | `TopologyTestSuite.junit_tests()`, `TopologyTest.run()`, imports `TYPE_CHECKING`, `get_cluster_manager`, `Test`, `run_test`, `argparse` | 37 |
| `tool.py` | ✅ FILE DELETED — `ToolTest.run()` + entire file | 43 |
| `run.py` | ✅ FILE DELETED — `RunTest.run()` + entire file | 37 |
| `runner.py` | `SUITE_CONFIG_FILENAME` import, `suite.yaml` check in `from_pytest_node()` | 3 |
| `test.py` | `TabularConsoleOutput`, `find_tests()`, `output_is_a_tty` import, `SUITE_CONFIG_FILENAME` import, `glob` import, `Any`/`TYPE_CHECKING`/`List` imports; simplified `run_all_tests()`, `print_summary()`, `main()` | 160 |
| `test_suite_base.py` | `SUITE_CONFIG_FILENAME` import, `read_log` import, `TestReadLog` class | 24 |
| `test_suite_subclasses.py` | `test_junit_tests_empty` test | 5 |
| **Total** | | **552 lines removed** |

### By Category (Remaining)

| Category | Count | Notable Items |
|----------|-------|---------------|
| LEGACY-ONLY (remaining) | ~440 lines (test.py) + ~80 lines (conftest files) | `process_coverage()`, `setup_signal_handlers()`, conftest legacy branches |
| SHARED | ~32 (suite/) + ~260 lines (test.py) | `opt_create()`, `get_testpy_test()`, `prepare_environment()`, `add_test()` |

### Largest Remaining Dead Code Blocks

| Block | File | Size | Phase |
|-------|------|------|-------|
| `process_coverage()` | `test.py` | ✅ Removed in Phase 3 | |
| `--test-py-init` guarded code | `runner.py` | ✅ Removed in Phase 2 | |
| `run_all_tests()` async scaffolding | `test.py` | ✅ Removed in Phase 3 | |
| `setup_signal_handlers()` | `test.py` | ✅ Removed in Phase 3 | |
| `print_summary()` / `open_log()` | `test.py` | ✅ Removed in Phase 3 | |
| `manager_api_sock_path` else branch | `cluster/conftest.py` | ~25 lines | Phase 4 |

### Fixture Scope: `testpy_test_fixture_scope` — Condition Changed (Phase 2)

~40+ fixtures across 7 conftest files use `testpy_test_fixture_scope` as their
scope parameter.  In Phase 2, the function's condition was changed from
`--test-py-init` to `TEST_RUNNER`:

- `TEST_RUNNER == "runpy"` → `"session"` (run.py scripts: single Scylla instance)
- `TEST_RUNNER == "pytest"` → `"module"` (test.py and bare pytest: one Test per module)

The function cannot be replaced with a literal because both scopes are needed.
No changes to conftest files were required — the function's internal behavior
changed while its call sites remained the same.

### Dead Import: `SUITE_CONFIG_FILENAME` — ✅ FULLY REMOVED

The constant `SUITE_CONFIG_FILENAME = "suite.yaml"` and all references to it
have been removed in Phase 1:
1. ~~`test.py` — `find_tests()` globbed for it~~ → removed with `find_tests()`.
2. ~~`runner.py` — `TestSuiteConfig.from_pytest_node` checked for it~~ → now
   checks only `TEST_CONFIG_FILENAME`.
3. ~~`base.py` — `get_testpy_test()` tried it first~~ → now uses
   `TEST_CONFIG_FILENAME` directly.
