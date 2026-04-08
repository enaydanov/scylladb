# Dead Code Audit: Legacy Pipeline

This document inventories all code that is reachable **only** from `test.py`'s
legacy execution pipeline and became dead code once the legacy pipeline was
removed.

## Critical Finding: The Legacy Pipeline Already Executes Zero Tests

No `suite.yaml` files exist in the repository.  All test suites have been
migrated to `test_config.yaml`.  The legacy discovery function `find_tests()`
in `test.py:357-363` globs for `SUITE_CONFIG_FILENAME` (`"suite.yaml"`) and
finds nothing.  As a result:

- `TestSuite.all_tests()` returns an empty iterator.
- The async loop in `run_all_tests()` at `test.py:508-520` iterates zero times.
- The entire `TestSuite.run()` -> `Test.run()` -> `run_test()` chain never
  executes.
- `process_coverage()` iterates `TestSuite.all_tests()` and processes nothing.

**`test.py` is already just a wrapper around `run_pytest()` in practice.**

> Note: `get_testpy_test()` in `base.py:597-609` still works because it tries
> `suite.yaml` first, catches `FileNotFoundError`, then falls back to
> `test_config.yaml`.  This path is used by conftest fixtures via the pytest
> runner, not by the legacy pipeline.

---

## Methodology

Every method, function, class, and global variable was traced through two
execution paths:

1. **Pytest path**: `runner.py` plugin + conftest fixtures.  Uses the suite
   framework for configuration, lifecycle, and cluster management.  Never calls
   `TestSuite.run()` or `Test.run()`.

2. **Legacy path**: `test.py` -> `find_tests()` -> `TestSuite.all_tests()` ->
   `TestSuite.run()` -> `Test.run()` -> `run_test()`.

Each symbol was classified as:

- **LEGACY-ONLY**: Only reachable from test.py's legacy pipeline.
- **SHARED**: Used by both pytest and legacy paths.
- **INTERNAL-LEGACY**: Internal helper whose only callers are legacy-only.
- **INTERNAL-SHARED**: Internal helper whose callers include shared code.
- **ALREADY-DEAD**: No callers at all, even in the legacy path.
- **PYTEST-ONLY**: Only used by the pytest pipeline.

---

## 1. `test/pylib/suite/base.py`

### Module-Level Symbols

| Symbol | Category | Justification |
|--------|----------|---------------|
| `SUITE_CONFIG_FILENAME` | SHARED | Imported by `runner.py:33` and `test.py:45`; but since no `suite.yaml` exists, every reference is a dead lookup that falls through |
| `TEST_CONFIG_FILENAME` | SHARED | Used in `find_suite_config()` which is called by `get_testpy_test()`, used by both paths |
| `PYTEST_TESTS_LOGS_FOLDER` | SHARED | Imported by `runner.py:34` and used in `prepare_dirs()` |
| `output_is_a_tty` | LEGACY-ONLY | Only imported by `test.py:49`, used in `TabularConsoleOutput` |
| `create_formatter()` | SHARED | Used by `palette` class, imported by `test/pylib/cql_repl.py:26` (pytest) and `test.py` |
| `palette` class | SHARED | Imported by `test.py:51` and `test/pylib/cql_repl.py:26` |
| `toxiproxy_id_gen` | ALREADY-DEAD | Unused global — no references anywhere |

### `TestSuite` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `suites` (class dict) | SHARED | Used by `opt_create()` and `all_tests()` |
| `artifacts` (class attr) | SHARED | Set by `init_testsuite_globals()`; used by `runner.py:284-285` and `test.py:507,530` |
| `hosts` (class attr) | SHARED | Set by `init_testsuite_globals()`; used by `runner.py:207` and `test.py:507` |
| `FLAKY_RETRIES` | LEGACY-ONLY | Only used in `TestSuite.run()` |
| `_next_id` | SHARED | Used by `next_id()` and `test_count()` |
| `__init__()` | SHARED | Called by all subclass constructors via `opt_create()` |
| `next_id()` | SHARED | Called from subclass `add_test()` methods |
| `test_count()` | LEGACY-ONLY | Only called from `test.py:470,542,597` |
| `load_cfg()` | SHARED | Called by `opt_create()` |
| `opt_create()` | SHARED | Called from `runner.py` via `get_testpy_test()` and from `test.py:362` |
| `all_tests()` | LEGACY-ONLY | Only called from `test.py:508,582,625,626,661` |
| `pattern` (abstract property) | LEGACY-ONLY | Only consumed by `build_test_list()` (legacy); required by ABC contract |
| `add_test()` (abstract) | SHARED | Called from `get_testpy_test()` (both) and `add_test_list()` (legacy) |
| `run()` | LEGACY-ONLY | Only called from `test.py:520` |
| `junit_tests()` | ALREADY-DEAD | No callers found anywhere |
| `boost_tests()` | ALREADY-DEAD | No callers found anywhere |
| `build_test_list()` | LEGACY-ONLY | Only called by `add_test_list()` |
| `add_test_list()` | LEGACY-ONLY | Only called from `test.py:363` |
| `need_coverage()` | SHARED | Called in `__init__()` for env setup (shared); standalone call only from `test.py:661` |

