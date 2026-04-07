# test.py Deprecation: Migration Roadmap

This document analyzes the feasibility of simplifying `test.py` and ensuring
all tests can also run through bare pytest.  It covers feature gaps, migration blockers,
CI/infrastructure dependencies, and a step-by-step migration plan.

See also: [Dead Code Audit](dead-code-audit.md) for the full inventory of
legacy-only code.

---

## Executive Summary

**test.py is already a thin shim around pytest.**  No `suite.yaml` files exist,
so the legacy pipeline discovers and executes zero tests.  In practice,
`test.py` does the following:

1. Parses arguments and computes optimal `-j` (via `ThreadsCalculator`).
2. Calls `find_tests()` -- discovers nothing.
3. Calls `prepare_environment()` -- starts 3rd-party services (LDAP, MinIO,
   S3Mock, S3Proxy).
4. Starts a resource watcher.
5. Calls `run_pytest()` -- **this is where all tests actually run**.
6. Parses JUnit XML for summary reporting.
7. Calls `process_coverage()` -- iterates empty `all_tests()`, does nothing.

Simplifying test.py to a thin CI wrapper is **highly feasible**.  The hardest piece (coverage
processing) is already non-functional.  Most remaining features are either
already handled by pytest or trivially portable.

---

## 1. Feature Gap Analysis

### Features Already Handled by Pytest

These features work identically in bare pytest (with `runner.py` plugin):

| Feature | test.py Implementation | Pytest Equivalent |
|---------|----------------------|-------------------|
| Test discovery & filtering | `--name`, `-k`, `--skip`, `--markers`, `--mode` translated to pytest args | Native `-k`, `-m`, `--mode` (runner.py) |
| Parallel execution | `-j` -> `-n{jobs} --dist=worksteal` | pytest-xdist directly |
| Test-level timeout | `--timeout` -> `--timeout={value}` | pytest-timeout plugin |
| Session-level timeout | `--session-timeout` -> `--session-timeout={value}` | pytest-timeout plugin |
| JUnit/XML output | `--junit-xml` passthrough | Native `--junit-xml` + custom attrs in runner.py |
| Allure reporting | `--alluredir` passthrough | allure-pytest plugin + `ReportPlugin` |
| Log management | `open_log()` creates `test.py.log` | runner.py sets up per-worker log files |
| Verbose/quiet modes | `-v`, `--quiet`, `-p no:sugar` | Direct pytest flags |
| Skip patterns | `--skip` -> `-k=not pat1 and not pat2` | Native `-k` expression |
| Marker filtering | `-m` passthrough | Native `-m` |
| Random seed | `--random-seed` passthrough | runner.py registers option |
| Repeat | `--repeat` passthrough | runner.py handles via `pytest_collect_file` |
| Extra scylla cmdline | `--extra-scylla-cmdline-options` passthrough | runner.py registers option |
| Gather metrics | `--gather-metrics` passthrough | runner.py registers option |
| Save log on success | `--save-log-on-success` passthrough | runner.py registers option |
| Exe path/URL | `--exe-path`, `--exe-url` passthrough | runner.py registers options |
| Max failures | `--max-failures` -> `--maxfail` | Native `--maxfail` |
| Mode selection | `--mode` | runner.py multi-mode collection |
| Suite config (test_config.yaml) | Via `TestSuiteConfig` | runner.py `TestSuiteConfig.from_pytest_node()` |
| 3rd party services | `prepare_environment()` | runner.py `pytest_sessionstart` (unconditional; `TESTPY_PREPARED_ENVIRONMENT` prevents double-init) |
| Cluster lifecycle cleanup | `TestSuite.artifacts.cleanup_before_exit()` | runner.py `pytest_sessionfinish` (unconditional; `TESTPY_PREPARED_ENVIRONMENT` prevents double-cleanup) |
| Test ordering | N/A (legacy used its own loop) | runner.py `pytest_collection_modifyitems` |
| Mode-based skip | N/A | runner.py `@pytest.mark.skip_mode` |
| Timeout scaling by mode | N/A | runner.py `scale_timeout` fixture |

### Features That Need Migration

