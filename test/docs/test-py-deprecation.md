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
| `--test-py-init` guards | runner.py (4 hooks) | **Easy** | Remove the guards; `TESTPY_PREPARED_ENVIRONMENT` is sufficient to prevent double-init |
| `testpy_test_fixture_scope` | runner.py + ~40 fixtures | **Easy** | Change condition from `--test-py-init` to `TEST_RUNNER`; keep function (runpy needs `"session"` scope) |
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

**`--test-py-init` removal** -- Remove the `--test-py-init` option and all
4 runner.py hook guards.  The `TESTPY_PREPARED_ENVIRONMENT` env var (set by
test.py before invoking pytest) is the correct and sufficient mechanism to
detect whether test.py already did setup.  ~20 lines of `if` removal.

**`testpy_test_fixture_scope` condition change** -- Change the function's
condition from `--test-py-init` to `TEST_RUNNER`.  The function cannot be
replaced with a literal `"module"` because run.py scripts
(`SCYLLA_TEST_RUNNER=runpy`) need `"session"` scope (single Scylla instance),
while test.py and bare pytest need `"module"` scope (one `Test` per module).
No changes to conftest call sites are needed.

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

### `--test-py-init` Coupling Points

| File | Lines | Context |
|------|-------|---------|
| `test.py` | 398 | Passes `--test-py-init` to pytest |
| `test/pylib/runner.py` | 82-87 | Option definition |
| `test/pylib/runner.py` | 197 | Guards `pytest_sessionstart` init |
| `test/pylib/runner.py` | 267 | Guards `pytest_sessionfinish` cleanup |
| `test/pylib/runner.py` | 298 | Guards `pytest_configure` logging |
| `test/pylib/runner.py` | 382 | Guards `pytest_runtest_makereport` log capture |

### Seastar References (separate project, non-blocking)

`seastar/.github/workflows/test.yaml:122` and `seastar/.claude/CLAUDE.md:60-82`
reference `./test.py` but this is Seastar's own test runner, not ScyllaDB's.

---

## 4. Step-by-Step Migration Plan

### Phase 1: Clean Up Dead Code (no behavioral change)

These changes can be made immediately with no risk:

1. **Remove dead imports in test.py**: `glob`, `itertools`.

2. **Remove `SUITE_CONFIG_FILENAME` fallback paths**: In `runner.py:422` and
   `base.py:600`, remove the `suite.yaml` check and go directly to
   `test_config.yaml`.  Delete `SUITE_CONFIG_FILENAME` constant.

3. **Remove already-dead symbols**: `TestSuite.boost_tests()`,
   `TestSuite.junit_tests()` (base), `Test.setup()`, `toxiproxy_id_gen`.

4. **Remove `find_tests()` in test.py**: It discovers nothing.  Remove the
   call in `main()` and the `await find_tests(options)` line.

5. **Remove legacy-only symbols in suite framework** that are not part of the
   ABC contract: `TestSuite.FLAKY_RETRIES`, `TestSuite.test_count()`,
   `TestSuite.all_tests()`, `TestSuite.build_test_list()`,
   `TestSuite.add_test_list()`, `read_log()`, `run_test()`,
   `Test.reset()`, `Test.failed`, `Test.did_not_run`, `Test.check_log()`,
   all `Test.run()` implementations, all `Test.print_summary()`
   implementations, `PythonTestSuite.run()`.

### Phase 2: Make Runner Self-Sufficient

These changes make `runner.py` work without `--test-py-init`:

1. **Change `testpy_test_fixture_scope` condition**: From checking
   `--test-py-init` to checking `TEST_RUNNER`.  Returns `"module"` for the
   pytest runner (both test.py and bare pytest), `"session"` for run.py
   scripts.  The function is kept (not replaced with a literal) because the
   dynamic scope serves a real purpose across three execution modes.

2. **Make `pytest_sessionstart` init unconditional**: Remove the
   `--test-py-init` guard.  The `TESTPY_PREPARED_ENVIRONMENT` env var (set by
   test.py on line 463) already prevents double-initialization.

3. **Make `pytest_sessionfinish` cleanup unconditional**: Same rationale --
   `TESTPY_PREPARED_ENVIRONMENT` prevents double-cleanup.

4. **Make `pytest_configure` logging unconditional**: Logging setup always
   runs.  When test.py calls pytest, both write to separate log files.

5. **Make `pytest_runtest_makereport` unconditional**: Failure log capture
   always runs (purely additive).

6. **Remove `--test-py-init` option** from runner.py and test.py.

### Phase 3: Simplify test.py to Thin Wrapper

Simplify test.py by moving functionality to runner.py and conftest.py:

1. **Move `ThreadsCalculator`** to a conftest fixture or a small wrapper
   script.

2. **Move `SCYLLA_CONF`/`SCYLLA_HOME` cleanup** to root `conftest.py`.

