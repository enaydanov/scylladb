# Pytest Runner Plugin Design Document

This document describes the pytest plugin `test/pylib/runner.py` that bridges
the pytest test framework with the ScyllaDB suite infrastructure. It covers
the complete file structure, hooks, fixtures, and classes.

**Related document:** [Test Suite Framework Design](test-suite-design.md)

## Table of Contents

1. [Overview](#1-overview)
2. [Imports and Module-Level State](#2-imports-and-module-level-state)
3. [Command-Line Options](#3-command-line-options)
4. [Pytest Configuration Hook](#4-pytest-configuration-hook)
5. [Session Lifecycle Hooks](#5-session-lifecycle-hooks)
6. [Collection Hooks](#6-collection-hooks)
7. [Test Execution Hooks](#7-test-execution-hooks)
8. [TestSuiteConfig Class](#8-testsuiteconfig-class)
9. [Fixtures](#9-fixtures)
10. [Helper Functions](#10-helper-functions)
11. [Integration Summary](#11-integration-summary)

---

## 1. Overview

`runner.py` is a pytest plugin (auto-discovered via `conftest.py` import chain)
that provides the following capabilities:

- **Mode/repeat multiplexing**: a single test file is collected once per
  `(build_mode, run_id)` combination, enabling cross-mode and repeat testing.
- **Suite framework bridge**: the `testpy_test` fixture creates `Test` instances
  from the suite framework, giving conftest files access to cluster pools,
  host registries, and suite configuration.
- **Collection-time filtering**: `TestSuiteConfig` reads `test_config.yaml` to
  skip disabled tests during collection (before any execution).
- **Logging and reporting**: per-worker log files, JUnit XML customization, and
  failure log capture.
- **Lifecycle management**: initializes suite globals and prepares the environment
  during session start, cleans up during session finish.

The file is approximately 527 lines.

---

## 2. Imports and Module-Level State

### 2.1 Suite Framework Imports

From `test.pylib.suite.base`:
- `PYTEST_TESTS_LOGS_FOLDER` -- subdirectory name for failure logs
- `TestSuite` -- class-level `artifacts`, `hosts`, `init_testsuite_globals()`
- `get_testpy_test` -- creates a `Test` instance for a file path
- `prepare_environment` -- initializes directories and services
- `init_testsuite_globals` -- one-time global setup

From `test/__init__.py`:
- `ALL_MODES`, `DEBUG_MODES` -- mode definitions
- `TEST_RUNNER` -- `"pytest"` (default, for test.py and bare pytest) or `"runpy"` (for run.py scripts); from `SCYLLA_TEST_RUNNER` env
- `TOP_SRC_DIR` -- repository root
- `TESTPY_PREPARED_ENVIRONMENT` -- env var gate
- `HOST_ID` -- unique host identifier

Resource watcher imports:
- `threading` -- thread management for resource monitor
- `datetime` -- timestamp generation
- `psutil` -- CPU/memory polling
- `test.pylib.db.model.SystemResourceMetric` -- data model for metrics
- `test.pylib.db.writer.SQLiteWriter`, `DEFAULT_DB_NAME`, `SYSTEM_RESOURCE_METRICS_TABLE` -- database writes

### 2.2 StashKeys

Four `pytest.StashKey` instances for storing per-node metadata:

| Key | Type | Purpose |
|-----|------|---------|
| `REPEATING_FILES` | `set[pathlib.Path]` | Tracks files already multiplexed (prevents infinite recursion in `pytest_collect_file`) |
| `BUILD_MODE` | `str` | Build mode assigned to this collector |
| `RUN_ID` | `int` | Repeat run ID assigned to this collector |
| `PYTEST_LOG_FILE` | `str` | Path to the current worker/main process log file |

A fifth StashKey, `TEST_SUITE`, is defined at module level (line 423) after the
`TestSuiteConfig` class:

| Key | Type | Purpose |
|-----|------|---------|
| `TEST_SUITE` | `TestSuiteConfig \| None` | Suite config associated with a collector/item |

### 2.3 Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `TEST_CONFIG_FILENAME` | `"test_config.yaml"` | Locally redefined (not imported from `base.py`) |
| `PYTEST_LOG_FOLDER` | `"pytest_log"` | Subdirectory for pytest process logs |
| `EXIT_MAXFAIL_REACHED` | `11` | Custom exit code when max failures reached |

### 2.4 Global State

| Variable | Type | Purpose |
|----------|------|---------|
| `logger` | `logging.Logger` | Module-level logger |
| `_pytest_config` | `pytest.Config \| None` | Global config reference, set in `pytest_configure` |
| `_resource_watcher_stop` | `threading.Event \| None` | Stop signal for resource monitor thread |
| `_resource_watcher_thread` | `threading.Thread \| None` | Background resource monitoring thread |

---

## 3. Command-Line Options

### `pytest_addoption(parser: pytest.Parser)`

Registers the following options:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--mode` | `choices(ALL_MODES)`, append | (auto-detect) | Build modes |
| `--tmpdir` | `str` | `TOP_SRC_DIR / 'testlog'` | Temp directory |
| `--run_id` | `str` | `None` | Explicit run ID |
| `--byte-limit` | `int` | `randint(0,2000)` | Failure injection byte limit |
| `--gather-metrics` | `BooleanOptionalAction` | `False` | Resource metrics |
| `--random-seed` | `str` | `None` | Boost RNG seed |
| `--save-log-on-success` | `bool` | `False` | Keep success logs |
| `--coverage` | `bool` | `False` | Coverage support |
| `--coverage-mode` | `list[str]` | `None` | Per-mode coverage |
| `--cluster-pool-size` | `int` | `None` | Pool size override |
| `--extra-scylla-cmdline-options` | `str` | `""` | Extra Scylla CLI options |
| `--x-log2-compaction-groups` | `int` | `0` | Compaction group count |
| `--repeat` | `int` | `1` | Repeat count |
| `--exe-path` | `str` | `False` | Custom executable path |
| `--exe-url` | `str` | `False` | Executable download URL |

These options mirror `test.py`'s options to maintain compatibility when test.py
invokes pytest.

---

## 4. Pytest Configuration Hook

### `pytest_configure(config: pytest.Config)`

Called during pytest startup. Performs:

1. **Store global config**: sets `_pytest_config = config`.

2. **Logging setup**:
   - Creates `pytest_log_dir = tmpdir / PYTEST_LOG_FOLDER`.
   - **xdist worker**: log file is `pytest_log/pytest_<worker_id>_<HOST_ID>.log`.
   - **Main process**: creates `pytest_log` directory, cleans old logs (unless
     `--save-log-on-success`), log file is `pytest_log/pytest_main_<HOST_ID>.log`.
   - Configures `logging.basicConfig` with the pytest config's log format/level.

3. **Executable validation**: `--exe-url` and `--exe-path` are mutually exclusive.
   Either one forces `modes = ["custom_exe"]` and cannot coexist with `--mode`.

4. **Shuffle seed**: sets `TOPOLOGY_RANDOM_FAILURES_TEST_SHUFFLE_SEED` env var to
   a random value if not already set.

5. **Build modes**: `config.build_modes = get_modes_to_run(config)`.

6. **Run IDs**: from `--repeat` and `--run_id`:
   - If `--run_id` set: `config.run_ids = (testpy_run_id,)`. Cannot combine
     with `--repeat != 1`.
   - Otherwise: `config.run_ids = tuple(range(1, repeat + 1))`.

---

## 5. Session Lifecycle Hooks

### 5.1 `pytest_sessionstart(session: pytest.Session)`

Runs during session startup. Gates:
- Skips if `TEST_RUNNER != "pytest"` or `--collect-only`.

**Global initialization** (xdist workers or if test.py hasn't prepared):
- Calls `init_testsuite_globals()`.
- Registers `TestSuite.hosts.cleanup` as an exit artifact.

**Environment preparation** (main process only, if test.py hasn't prepared):
- Calls `prepare_environment()` with tmpdir, modes, gather_metrics,
  save_log_on_success, and byte_limit.
- If `--gather-metrics` is true: starts the resource watcher via
  `_start_resource_watcher(temp_dir)`.

The xdist detection uses `xdist.is_xdist_worker(request_or_session=session)`.

### 5.2 `pytest_sessionfinish(session: pytest.Session)`

Runs during session teardown.

1. **Log cleanup**: if not xdist-worker-in-test.py-mode, and all tests passed,
   and `--save-log-on-success` is false: deletes the pytest log file.

2. **Resource watcher shutdown** (non-worker, non-test.py): calls
   `_stop_resource_watcher()` to stop the background monitoring thread.

3. **Artifact cleanup** (non-worker, non-test.py): calls
   `asyncio.run(TestSuite.artifacts.cleanup_before_exit())`.

4. **Exit code**: if `maxfail > 0` and `testsfailed >= maxfail`, sets
   `session.exitstatus = EXIT_MAXFAIL_REACHED` (11) for CI detection.

---

## 6. Collection Hooks

### 6.1 `pytest_collect_file` (wrapper hook)

Decorated with `@pytest.hookimpl(wrapper=True)`. Intercepts the standard file
collection and multiplexes it across `(build_mode, run_id)` combinations.

**Algorithm:**

1. Yields to the inner hook to get the initial `collectors` list.
2. If exactly one collector and the file hasn't been seen before (tracked in
   `REPEATING_FILES` stash set):
   a. Marks the file as being processed (adds to set).
   b. Determines build modes from `config.build_modes`.
   c. If a `TestSuiteConfig` is found for the collector, filters out modes where
      the test is disabled via `is_test_disabled(build_mode, file_path)`.
   d. Computes `repeats = list(product(build_modes, config.run_ids))`.
   e. If `repeats` is empty: returns `[]` (test fully disabled).
   f. Invokes `ihook.pytest_collect_file()` `len(repeats) - 1` additional times
      to create duplicate collectors.
   g. Assigns `BUILD_MODE`, `RUN_ID`, and `TEST_SUITE` stash entries to each
      collector via `zip(repeats, collectors, strict=True)`.
   h. Removes the file from `REPEATING_FILES` (re-enables future collection if
      called again).
3. Returns the (possibly expanded) collectors list.

### 6.2 `pytest_collection_modifyitems(items: list[pytest.Item])`

Post-collection hook that processes all collected items:

1. Calls `modify_pytest_item(item)` on each item (see Section 10).
2. Sorts items by suite order: uses a `defaultdict(count().__next__)` to number
   suites in order of first appearance.
3. Sort key: `(suite_order_number, not_in_run_first)`. Tests whose file stem is
   in `suite.cfg["run_first"]` are sorted before others within the same suite.

---

## 7. Test Execution Hooks

### 7.1 `pytest_runtest_makereport(item, call)`

Decorated with `@pytest.hookimpl(tryfirst=True, hookwrapper=True)`.

Captures test failure details and writes them to log files in the
`PYTEST_TESTS_LOGS_FOLDER` directory.

For each report (setup, call, teardown):
- If the report indicates failure or `--save-log-on-success`:
  - Writes to a file named
    `<nodeid_sanitized>-<when>-<HOST_ID>.log`
    (`::`replaced with `-`, `/` replaced with `-`).
  - Writes `longreprtext` and all report sections (header + content).

### 7.2 `pytest_runtest_logreport(report)`

Decorated with `@pytest.hookimpl(tryfirst=True)`.

Adds a custom `function_path` attribute to JUnit XML `<testcase>` elements:

1. Gets the XML reporter from `config.stash[xml_key]`.
2. Gets or creates the `node_reporter` for this report.
3. If not already modified (tracked via `__reporter_modified` flag):
   - Computes `function_path` from the `nodeid` by stripping parameters (`[...]`)
     and mode suffix (`.mode`), prefixing with `test/`.
   - Wraps the `to_xml()` method to add `function_path` as an XML attribute on
     the generated `<testcase>` element.

---

## 8. TestSuiteConfig Class

A lightweight, collection-time-only representation of suite configuration.
Unlike `TestSuite` from the suite framework, this class does **not** create
clusters, pools, or test instances. It exists solely to filter disabled tests
during pytest collection.

### 8.1 Constructor

```python
def __init__(self, config_file: pathlib.Path):
```

- `self.path = config_file.parent`
- `self.cfg = yaml.safe_load(config_file.read_text(encoding="utf-8"))`

### 8.2 Properties and Methods

**`name`** (cached_property): `self.path.name`.

**`_run_in_specific_mode`** (cached_property): union of all tests listed in
`run_in_<mode>` directives across all modes in `ALL_MODES`. Returns a `set[str]`.

**`disabled_tests(build_mode: str) -> set[str]`** (cached via `@cache`):
Computes the set of disabled tests for a specific build mode. Algorithm mirrors
`TestSuite.__init__` (see [test-suite-design.md](test-suite-design.md) Section 3.2):

1. Starts with `cfg["disable"]`.
2. Adds `cfg["skip_in_<build_mode>"]`.
3. If `build_mode` is in `DEBUG_MODES`: adds `cfg["skip_in_debug_modes"]`.
4. Computes `run_in_<build_mode>` tests. Adds the difference between
   `_run_in_specific_mode` and `run_in_<build_mode>` (tests opted into other
   modes but not this one).

**`is_test_disabled(build_mode: str, path: pathlib.Path) -> bool`**: checks if
the relative path (without extension) is in `disabled_tests(build_mode)`.

### 8.3 Factory Method

**`from_pytest_node(cls, node) -> TestSuiteConfig | None`** (classmethod):

Recursively walks the pytest node tree to find the suite config:

1. Checks for `TEST_CONFIG_FILENAME` (`test_config.yaml`) in `node.path`.
2. If not found: walks up via `node.parent` stash or recursive call.
3. If found: applies `--extra-scylla-cmdline-options` by merging into the config's
   `extra_scylla_cmdline_options` via `merge_cmdline_options()`.
4. Stores the result in `node.stash[TEST_SUITE]`.

---

## 9. Fixtures

### 9.1 `testpy_test_fixture_scope`

Not a fixture itself, but a **scope function** used by multiple fixtures:

```python
def testpy_test_fixture_scope(fixture_name: str, config: pytest.Config) -> _ScopeName
```

Returns `"module"` when `TEST_RUNNER` is `"pytest"` (both test.py and bare
pytest), where each module needs its own `Test` instance tied to its
`test_config.yaml` and build mode.  Returns `"session"` when `TEST_RUNNER` is
`"runpy"` (run.py scripts start a single Scylla instance for the entire test
session, so fixtures like `host` and `cql` should be session-scoped).

Has `__test__ = False` to prevent pytest from collecting it as a test.

### 9.2 `testpy_test` (async, scope=dynamic)

The primary bridge fixture between pytest and the suite framework.

```python
@pytest.fixture(scope=testpy_test_fixture_scope)
async def testpy_test(request, build_mode) -> Test | None
```

- If scope is `"module"` (test.py and bare pytest): calls `get_testpy_test(path=request.path, options=request.config.option, mode=build_mode)` and returns the created `Test` instance.
- If scope is `"session"` (runpy): returns `None`.  In runpy mode, runner.py is
  not loaded as a plugin; `test/conftest.py` provides its own session-scoped
  `testpy_test` that also returns `None`.

When it returns a `Test` instance, conftest files can access `testpy_test.suite`
for cluster pool, host registry, and configuration.

### 9.3 `build_mode` (scope=dynamic)

```python
@pytest.fixture(scope=testpy_test_fixture_scope, autouse=True)
def build_mode(request) -> str
```

Returns the build mode for the current test:
- If the test has been assigned a `BUILD_MODE` stash entry (via collection
  multiplexing): returns that value.
- Otherwise: returns `config.build_modes[0]` as fallback.

### 9.4 `scale_timeout` (scope=dynamic)

```python
@pytest.fixture(scope=testpy_test_fixture_scope)
def scale_timeout(build_mode) -> Callable[[int | float], int | float]
```

Returns a closure that scales a timeout value based on the current build mode
via `scale_timeout_by_mode()`. Debug modes get longer timeouts.

### 9.5 `scylla_binary` (scope=function)

```python
@pytest.fixture(scope="function")
def scylla_binary(testpy_test) -> Path
```

Returns `testpy_test.suite.scylla_exe`.

---

## 10. Helper Functions

### 10.1 `get_params_stash(node) -> pytest.Stash | None`

Walks up the node tree to find the parent `pytest.File` collector and returns
its stash. Returns `None` if no File parent exists.

### 10.2 `modify_pytest_item(item: pytest.Item)`

Called during `pytest_collection_modifyitems` for each item. Performs:

1. **Stash propagation**: copies `BUILD_MODE`, `RUN_ID`, and `TEST_SUITE` from
   the parent File collector's stash to the item's stash.

2. **ID suffixing**: appends `.<build_mode>.<run_id>` to both `item._nodeid`
   and `item.name`. This makes each mode/repeat combination a distinct test in
   pytest's view.

3. **Skip-mode markers**: processes `@pytest.mark.skip_mode(mode, reason, platform_key=None)`:
   - `mode` can be a string or list of strings.
   - If the current build mode matches and (optionally) the platform matches,
     adds a `pytest.mark.skip` marker.

4. **Auto-nightly**: if an item has `xfail` marker but not `nightly`, adds
   `nightly` marker automatically.

5. **Auto-non_gating**: if an item has `perf`, `manual`, or `unstable` markers
   but not `non_gating`, adds `non_gating` marker.

### 10.3 `_STASH_KEYS_TO_COPY`

Tuple `(BUILD_MODE, RUN_ID, TEST_SUITE)` -- the stash keys propagated from
collector to item.

---

## 11. Resource Watcher

The resource watcher monitors system CPU and memory utilization during test
execution, writing periodic snapshots to SQLite for post-run analysis.

### 11.1 Architecture

The watcher uses a daemon thread (not asyncio) to decouple from the pytest
event loop.  Three module-level functions manage the lifecycle:

**`_resource_monitor_loop(stop_event, tmpdir)`**: the thread target.  Creates a
`SQLiteWriter` and loops every 2 seconds (using `stop_event.wait(timeout=2.0)`).
Each iteration calls `psutil.cpu_percent(interval=0.1)` and
`psutil.virtual_memory().percent`, wraps the values in a `SystemResourceMetric`
record, and writes to the `SYSTEM_RESOURCE_METRICS_TABLE`.

**`_start_resource_watcher(tmpdir)`**: creates a `threading.Event` and
`threading.Thread`, starts the thread.

**`_stop_resource_watcher()`**: sets the stop event, joins the thread with a
5-second timeout, and resets both globals to `None`.

### 11.2 Integration

- **Started** in `pytest_sessionstart` after `prepare_environment()`, only when
  `--gather-metrics` is true and the session is not an xdist worker.
- **Stopped** in `pytest_sessionfinish` before artifact cleanup, only for the
  main process (after the xdist worker early-return check).
- **Safe to skip**: if `--gather-metrics` is false (default for bare pytest),
  the watcher is never started.  `_stop_resource_watcher()` is a no-op when
  no watcher is running.

---

## 12. Integration Summary

### Suite Framework Usage

| Symbol | Usage |
|--------|-------|
| `PYTEST_TESTS_LOGS_FOLDER` | `pytest_runtest_makereport` -- failure log directory |
| `TestSuite.artifacts` | `pytest_sessionstart` (exit artifact), `pytest_sessionfinish` (cleanup) |
| `TestSuite.hosts` | `pytest_sessionstart` (cleanup registration) |
| `init_testsuite_globals()` | `pytest_sessionstart` -- global setup |
| `prepare_environment()` | `pytest_sessionstart` -- directory and service setup |
| `get_testpy_test()` | `testpy_test` fixture -- creates Test instances |
| `SystemResourceMetric` | `_resource_monitor_loop` -- metric records |
| `SQLiteWriter` | `_resource_monitor_loop` -- database writes |

### Key Design Decisions

1. **`TEST_CONFIG_FILENAME` is redefined locally** rather than imported from
   `base.py`. Both `base.py` and `runner.py` define this as `"test_config.yaml"`.

2. **`TestSuiteConfig` vs `TestSuite`**: `TestSuiteConfig` is deliberately
   lightweight -- it only reads YAML and computes disabled tests. Full `TestSuite`
   instances (with cluster pools, artifacts, etc.) are created lazily via
   `get_testpy_test()` in the `testpy_test` fixture, not during collection.

3. **Dynamic fixture scoping**: `testpy_test_fixture_scope` returns `"module"`
   for the pytest runner (both test.py and bare pytest), ensuring each module
   gets its own `Test` instance.  It returns `"session"` for run.py scripts,
   where a single Scylla instance is shared across the entire test session.

4. **xdist awareness**: the plugin handles both xdist workers and the main
   process differently. Workers always initialize globals (separate processes).
   The main process only initializes if test.py hasn't already done so.

5. **Collection multiplexing**: rather than parametrizing tests, the plugin
   intercepts `pytest_collect_file` and creates multiple collectors for the same
   file. This keeps the mode/repeat logic transparent to test authors.

### Data Flow

```
pytest startup
    |
    v
pytest_configure()
    |-- set global config
    |-- configure logging
    |-- validate exe options
    |-- compute build_modes, run_ids
    v
pytest_sessionstart()
    |-- init_testsuite_globals()  ------> TestSuite.artifacts, TestSuite.hosts
    |-- prepare_environment()     ------> directories, 3rd-party services
    |-- _start_resource_watcher() ------> daemon thread polling CPU/memory
    v
pytest_collect_file()  (per file)
    |-- TestSuiteConfig.from_pytest_node()  --> reads test_config.yaml
    |-- filter disabled tests per build_mode
    |-- multiply collectors for (build_mode, run_id) combinations
    |-- assign BUILD_MODE, RUN_ID, TEST_SUITE to each collector stash
    v
pytest_collection_modifyitems()
    |-- modify_pytest_item() per item:
    |     copy stash keys, suffix nodeid/name, process markers
    |-- sort by suite order (run_first promoted)
    v
test execution
    |-- testpy_test fixture  --> get_testpy_test() --> TestSuite.opt_create() + suite.add_test()
    |-- conftest fixtures use testpy_test.suite for clusters, hosts, config
    v
pytest_runtest_makereport()
    |-- capture failure logs to PYTEST_TESTS_LOGS_FOLDER
    v
pytest_runtest_logreport()
    |-- add function_path to JUnit XML
    v
pytest_sessionfinish()
    |-- clean up log files
    |-- _stop_resource_watcher()  ------> stop daemon thread
    |-- TestSuite.artifacts.cleanup_before_exit()
    |-- set EXIT_MAXFAIL_REACHED if applicable
```