| Feature | Current Location | Difficulty | Migration Path |
|---------|-----------------|------------|----------------|
| `ThreadsCalculator` | test.py:77-119 | **Easy** | Move to a conftest fixture or wrapper script that computes `-n` from system resources |
| `SCYLLA_CONF`/`SCYLLA_HOME` cleanup | test.py:895-898 | **Easy** | Add 4 lines to root `conftest.py` `pytest_configure` |
| `--cpus` (taskset binding) | test.py:250-252,332-335,442-443 | **Easy** | Run `taskset -c <cpus> pytest ...` externally, or add a conftest hook |
| `--test-py-init` guards | runner.py (4 hooks) | **Easy** | ✅ Done (Phase 2) -- guards removed; `TESTPY_PREPARED_ENVIRONMENT` is sufficient |
| `testpy_test_fixture_scope` | runner.py + ~40 fixtures | **Easy** | ✅ Done (Phase 2) -- condition changed from `--test-py-init` to `TEST_RUNNER`; function kept because runpy needs `"session"` scope |
| Resource watcher | test.py:604 | **Medium** | Move to a session-scoped fixture or pytest plugin using a background thread |
| Post-run JUnit XML summary | test.py:439-462 | **Easy** | Pytest's native summary is adequate; enhance with a plugin if needed |
| `process_coverage()` | test.py:641-888 | **Hard** | Extract to standalone script; currently non-functional anyway |
| `--list` mode (legacy suites) | test.py:580-584 | **N/A** | Dead code; pytest `--collect-only` already works |
| `--manual-execution` | test.py:293,597-599 | **N/A** | Dead code; only checked against `TestSuite.test_count()` which is 0 |
| Flaky test retry | base.py `TestSuite.run()` | **N/A** | Dead code; pytest-rerunfailures available if needed |

---

## 2. Migration Blockers

### Easy (< 1 day each)

**`SCYLLA_CONF`/`SCYLLA_HOME` env cleanup** -- 4 lines in test.py:895-898.
Move to root `conftest.py`:

```python
# In pytest_configure or conftest.py module level:
os.environ.pop("SCYLLA_CONF", None)
os.environ.pop("SCYLLA_HOME", None)
```

**`--test-py-init` removal** -- ✅ Done (Phase 2).  All 4 runner.py hooks are
now unconditional.  The `TESTPY_PREPARED_ENVIRONMENT` env var (set by test.py
before invoking pytest) is the correct and sufficient mechanism to detect
whether test.py already did setup.

**`testpy_test_fixture_scope` condition change** -- ✅ Done (Phase 2).  The
function's condition was changed from `--test-py-init` to `TEST_RUNNER`.  It
returns `"module"` for the pytest runner (both test.py and bare pytest) and
`"session"` for run.py scripts.  The function was NOT replaced with a literal
because runpy needs `"session"` scope (single Scylla instance) while pytest
needs `"module"` scope (one `Test` instance per module).

**`ThreadsCalculator`** -- 43 lines.  Options:
- Move to a pytest plugin that sets `-n` based on system resources.
- Move to a thin wrapper script (`run_tests.sh`) that computes `-n` and
  invokes pytest.
- Document as "run `pytest -n auto`" (pytest-xdist has its own auto mode,
  though without the memory heuristic).

**CI build targets** -- `configure.py:2567` and `test/CMakeLists.txt:127`
invoke `./test.py` with `--mode`, `--repeat`, `--timeout`.  No changes needed;
test.py is preserved as the CI entry point.

**Dead import cleanup** -- Remove `glob` (unused even now).

### Medium (1-3 days each)

**Resource watcher migration** -- `run_resource_watcher()` runs as an asyncio
task monitoring CPU/memory to SQLite during the entire test session.  Options:
- Session-scoped autouse fixture that spawns a background thread.
- Standalone pytest plugin with `pytest_sessionstart`/`pytest_sessionfinish`.
- Challenge: original uses asyncio; pytest hooks are synchronous, so it would
  need a thread-based approach.

**`scylla_gdb/conftest.py` crash fix** -- The `scylla_server` fixture has no
`None` guard for `testpy_test`.  It will crash in bare pytest.  Options:
- Add a `None` guard with an alternative code path (start Scylla server
  independently).
- If GDB tests are always run under test.py, this may be intentional -- but
  it blocks full migration.

**`cluster/conftest.py` decoupling** -- The `manager` fixture calls
`get_testpy_test()` solely for log path computation.  This instantiates a
full `TestSuite` + `Test` object just for strings like `log_dir` and
`log_filename`.  To decouple:
- Compute log paths directly from pytest's `tmp_path` or a configuration
  fixture.
- Remove the `get_testpy_test()` dependency.