3. **Move resource watcher** to a pytest plugin (session-scoped, thread-based).

4. **Remove `TabularConsoleOutput`**, `setup_signal_handlers()`,
   `run_all_tests()` async scaffolding, and `process_coverage()`.

5. **Simplify `main()`** to a synchronous function: parse args, invoke pytest,
   return exit code.

### Phase 4: Clean Up Suite Framework

With the legacy execution pipeline removed, the suite framework can be
substantially simplified.

**Dead code removal** (symbols with zero remaining callers):

1. **Remove `TestSuite.test_count()`** -- only called from deleted `test.py`
   functions.
2. **Remove `TestSuite.all_tests()`** -- only called from deleted `test.py`
   functions.
3. **Remove abstract `pattern` property** from `TestSuite` and all overrides
   (only consumed by deleted `build_test_list()`).
4. **Remove `Test.failed`** and **`Test.did_not_run`** properties -- only read
   from deleted `test.py` functions.
5. **Remove `Test.print_summary()` abstract** and all subclass implementations
   -- only called from deleted `test.py` functions.
6. **Remove `read_log()`** -- only called from deleted `print_summary()`
   methods.
7. **Remove `PythonTest.scylla_env` setup block** -- only used by deleted
   `run()` method.
8. **Remove palette.nocolor members** -- dead code.

**Empty subclass removal** (classes that are trivial pass-throughs after
Phase 1 removed their `run()` / `junit_tests()` overrides):

9. **Remove `TopologyTestSuite`/`TopologyTest`** in `topology.py` -- empty
   subclasses that add nothing over `PythonTestSuite`/`PythonTest`.  Change
   `test/cluster/test_config.yaml` from `type: Topology` to `type: Python`.
10. **Remove `CQLApprovalTestSuite`** in `cql_approval.py` -- only adds
    `test_file_ext = ".cql"`.  Remove `test_file_ext` from `PythonTestSuite`
    as well (no longer needed without the subclass override).  Change
    `test/cql/test_config.yaml` from `type: Approval` to `type: Python`.
    Remove the `Approval` special case from `suite_type_to_class_name()`.

**Dead method and attribute removal** (shared code that becomes dead once
`run()` and its callees are removed):

11. **Remove `PythonTest._prepare_pytest_params()`** -- only called from
    `run_ctx()` which builds `self.args` itself; `run_ctx()` signature changes
    from `run_ctx(options)` to `run_ctx()`.  Also remove `self.xmlout` and
    `self.args` from `PythonTest.__init__`.
12. **Remove `Test.allure_dir`** -- only consumed by deleted
    `_prepare_pytest_params()`.
13. **Remove `PythonTest.server_log`** -- set in `run_ctx()` but never read
    after `print_summary()` was removed.

**Runner cleanup** (options that existed only for test.py → pytest
communication):

14. **Remove `--scylla-log-filename`** option from `runner.py` -- only passed
    by test.py's legacy pipeline via `_prepare_pytest_params()`.
15. **Remove `print_scylla_log_filename`** autouse fixture from `runner.py`
    -- depends on `--scylla-log-filename`.

### Phase 5: Documentation and Cleanup

1. **Update `docs/dev/testing.md`** -- Document bare pytest invocations
   alongside `./test.py` examples.

2. **Update `HACKING.md`** -- Same.

3. **Update `.github/copilot-instructions.md`** -- Same.

4. **Update `docs/dev/code-coverage.md`** -- Document new coverage approach.

5. **Update `test/docs/test-py-design.md`** -- Keep in sync with current
   test.py state.

---

## 5. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Coverage processing breaks | None | None | It's already broken (empty `all_tests()`) |
| scylla_gdb tests break | Medium | Low | These tests may already be broken in bare pytest; fix the `None` guard |
| Fixture scope condition change causes unexpected issues | Low | Low | The condition changes from `--test-py-init` to `TEST_RUNNER`; returns correct scope for all three modes |
| Developer workflows disrupted | Medium | Low | Provide clear migration documentation; test.py is preserved as a thin wrapper |
| External tools/scripts reference `./test.py` | Low | Low | Grep the codebase; only configure.py and CMakeLists.txt are programmatic references |

---

## 6. Effort Estimate

| Phase | Effort | Risk |
|-------|--------|------|
| Phase 1: Dead code removal | 1-2 days | Very low (no behavioral change) |
| Phase 2: Runner self-sufficiency | 2-3 days | Low (remove guards, change scope condition) |
| Phase 3: Simplify test.py | 2-3 days | Medium (CI integration) |
| Phase 4: Suite framework cleanup | 1-2 days | Low |
| Phase 5: Documentation | 1 day | None |
| **Total** | **7-11 days** | |

The phases can be executed incrementally.  Each phase produces a working state.
Phase 1 can be merged independently with zero risk.  Phases 2-3 should be
coordinated.  Phases 4-5 are pure cleanup.