### `Test` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `__init__()` | SHARED | Called by all test subclass constructors |
| `reset()` | LEGACY-ONLY | Only called from `TestSuite.run()` |
| `failed` (property) | LEGACY-ONLY | Only read from `test.py:625` |
| `did_not_run` (property) | LEGACY-ONLY | Only read from `test.py:626` |
| `run()` (abstract) | LEGACY-ONLY | Only called from `TestSuite.run()` |
| `print_summary()` (abstract) | LEGACY-ONLY | Only called from `test.py:459,548` |
| `setup()` | ALREADY-DEAD | No callers found anywhere |
| `check_log()` | INTERNAL-LEGACY | Only called from `run_test()` |

### Module-Level Functions

| Function | Category | Justification |
|----------|----------|---------------|
| `init_testsuite_globals()` | SHARED | Called from `runner.py:206` and `test.py:587` |
| `read_log()` | INTERNAL-LEGACY | Only called from `print_summary()` methods (legacy-only) |
| `run_test()` | LEGACY-ONLY | Only called from `Test.run()` implementations; 112 lines, the largest single block of dead code in the suite framework |
| `prepare_dir()` | INTERNAL-SHARED | Called by `prepare_dirs()` |
| `prepare_environment()` | SHARED | Called from `runner.py:214` and `test.py:588` |
| `prepare_dirs()` | INTERNAL-SHARED | Called by `prepare_environment()` |
| `start_3rd_party_services()` | INTERNAL-SHARED | Called by `prepare_environment()` |
| `find_suite_config()` | INTERNAL-SHARED | Called by `get_testpy_test()` |
| `get_testpy_test()` | SHARED | Called from `runner.py:170`, conftest fixtures, and indirectly from legacy discovery |

---

## 2. `test/pylib/suite/python.py`

### `PythonTestSuite` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `__init__()` | SHARED | Called via `opt_create()` |
| `get_cluster_factory()` | SHARED | Called in `__init__()`; cluster pool used by `PythonTest.run_ctx()` |
| `pattern` (property) | SHARED | Required abstract property (consumed only by legacy `build_test_list()`) |
| `add_test()` | SHARED | Called from `get_testpy_test()` |
| `run()` | LEGACY-ONLY | Override of `TestSuite.run()`, only called from `test.py:520` |

### `PythonTest` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `__init__()` | SHARED | Called from `PythonTestSuite.add_test()` |
| `_prepare_pytest_params()` | LEGACY-ONLY | Called only from `run_ctx()` which will be refactored to build args directly, and `run()` (legacy, already removed) |
| `reset()` | LEGACY-ONLY | Only called from `TestSuite.run()` chain |
| `print_summary()` | LEGACY-ONLY | Only called from `test.py:548` |
| `run_ctx()` | SHARED | Called from `test/cqlpy/conftest.py:45` and `test/scylla_gdb/conftest.py:18` |
| `run()` | LEGACY-ONLY | Only called from `TestSuite.run()` chain |

### Module-Level Functions

| Function | Category | Justification |
|----------|----------|---------------|
| `add_host_option()` | SHARED | Called from 5+ conftest files |
| `add_cql_connection_options()` | SHARED | Called from 5+ conftest files |
| `add_s3_options()` | SHARED | Called from 2+ conftest files |

---

## 3. `test/pylib/suite/cql_approval.py`