**`cql/conftest.py` `output_path` fixture** -- Similarly calls
`get_testpy_test()` just for `testpy_test.suite.log_dir / f"{testpy_test.uname}.reject"`.
Replace with a path derived from pytest's `tmp_path`.

### Hard (> 3 days)

**`process_coverage()` extraction** -- 248 lines of LLVM coverage processing:
raw profiles -> indexed profiles -> lcov traces -> per-suite -> per-mode ->
consolidated.  Uses `treelib` for hierarchical statistics.

However, this function is **already non-functional**: it iterates
`TestSuite.all_tests()` which returns nothing because no `suite.yaml` files
exist.  The `--coverage` flag is registered in runner.py but no post-processing
happens.

Options:
1. **Extract to standalone script** -- Convert to a script that takes coverage
   data paths as arguments rather than depending on `TestSuite.all_tests()`.
   This is the cleanest approach but requires understanding the full pipeline.
2. **Defer** -- Since coverage processing is already broken, simplifying test.py
   does not make it worse.  Fix it separately as a standalone tool.
3. **Use pytest-cov** -- If the coverage model changes to Python-level
   coverage rather than LLVM source coverage, pytest-cov handles this natively.
   However, ScyllaDB's coverage is C++ LLVM coverage, not Python coverage, so
   pytest-cov is not applicable.

Recommendation: **Option 2 (defer)**.  Coverage processing needs a redesign
regardless of whether test.py exists.

---

## 3. CI / Infrastructure Dependencies

### Direct Command References

| File | Line | Reference | Impact |
|------|------|-----------|--------|
| `configure.py` | 2567 | `command = ./test.py --mode={mode} --repeat={test_repeat} --timeout={test_timeout}` | **HIGH** -- Ninja `test.{mode}` target |
| `test/CMakeLists.txt` | 127 | `COMMAND ./test.py --mode=${build_mode} --repeat=... --timeout=...` | **HIGH** -- CMake `test` target |
| `.github/copilot-instructions.md` | 28-73 | Multiple `./test.py` examples | LOW -- documentation |
| `docs/dev/testing.md` | 52-422 | Extensive `./test.py` documentation | LOW -- documentation |
| `HACKING.md` | 122,129,396 | `./test.py --mode=...` | LOW -- documentation |
| `docs/dev/code-coverage.md` | 40-42 | `./test.py --coverage` | LOW -- documentation |
| `test/rest_api/README.md` | 6 | `./test.py api/run` | LOW -- documentation |

### `--test-py-init` Coupling Points — ✅ ALL REMOVED (Phase 2)

All `--test-py-init` references have been removed.  The option no longer exists.
`TESTPY_PREPARED_ENVIRONMENT` env var is the sole mechanism for detecting that
test.py has already prepared the environment.

### Seastar References (separate project, non-blocking)

`seastar/.github/workflows/test.yaml:122` and `seastar/.claude/CLAUDE.md:60-82`
reference `./test.py` but this is Seastar's own test runner, not ScyllaDB's.

---

## 4. Step-by-Step Migration Plan

### Phase 1: Clean Up Dead Code (no behavioral change) — ✅ COMPLETE

Phase 1 has been completed.  9 files were modified, removing 552 lines of dead
code with 19 lines of insertions.  246 framework unit tests pass.

**What was removed:**

1. ✅ **Dead imports in test.py**: `glob`, `output_is_a_tty`,
   `SUITE_CONFIG_FILENAME`, `Any`/`TYPE_CHECKING`/`List` imports.

2. ✅ **`SUITE_CONFIG_FILENAME` fallback paths**: In `runner.py`,
   `from_pytest_node()` now checks only `TEST_CONFIG_FILENAME`.  In `base.py`,
   `get_testpy_test()` uses `TEST_CONFIG_FILENAME` directly.  The
   `SUITE_CONFIG_FILENAME` constant was deleted from `base.py`.

3. ✅ **Already-dead symbols**: `TestSuite.boost_tests()`,
   `TestSuite.junit_tests()` (base), `TopologyTestSuite.junit_tests()`,
   `Test.setup()`, `toxiproxy_id_gen`.

4. ✅ **`find_tests()` in test.py**: Removed entirely.  The call in `main()`
   was deleted.

5. ✅ **Legacy execution pipeline**: `TestSuite.run()`,
   `TestSuite.FLAKY_RETRIES`, `TestSuite.build_test_list()`,
   `TestSuite.add_test_list()`, `run_test()` (112 lines), `Test.reset()`,
   `Test.run()` (abstract), `Test.check_log()`, all concrete `Test.run()`
   implementations (`PythonTest`, `TopologyTest`, `ToolTest`, `RunTest`),
   `PythonTest.reset()`, `PythonTestSuite.run()`.

6. ✅ **test.py simplification**: `TabularConsoleOutput` class removed.
   `run_all_tests()` simplified to just run `run_pytest()` in executor +
   artifact cleanup.  `print_summary()` simplified (removed `failed_tests`
   and `cancelled_tests` params).  `main()` simplified (removed `find_tests()`
   call, manual_execution block, legacy test iteration).

7. ✅ **Dead suite files**: `tool.py` and `run.py` deleted entirely —
   `ToolTestSuite`/`ToolTest` and `RunTestSuite`/`RunTest` had no consumers
   since all directories migrated to `type: Python`.  `self.path` attribute
   removed from `Test` and `PythonTest` (never read by any production code).

**What was intentionally kept (at that time; later removed in Phase 4):**

- `Test.print_summary()` (abstract) and all subclass implementations — the
  `@abstractmethod` constraint required implementations.  ✅ Removed in Phase 4
  along with `read_log()`.
- `TestSuite.test_count()`, `TestSuite.all_tests()` — still called from
  `test.py` at that time.  ✅ Removed in Phase 4 (callers removed in Phase 3).
- `Test.failed`, `Test.did_not_run` properties — still read from `test.py`
  at that time.  ✅ Removed in Phase 4 (callers removed in Phase 3).
- `pattern` abstract property — required by ABC contract at that time.
  ✅ Removed in Phase 4.
- `PythonTest.run_ctx()` — cluster lifecycle context manager, still called
  by `test/cqlpy/conftest.py` and `test/scylla_gdb/conftest.py`.  Still present.
- `_prepare_pytest_params()` on all test classes — still called by conftest
  fixtures.  Still present.

### Phase 2: Make Runner Self-Sufficient — ✅ COMPLETE

Phase 2 has been completed.  2 source files were modified, removing the
`--test-py-init` option and making all runner.py hooks unconditional.

**Key discovery: three execution modes.**  There are three ways tests run, not
two.  Run.py scripts (`test/cqlpy/run`, `test/alternator/run`,
`test/rest_api/run`) set `SCYLLA_TEST_RUNNER=runpy`, which causes
`test/conftest.py` to skip loading runner.py as a plugin entirely.  This
affects fixture scoping: runpy needs `"session"` scope (single externally-
managed Scylla instance), while test.py and bare pytest need `"module"` scope
(each module gets its own `Test` instance).

**What was done:**

1. ✅ **`testpy_test_fixture_scope` condition changed**: From checking
   `--test-py-init` to checking `TEST_RUNNER`.  Returns `"module"` for the
   pytest runner (both test.py and bare pytest), `"session"` for run.py
   scripts.  The function was kept (not replaced with a literal) because the
   dynamic scope serves a real purpose across execution modes.

2. ✅ **`pytest_sessionstart` guard removed**: The `--test-py-init` guard was
   redundant — `TESTPY_PREPARED_ENVIRONMENT` env var (set by test.py on line
   463) already prevents double-initialization.

3. ✅ **`pytest_sessionfinish` guard removed**: Same rationale —
   `TESTPY_PREPARED_ENVIRONMENT` prevents double-cleanup.

4. ✅ **`pytest_configure` logging made unconditional**: Logging setup always
   runs.  When test.py calls pytest, both write to separate log files.

5. ✅ **`pytest_runtest_makereport` made unconditional**: Failure log capture
   always runs (purely additive).

6. ✅ **`--test-py-init` option removed** from runner.py and test.py.

**What was intentionally kept (deferred):**

- `scylla_gdb/conftest.py` crash — the `scylla_server` fixture has no `None`
  guard (deferred to later phase).
- `cluster/conftest.py` and `cql/conftest.py` `get_testpy_test()` calls —
  these create full `TestSuite`+`Test` objects for path computation (deferred).
- `testpy_test_fixture_scope` function itself — cannot be replaced with a
  literal because runpy needs `"session"` and pytest needs `"module"`.

### Phase 3: Simplify test.py to Thin Wrapper — ✅ COMPLETE