### `CQLApprovalTestSuite` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `test_file_ext` | LEGACY-ONLY | Only needed to distinguish `.cql` files from `.py`; with `CQLApprovalTestSuite` removed, `test_file_ext` is no longer needed on `PythonTestSuite` either |
| `pattern` (property) | SHARED | Required abstract property override |

`CQLApprovalTestSuite` is an empty subclass that only overrides
`test_file_ext`.  After Phase 1 removes the legacy pipeline, nothing in
the pytest path requires the `Approval` → `CQLApproval` class dispatch.
The entire class and file are planned for removal in Phase 4.

---

## 4. `test/pylib/suite/topology.py`

### `TopologyTestSuite` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `add_test()` | LEGACY-ONLY | After Phase 1 removes `run()`, `add_test()` is a pass-through to `PythonTestSuite.add_test()` with an extra `casename` arg that is always `None` |
| `junit_tests()` | LEGACY-ONLY | Overrides base method that has no callers |

### `TopologyTest` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `__init__()` | LEGACY-ONLY | After Phase 1 removes `run()`, `__init__()` is a pass-through to `PythonTest.__init__()` |
| `run()` | LEGACY-ONLY | Only called from `TestSuite.run()` chain |

Both classes are empty pass-throughs after Phase 1 removes `run()` and
`junit_tests()`.  They add nothing over `PythonTestSuite`/`PythonTest`.
The entire file is planned for removal in Phase 4, with
`test/cluster/test_config.yaml` changing from `type: Topology` to
`type: Python`.

---

## 5. `test/pylib/suite/tool.py`

### `ToolTestSuite` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `__init__()` | SHARED | Called via `opt_create()` |
| `pattern` (property) | SHARED | Required abstract property override |
| `add_test()` | SHARED | Called from `get_testpy_test()` |

### `ToolTest` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `__init__()` | SHARED | Called from `ToolTestSuite.add_test()` |
| `_prepare_pytest_params()` | INTERNAL-LEGACY | Only called from `ToolTest.run()` (no `run_ctx()` equivalent) |
| `print_summary()` | LEGACY-ONLY | Only called from `test.py:548` |
| `run()` | LEGACY-ONLY | Only called from `TestSuite.run()` chain |

---

## 6. `test/pylib/suite/run.py`

### `RunTestSuite` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `__init__()` | SHARED | Called via `opt_create()` |
| `add_test()` | SHARED | Called from `get_testpy_test()` |
| `pattern` (property) | SHARED | Required abstract property override |

### `RunTest` Class

| Method / Attribute | Category | Justification |
|--------------------|----------|---------------|
| `__init__()` | SHARED | Called from `RunTestSuite.add_test()` |
| `print_summary()` | LEGACY-ONLY | Only called from `test.py:548` |
| `run()` | LEGACY-ONLY | Only called from `TestSuite.run()` chain |

---

## 7. `test.py`

### Classes

| Item | Lines | Category | Justification |
|------|-------|----------|---------------|
| `ThreadsCalculator` | 77-119 | SHARED | Computes `-j`; value used by both `run_pytest()` and legacy loop |
| `TabularConsoleOutput` | 123-173 | LEGACY-ONLY | Progress printing for legacy pipeline; pytest has its own reporting |

### Functions

| Function | Lines | Category | Justification |
|----------|-------|----------|---------------|
| `setup_signal_handlers()` | 176-188 | LEGACY-ONLY | Sets asyncio signal handlers for legacy loop; pytest handles its own signals |
| `parse_cmd_line()` | 191-354 | SHARED | Most args consumed by both; `--parallel-cases`, `--manual-execution`, `--cluster-pool-size` are legacy-only |
| `find_tests()` | 357-363 | LEGACY-ONLY | Discovers zero tests (no `suite.yaml` exists) |
| `run_pytest()` | 365-463 | PYTEST-ONLY | Core pytest pipeline |
| `run_all_tests()` | 467-533 | MIXED | ~60 lines of async scaffolding are LEGACY-ONLY; only the `run_pytest()` call on lines 503-505 is PYTEST-ONLY |
| `print_summary()` | 536-560 | SHARED | ~60% of body is LEGACY-ONLY (`failed_tests`, `cancelled_tests`, `TestSuite.test_count()`) |
| `open_log()` | 563-572 | SHARED | Log file creation |
| `main()` | 575-638 | MIXED | `find_tests`, `init_testsuite_globals`, `prepare_environment`, `signaled`, `resource_watcher`, `setup_signal_handlers`, `process_coverage` are LEGACY-ONLY |
| `process_coverage()` | 641-888 | LEGACY-ONLY | 248 lines; iterates `TestSuite.all_tests()` (empty); already non-functional |