Phase 3 has been completed.  test.py was reduced from 755 lines to 334 lines
by removing all async scaffolding, dead functions, dead imports, and dead CLI
options.  The resource watcher was reimplemented as a thread-based monitor in
runner.py, and SCYLLA_CONF/SCYLLA_HOME cleanup was moved to conftest.py.

**What was done:**

1. ✅ **Simplified test.py to a synchronous thin wrapper**: `main()` is now a
   plain `def` (not `async def`) that calls `parse_cmd_line()`, `run_pytest()`,
   and optionally `coverage.generate_coverage_report()`.  The `asyncio.run()`
   call was removed from `__main__`.

2. ✅ **Removed functions**: `setup_signal_handlers()`, `run_all_tests()`,
   `print_summary()`, `open_log()`, `process_coverage()` — total ~370 lines
   removed.

3. ✅ **Simplified `run_pytest()`**: Now returns `int` (the `pytest.main()`
   exit code) instead of `tuple[int, list[SimpleNamespace]]`.  JUnit XML
   parsing was removed — pytest's native summary and exit code are sufficient.

4. ✅ **Removed dead CLI options**: `--no-parallel-cases`, `--log-level`,
   `--coverage-keep-raw`, `--coverage-keep-indexed`, `--coverage-keep-lcovs`,
   `--artifacts_dir_url`, `--manual-execution`, `--skip-internet-dependent-tests`.

5. ✅ **Removed dead imports**: `asyncio`, `itertools`, `resource`, `signal`,
   `time`, `xml.etree.ElementTree`, `humanfriendly`, `treelib`,
   `coverage_utils`, `LogPrefixAdapter`, `init_testsuite_globals`,
   `prepare_environment`, `TestSuite`, `run_resource_watcher`,
   `TESTPY_PREPARED_ENVIRONMENT`, `SimpleNamespace`.

6. ✅ **Moved `SCYLLA_CONF`/`SCYLLA_HOME` cleanup** to root `conftest.py`
   `pytest_configure` hook.  This means the cleanup runs for all execution
   modes (test.py, bare pytest, and run.py).

7. ✅ **Moved resource watcher** to runner.py as a thread-based monitor.
   `_resource_monitor_loop()` runs in a daemon thread, polling `psutil` every
   2 seconds and writing `SystemResourceMetric` records to SQLite.  Started in
   `pytest_sessionstart` when `--gather-metrics` is true; stopped in
   `pytest_sessionfinish`.

8. ✅ **`TESTPY_PREPARED_ENVIRONMENT` no longer set** by test.py — runner.py
   always handles environment setup.  test.py no longer calls
   `prepare_environment()` or `init_testsuite_globals()`.

**What was intentionally kept:**

- `ThreadsCalculator` — still used by `parse_cmd_line()` to compute `--jobs`.
- `PYTEST_RUNNER_DIRECTORIES` — used by `run_pytest()` for file selection.
- `parse_cmd_line()` — simplified but still needed for CI compatibility.
- `run_pytest()` — simplified to just assemble args and call `pytest.main()`.
- Coverage report generation — `coverage.generate_coverage_report()` for the
  `"coverage"` build mode.
- CI targets unchanged — `configure.py` and `CMakeLists.txt` still invoke
  `./test.py` with `--mode`, `--repeat`, `--timeout`.

### Phase 4: Clean Up Suite Framework — ✅ COMPLETE

Phase 4 has been completed.  Dead code from the suite framework was removed
across 6 source files, empty subclasses were eliminated, and dead options
were cleaned up from runner.py.

**What was removed from `base.py`:**

- `TestSuite.test_count()`, `TestSuite.all_tests()` — static methods with
  zero callers after Phase 3.
- `TestSuite.pattern` abstract property — only consumed by the deleted
  `build_test_list()`.  Removing this also removed the `@abstractmethod`
  constraint that forced all subclasses to implement `pattern`.
- `TestSuite.__init__` dead attributes: `pending_test_count`, `n_failed`,
  `run_first_tests`, `no_parallel_cases`, `disabled_tests`, `flaky_tests`.
- `Test.failed` property, `Test.did_not_run` property — zero callers.
- `Test.print_summary()` abstract method — zero callers.
- `Test.__init__` dead attributes: `is_flaky`, `is_flaky_failure`,
  `is_cancelled`, `env`, `started`, `allure_dir`.
- `read_log()` function — only called by dead `print_summary()` methods.
- Dead `palette` members: `ok`, `new`, `skip`, `path`, `warn`, `crit`,
  `ansi_escape`, `nocolor()`.