### Summary for test.py

| Category | Approximate Lines | Percentage |
|----------|------------------|------------|
| LEGACY-ONLY | ~440 | 49% |
| SHARED | ~260 | 29% |
| PYTEST-ONLY | ~105 | 12% |
| Mixed (contains both) | ~100 | 11% |

### Legacy-Only Imports in test.py

| Import | Justification |
|--------|---------------|
| `signal` | Only used by `setup_signal_handlers()` |
| `humanfriendly` | Only used by `process_coverage()` |
| `treelib` | Only used by `process_coverage()` |
| `test.pylib.coverage_utils` | Only used by `process_coverage()` |
| `test.pylib.resource_gather.run_resource_watcher` | Only used in `main()` for resource monitoring during legacy loop |
| `test.pylib.util.LogPrefixAdapter` | Only used by `process_coverage()` |
| `output_is_a_tty` (from suite.base) | Only used by `TabularConsoleOutput` |
| `init_testsuite_globals` (from suite.base) | Legacy infrastructure setup |
| `prepare_environment` (from suite.base) | Legacy infrastructure setup (pytest has its own via `--test-py-init`) |
| `SUITE_CONFIG_FILENAME` (from suite.base) | Only used by `find_tests()` |
| `glob` | **Completely unused** — dead import even in legacy |
| `itertools` | Used by `process_coverage()` (`itertools.product` at line 650) — not dead |

---

## 8. `test/pylib/runner.py`

| Code Block | Lines | Category | Justification |
|------------|-------|----------|---------------|
| `--test-py-init` option | 86-87 | LEGACY-ONLY | Only passed by `test.py:398` |
| `--scylla-log-filename` option | 111-113 | LEGACY-ONLY | Only meaningful under test.py; planned for removal in Phase 4 |
| `print_scylla_log_filename` fixture | 121-132 | LEGACY-ONLY | Depends on `--scylla-log-filename`; planned for removal in Phase 4 |
| `testpy_test_fixture_scope()` | 135-146 | SHARED | Returns `"module"` when `--test-py-init` is set, `"session"` otherwise; condition will change from `--test-py-init` to `TEST_RUNNER` (cannot be replaced with literal because runpy needs `"session"`) |
| `testpy_test` fixture | 165-171 | SHARED (simplifiable) | Returns `Test` under test.py, `None` otherwise; would always return `None` |
| `scylla_binary` fixture | 173-175 | LEGACY-ONLY | Accesses `testpy_test.suite.scylla_exe`; `testpy_test` would always be `None` |
| `pytest_sessionstart` init block | 194-220 | LEGACY-ONLY | Gated by `--test-py-init` |
| `pytest_sessionfinish` cleanup | 267-291 | LEGACY-ONLY | Gated by `--test-py-init` |
| `pytest_configure` logging | 298-321 | LEGACY-ONLY | Gated by `--test-py-init` |
| `pytest_runtest_makereport` log capture | 382-392 | LEGACY-ONLY | Gated by `--test-py-init` |
| `SUITE_CONFIG_FILENAME` check | 422 | LEGACY-ONLY | Always fails (no `suite.yaml`); falls through to `TEST_CONFIG_FILENAME` |

**Total**: ~120 lines (25%) are test.py-specific.

---

## 9. Conftest Files

### `test/cqlpy/conftest.py`

| Code | Lines | Category |
|------|-------|----------|
| `host` fixture `else` branch (enters `testpy_test.run_ctx()`) | 44-46 | LEGACY-ONLY |
| All `scope=testpy_test_fixture_scope` references (~10 fixtures) | various | Scope function kept (condition changes in Phase 2) |

### `test/cluster/conftest.py`

| Code | Lines | Category |
|------|-------|----------|
| `manager_api_sock_path` fixture `else` branch (starts `ScyllaClusterManager`) | 189-213 | LEGACY-ONLY |
| `manager` fixture — `get_testpy_test()` call for log path computation | 248-277 | LEGACY-ONLY |