- Dead imports: `itertools`, `re`, `ALL_MODES`, `DEBUG_MODES`, `Iterable`.
- `Approval` special case in `suite_type_to_class_name()`.

**What was removed from subclass files:**

- All `pattern` property overrides: `PythonTestSuite`, `CQLApprovalTestSuite`.
- All `print_summary()` methods: `PythonTest`.
- `PythonTestSuite.scylla_env` (set in `__init__`, never read outside).
- `TopologyTest.status` type annotation (never set or read).
- Orphaned `from scripts import coverage` import in `python.py`
  (only used by removed `scylla_env` block).
- `read_log` import from `python.py`.
- `PythonTest._prepare_pytest_params()` method and `self.xmlout` attribute.
- `PythonTestSuite.test_file_ext` class attribute.
- `PythonTest.server_log` attribute.

**Empty subclass removal:**

- `TopologyTestSuite`/`TopologyTest` in `topology.py` — empty pass-through
  subclasses.  File deleted.  `test/cluster/test_config.yaml` changed from
  `type: Topology` to `type: Python`.
- `CQLApprovalTestSuite` in `cql_approval.py` — only added
  `test_file_ext = ".cql"`.  File deleted.  `test/cql/test_config.yaml`
  changed from `type: Approval` to `type: Python`.

**Runner cleanup:**

- `--scylla-log-filename` option removed from `runner.py`.
- `print_scylla_log_filename` autouse fixture removed from `runner.py`.

**`run_ctx()` signature change:**

- `run_ctx(options)` → `run_ctx()`: the `options` parameter was only needed
  by `_prepare_pytest_params()`.  Callers in `test/cqlpy/conftest.py` and
  `test/scylla_gdb/conftest.py` updated.  `self.args.insert()` calls for
  `--host` and `--scylla-log-filename` removed from `run_ctx()` body.

### Phase 5: Documentation and Cleanup — ✅ COMPLETE

Phase 5 has been completed.  External-facing documentation was updated to
reflect that test.py is a thin wrapper and bare pytest works directly.

**What was done:**

1. ✅ **Updated `docs/dev/testing.md`** -- Rewrote introduction to describe
   test.py as a thin wrapper.  Added bare pytest usage section.  Fixed
   "How it works" to reflect that runner.py handles discovery.  Updated stale
   `test.py.log` references to `testlog/pytest_log/`.  Fixed stale
   `suite.yaml` references to `test_config.yaml`.

2. ✅ **Updated `HACKING.md`** -- Added bare pytest example to "Unit testing"
   section.  Nuanced the coverage section to note that coverage post-processing
   requires test.py.

3. ✅ **Updated `.github/copilot-instructions.md`** -- Added note that bare
   pytest works directly alongside test.py.

4. ✅ **Updated `docs/dev/code-coverage.md`** -- Added note that coverage
   post-processing is handled by test.py, and bare pytest requires manual
   profile processing.

5. ✅ **`test/docs/test-py-design.md`** -- Already accurate, no changes
   needed.

---

## 5. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Coverage processing breaks | None | None | It's already broken (empty `all_tests()`) |
| scylla_gdb tests break | Medium | Low | These tests may already be broken in bare pytest; fix the `None` guard |
| Fixture scope condition change causes unexpected issues | Low | Low | ✅ Done — condition changed from `--test-py-init` to `TEST_RUNNER`; returns correct scope for all three modes |
| Developer workflows disrupted | Medium | Low | Provide clear migration documentation; test.py is preserved as a thin wrapper |
| External tools/scripts reference `./test.py` | Low | Low | Grep the codebase; only configure.py and CMakeLists.txt are programmatic references |

---

## 6. Effort Estimate

| Phase | Effort | Risk |
|-------|--------|------|
| Phase 1: Dead code removal | ✅ Done | Very low (no behavioral change) |
| Phase 2: Runner self-sufficiency | ✅ Done | Low (removed guards, changed scope condition) |
| Phase 3: Simplify test.py | ✅ Done | Low (thin wrapper preserved for CI) |
| Phase 4: Suite framework cleanup | ✅ Done | Low (removed dead code from 6 source files) |
| Phase 5: Documentation | ✅ Done | None |
| **Total** | **7-11 days** | |

The phases can be executed incrementally.  Each phase produces a working state.
Phase 1 can be merged independently with zero risk.  Phases 2-3 should be
coordinated.  Phases 4-5 are pure cleanup.