This file has the **deepest coupling**: it instantiates a full `TestSuite` +
`Test` solely for path string resolution.

### `test/cql/conftest.py`

| Code | Lines | Category |
|------|-------|----------|
| `output_path` fixture (calls `get_testpy_test()` for reject file path) | 78-82 | LEGACY-ONLY |
| `scope=testpy_test_fixture_scope` on `keyspace` | 64 | Scope function kept (condition changes in Phase 2) |

### `test/nodetool/conftest.py`

| Code | Lines | Category |
|------|-------|----------|
| `server_address` fixture `testpy_test is not None` branch (host leasing) | 58-59, 64-65 | LEGACY-ONLY |

### `test/scylla_gdb/conftest.py`

| Code | Lines | Category |
|------|-------|----------|
| `scylla_server` fixture (entire body) | 15-19 | LEGACY-ONLY |

**This fixture has NO fallback**: it accesses `testpy_test.run_ctx()` without
a `None` guard.  It will crash with `AttributeError` when run without test.py.
This is the only conftest that is **broken** in bare pytest mode.

### `test/alternator/conftest.py`, `test/rest_api/conftest.py`

Only use `testpy_test_fixture_scope` for scoping.  No `testpy_test` access in
fixture bodies.  Impact is limited to scope simplification.

### `test/broadcast_tables/conftest.py`, `test/cluster/object_store/conftest.py`

Only import utility functions (`add_host_option`, `add_cql_connection_options`,
`add_s3_options`).  No test.py-specific code.

---

## 10. Aggregate Summary

### By Category

| Category | Count | Notable Items |
|----------|-------|---------------|
| LEGACY-ONLY | 30 (suite/) + ~440 lines (test.py) + ~120 lines (runner.py) + ~80 lines (conftest files) | `run_test()`, `process_coverage()`, `TabularConsoleOutput`, `TestSuite.run()`, all `Test.run()` implementations |
| ALREADY-DEAD | 4 symbols | `boost_tests()`, `junit_tests()` (base), `Test.setup()`, `toxiproxy_id_gen` |
| SHARED | ~32 (suite/) + ~260 lines (test.py) | `opt_create()`, `get_testpy_test()`, `prepare_environment()`, `add_test()`, `run_ctx()` |

### Largest Dead Code Blocks

| Block | File | Lines | Size |
|-------|------|-------|------|
| `process_coverage()` | `test.py` | 641-888 | 248 lines |
| `run_test()` | `base.py` | 397-508 | 112 lines |
| `run_all_tests()` async scaffolding | `test.py` | 467-533 | ~60 lines |
| `TabularConsoleOutput` | `test.py` | 123-173 | 51 lines |
| `manager_api_sock_path` else branch | `cluster/conftest.py` | 189-213 | ~25 lines |

### Fixture Scope: `testpy_test_fixture_scope`

~40+ fixtures across 7 conftest files use `testpy_test_fixture_scope` as their
scope parameter.  The function's condition needs to change from `--test-py-init`
to `TEST_RUNNER`:

- `TEST_RUNNER == "runpy"` -> `"session"` (run.py scripts: single Scylla instance)
- `TEST_RUNNER == "pytest"` -> `"module"` (test.py and bare pytest: one Test per module)

The function cannot be replaced with a literal because both scopes are needed.
No changes to conftest files are required -- only the function's internal
condition changes while its call sites remain the same.

### Dead Import: `SUITE_CONFIG_FILENAME`

The constant `SUITE_CONFIG_FILENAME = "suite.yaml"` is referenced in three
code locations:
1. `test.py:359` — `find_tests()` globs for it, finds nothing.
2. `runner.py:422` — `TestSuiteConfig.from_pytest_node` checks for it, always
   fails, falls through to `TEST_CONFIG_FILENAME`.
3. `base.py:600` — `get_testpy_test()` tries it first, catches
   `FileNotFoundError`, falls back to `TEST_CONFIG_FILENAME`.

Since no `suite.yaml` files exist, this constant and all code paths that
reference it are functionally dead.  Every lookup is a wasted filesystem check
that always falls through to the `test_config.yaml` path.
