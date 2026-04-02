# Test Suite Framework Design Document

This document describes the test suite framework located in `test/pylib/suite/`.

## Table of Contents

1. [Introduction](#1-introduction)
2. [Architecture Overview](#2-architecture-overview)
3. [Configuration Schema](#3-configuration-schema-test_configyaml)
4. [TestSuite Base Class Contract](#4-testsuite-base-class-contract)
5. [Test Base Class Contract](#5-test-base-class-contract)
6. [Concrete Suite Classes](#6-concrete-suite-classes)
7. [Concrete Test Classes](#7-concrete-test-classes)
8. [Module-Level Infrastructure](#8-module-level-infrastructure)
9. [Integration Points](#9-integration-points)
10. [Data Flow Diagrams](#10-data-flow-diagrams)
11. [Appendix: Configuration Reference](#11-appendix-configuration-reference)

---

## 1. Introduction

The test suite framework provides a unified mechanism for discovering, configuring,
and executing ScyllaDB integration and functional tests across multiple build modes
(`debug`, `release`, `dev`, `sanitize`, `coverage`).

Key characteristics:

- **YAML-driven**: each test directory declares its suite type and configuration in
  a `test_config.yaml` file.
- **Dynamic dispatch**: a factory method reads the YAML `type` field and instantiates
  the correct `TestSuite` subclass at runtime via dynamic import.
- **Three execution modes**: the `test.py` wrapper script, bare pytest with
  the `test/pylib/runner.py` plugin, and `run.py` scripts that start Scylla
  externally and invoke pytest with `SCYLLA_TEST_RUNNER=runpy`.
- **Cluster pooling**: Python-based test suites maintain a pool of reusable
  ScyllaDB cluster instances to amortize startup cost.

The framework lives in 5 files under `test/pylib/suite/`:

| File               | Contents                                                |
|--------------------|---------------------------------------------------------|
| `__init__.py`      | Re-exports concrete suite classes                       |
| `base.py`          | `TestSuite` (ABC), `Test` (ABC), module-level utilities |
| `python.py`        | `PythonTestSuite`, `PythonTest`                         |
| `cql_approval.py`  | `CQLApprovalTestSuite`                                  |
| `topology.py`      | `TopologyTestSuite`, `TopologyTest`                     |

---

## 2. Architecture Overview

### 2.1 Class Hierarchy

```
TestSuite (ABC)                       base.py
  |
  +-- PythonTestSuite                 python.py   (intermediate base)
  |     |
  |     +-- CQLApprovalTestSuite      cql_approval.py
  |     +-- TopologyTestSuite         topology.py


Test (abstract, not ABC)              base.py
  |
  +-- PythonTest                      python.py   (intermediate base)
  |     |
  |     +-- TopologyTest              topology.py
```

`PythonTestSuite` and `PythonTest` are the key intermediate base classes, shared
by `CQLApprovalTestSuite`, `TopologyTestSuite`, and `TopologyTest`.

The `ToolTestSuite`/`ToolTest` and `RunTestSuite`/`RunTest` classes have been
deleted — all directories that previously used `type: Tool` or `type: Run`
now use `type: Python`.

### 2.2 Factory Pattern

Suite instantiation is never done directly by callers. Instead,
`TestSuite.opt_create(config, options, mode)` acts as the single factory:

1. Loads `test_config.yaml` via `load_cfg()`.
2. Reads `cfg["type"]` (e.g. `"Python"`, `"Topology"`, `"Approval"`).
3. Converts the type string to a class name via `suite_type_to_class_name()`:
   - Special case: `"Approval"` (case-insensitive) maps to `"CQLApprovalTestSuite"`.
   - All others: `type.title() + "TestSuite"` (e.g. `"Python"` -> `"PythonTestSuite"`).
4. Dynamically imports the class from the `test.pylib.suite` package
   using `getattr(import_module("test.pylib.suite"), class_name)`.
5. Instantiates it and caches it in `TestSuite.suites` (a class-level dict keyed
   by `path + "/" + mode`), ensuring exactly one suite instance per directory/mode
   combination.

### 2.3 Package Initialization

`test/pylib/suite/__init__.py` re-exports all 5 concrete classes so the dynamic
import in `opt_create()` can resolve them:

- `CQLApprovalTestSuite`
- `PythonTestSuite`
- `RunTestSuite`
- `ToolTestSuite`
- `TopologyTestSuite`

---

## 3. Configuration Schema (`test_config.yaml`)

Each test directory may contain a `test_config.yaml` that configures the suite.
The framework walks up the directory tree from the test file to find the nearest
config.

### 3.1 Complete Key Reference

| Key | Type | Default | Consumed By | Description |
|-----|------|---------|-------------|-------------|
| `type` | `string` | (required) | `TestSuite.opt_create()` | Suite class selector. Values: `Python`, `Topology`, `Approval`. |
| `disable` | `list[string]` | `[]` | `TestSuite.__init__` | Test shortnames to unconditionally disable. |
| `skip_in_<mode>` | `list[string]` | `[]` | `TestSuite.__init__` | Tests to skip in a specific build mode. `<mode>` is one of `debug`, `release`, `dev`, `sanitize`, `coverage`. |
| `skip_in_debug_modes` | `list[string]` | `[]` | `TestSuite.__init__` | Tests to skip in all debug modes (as defined by the `DEBUG_MODES` constant). |
| `run_in_<mode>` | `list[string]` | `[]` | `TestSuite.__init__` | Tests that should only run in a specific mode. A test listed in `run_in_X` but not in `run_in_<current_mode>` is disabled. |
| `run_first` | `list[string]` | `[]` | `TestSuite.__init__` | Tests to prioritize (sorted to the front of the execution list). |
| `no_parallel_cases` | `list[string]` | `[]` | `TestSuite.__init__` | Tests whose cases should not run in parallel. |
| `flaky` | `list[string]` | `[]` | `TestSuite.__init__` | Tests that are known-flaky (retained in config for historical tracking). |
| `coverage` | `bool` | `true` | `TestSuite.need_coverage()` | Whether to enable code coverage for this suite. |
| `cluster` | `mapping` | `{"initial_size": 1}` | `PythonTestSuite.__init__` | Cluster configuration. Sub-key `initial_size` controls the number of nodes. |
| `pool_size` | `int` | `2` | `PythonTestSuite.__init__` | Number of clusters in the reuse pool. Overridden by CLI `--cluster-pool-size` or env `CLUSTER_POOL_SIZE`. |
| `dirties_cluster` | `list[string]` | `[]` | `PythonTestSuite.__init__` | Tests that leave the cluster in a dirty state (requiring recycle). |
| `extra_scylla_cmdline_options` | `list[string]` or `string` | `[]` | `PythonTestSuite.get_cluster_factory()` | Additional Scylla command-line flags. Merged with test-level and CLI-level options. |
| `extra_scylla_config_options` | `mapping` | `{}` | `PythonTestSuite.get_cluster_factory()` | Additional Scylla config file options. Merged with defaults and test-level config. |
| `prepare_cql` | `string` or `list[string]` | `null` | `PythonTest.run_ctx()` | CQL statements to execute once per cluster before tests run. |
| `custom_args` | `mapping[string, list[string]]` | `{}` | (Boost/unit suites, outside this framework) | Per-test custom arguments. Not consumed by the Python suite classes. |

### 3.2 Disabled-Test Resolution Algorithm

The constructor computes `disabled_tests` as the union of:

1. All tests in `cfg["disable"]`.
2. All tests in `cfg["skip_in_<current_mode>"]`.
3. If the current mode is a debug mode: all tests in `cfg["skip_in_debug_modes"]`.
4. For every mode `M` other than the current mode: tests in `cfg["run_in_M"]` that
   are **not** also in `cfg["run_in_<current_mode>"]`.

This means `run_in_<mode>` acts as an opt-in list: if a test appears in any
`run_in_*` directive, it will only run in the modes where it is explicitly listed.
Tests not mentioned in any `run_in_*` directive run in all modes.

---

## 4. TestSuite Base Class Contract

**Location:** `base.py`
**Inherits from:** `ABC`

### 4.1 Class-Level State

| Attribute | Type | Description |
|-----------|------|-------------|
| `suites` | `dict[str, TestSuite]` | Global registry of all suite instances, keyed by `"path/mode"`. Serves as a singleton cache. |
| `artifacts` | `ArtifactRegistry` | Global artifact/cleanup registry. Set once by `init_testsuite_globals()`. |
| `hosts` | `HostRegistry` | Global host/IP registry for leasing network addresses. Set once by `init_testsuite_globals()`. |
| `_next_id` | `defaultdict(int)` | Per-test-key monotonic counter for generating unique IDs. Keyed by tuples whose shape varies by suite type. |

### 4.2 Constructor

**Parameters:** `path: str`, `cfg: dict`, `options: argparse.Namespace`, `mode: str`

Sets the following instance state:

| Variable | Derivation |
|----------|------------|
| `suite_path` | `pathlib.Path(path)` |
| `log_dir` | `options.tmpdir / mode` |
| `name` | Basename of `suite_path` |
| `cfg` | The raw parsed YAML dict |
| `options` | CLI options namespace |
| `mode` | Build mode string |
| `suite_key` | `os.path.join(path, mode)` |
| `tests` | Empty list, populated later by `add_test()` |
| `pending_test_count` | `0`, incremented as tests are added |
| `n_failed` | `0`, incremented as tests fail |
| `run_first_tests` | Set from `cfg["run_first"]` |
| `no_parallel_cases` | Set from `cfg["no_parallel_cases"]` |
| `disabled_tests` | Computed via the algorithm in Section 3.2 |
| `flaky_tests` | Set from `cfg["flaky"]` |
| `base_env` | Base environment dict. If coverage is needed, adds `LLVM_PROFILE_FILE`. |

### 4.3 Abstract Interface

**`pattern`** (read-only property): must return a glob pattern string or a list
of glob pattern strings for discovering test files within the suite directory.

**`add_test(shortname: str, casename: str | None)`** (async): must create a `Test`
subclass instance and append it to `self.tests`.

### 4.4 Concrete Methods

**`next_id(test_key) -> int`**: generates a unique monotonic ID for each test key.
If `options.run_id` is set (pytest mode), uses that fixed value. Otherwise
increments the per-key counter. This ensures repeated runs of the same test
(via `--repeat`) get distinct IDs for result differentiation.

**`test_count() -> int`** (static): returns the total count of all test IDs
generated across all suites.

**`load_cfg(path: Path) -> dict`** (static): loads a YAML file, validates it
produces a dict, and returns it. Raises `RuntimeError` if parsing fails.

**`opt_create(config, options, mode) -> TestSuite`** (static): factory method
described in Section 2.2.

**`all_tests() -> Iterable[Test]`** (static): chains all tests from all
registered suites into a single iterable.

**`need_coverage() -> bool`**: returns `True` if coverage is enabled in options,
the current mode is in the coverage modes, and the suite config does not set
`coverage: false`.

---

## 5. Test Base Class Contract

**Location:** `base.py`
**Note:** Uses `@abstractmethod` decorators but does not inherit from `ABC`.
Enforcement relies on subclass discipline.

### 5.1 Constructor

**Parameters:** `test_no: int`, `shortname: str`, `suite: TestSuite`

Sets the following instance state:

| Variable | Derivation |
|----------|------------|
| `id` | The `test_no` parameter |
| `args` | Empty list (subclasses populate) |
| `core_args` | Empty list (program-required args regardless of test) |
| `valid_exit_codes` | `[0]` |
| `name` | `suite.name + "/" + shortname` (with extension stripped at the dot) |
| `shortname` | The `shortname` parameter |
| `mode` | `suite.mode` |
| `suite` | Back-reference to parent suite |
| `allure_dir` | `suite.log_dir / "allure"` |
| `uname` | Unique name: `"suite.shortname.id"` (with `/` replaced by `_`). If running under xdist, prefixed with the worker ID. |
| `log_filename` | `suite.log_dir / "{uname}.log"` |
| `is_flaky` | Whether `shortname` is in `suite.flaky_tests` |
| `is_flaky_failure` | `False` (set to `True` when retried after flaky failure) |
| `is_cancelled` | `False` (set to `True` on ctrl-c or timeout) |
| `env` | Copy of `suite.base_env` |
| `started` | `False` |
| `success` | `False` |
| `time_start` | `0` |
| `time_end` | `0` |

### 5.2 Abstract Interface

**`print_summary()`**: must print human-readable test results to stdout.

### 5.3 Concrete Methods

**`failed`** (property): `True` if the test was started, did not succeed, and was
not cancelled.

**`did_not_run`** (property): `True` if the test was never started or was cancelled.

---

## 6. Concrete Suite Classes

### 6.1 PythonTestSuite

**Location:** `python.py`
**Inherits from:** `TestSuite`
**Role:** Intermediate base class for all suites that run Python pytest tests
against a live Scylla cluster.

#### Constructor Additions

Beyond the base class, the constructor initializes:

- **`scylla_exe`**: path to the Scylla executable for the current mode, resolved
  via `path_to(mode, "scylla")`.
- **`scylla_env`**: environment dict for Scylla processes. Starts as a copy of
  `base_env`. If the mode is `"coverage"`, adds coverage-specific environment
  variables via `scripts.coverage.env()`. Always sets `SCYLLA` to the executable
  path.
- **`dirties_cluster`**: set of test shortnames from `cfg["dirties_cluster"]`.
  Tests in this set cause their cluster to be marked dirty after execution.
- **`create_cluster`**: an async factory function returned by `get_cluster_factory()`.
- **`clusters`**: a `Pool` instance parameterized with `pool_size`, the
  `create_cluster` factory, and a recycler function. The pool manages cluster
  lifecycle.

The **cluster pool size** is resolved with the following priority:
1. `options.cluster_pool_size` (CLI flag)
2. `CLUSTER_POOL_SIZE` environment variable
3. `cfg["pool_size"]`
4. Default: `2`

The **recycler function** for dirty clusters:
- Closes log files and cleans up maintenance socket directories for each server.
- Stops the cluster.
- Closes the API client and releases its connector resources.
- Releases all leased IPs back to the host registry.

#### Cluster Factory (`get_cluster_factory`)

Returns an async `create_cluster` function that:

1. Defines a `create_server` inner function that constructs a `ScyllaServer` with
   merged options from three sources (increasing priority):
   - Default config options (`PasswordAuthenticator`, `CassandraAuthorizer`,
     `tablets_initial_scale_factor` of 4 for release / 2 otherwise).
   - Suite-level config from `extra_scylla_config_options`.
   - Test-level config (passed via `CreateServerParams`).
   - Command-line options are similarly merged from suite config, test params,
     and CLI `--extra-scylla-cmdline-options`.
2. Creates a `ScyllaCluster` with the host registry and initial cluster size.
3. Registers `stop` as both a suite artifact and an exit artifact.
4. If `save_log_on_success` is false, also registers `uninstall` as a suite artifact.
5. Calls `install_and_start()` on the cluster.
6. If the cluster fails to start, cleans up (stop, close API, release IPs) and
   raises the start exception immediately, preventing the pool from returning a
   broken cluster.

#### Overridden Methods

- **`pattern`**: returns `["*_test.py", "*_tests.py", "test_*.py"]` (three
  naming conventions).
- **`add_test(shortname, casename)`**: creates a `PythonTest` with ID key
  `(shortname, self.suite_key)`.

#### Class-Level Constants

- **`test_file_ext`**: `".py"` -- used by `PythonTest` when constructing the test
  file argument path.

---

### 6.2 CQLApprovalTestSuite

**Location:** `cql_approval.py`
**Inherits from:** `PythonTestSuite`
**Role:** Runs CQL approval/comparison tests (`.cql` files) against a single
Scylla instance.

This is the most minimal subclass. It overrides only two things:

- **`test_file_ext`**: `".cql"` (overriding `".py"` from `PythonTestSuite`).
- **`pattern`**: returns `"*_test.cql"` (using the `CQL_TEST_SUFFIX` constant
  imported from `test.pylib.cql_repl`).

It inherits `add_test()` from `PythonTestSuite`, so it creates `PythonTest`
instances (not a CQL-specific test class). The `test_file_ext` attribute is what
tells `PythonTest._prepare_pytest_params()` to look for `.cql` files instead of
`.py` files.

---

### 6.3 TopologyTestSuite

**Location:** `topology.py`
**Inherits from:** `PythonTestSuite`
**Role:** Runs topology change tests that use a cluster manager for full lifecycle
control.

Has no constructor of its own -- inherits `PythonTestSuite.__init__` entirely,
including cluster pool, scylla executable, and coverage env setup.

#### Overridden Methods

- **`add_test(shortname, casename)`**: creates a `TopologyTest` (not `PythonTest`).
  Note: narrows the `casename` parameter type from `str | None` (base class) to `str`.
  Uses a different ID key tuple: `(shortname, 'topology', self.mode)` instead of
  `(shortname, self.suite_key)`. This means topology test IDs are shared across
  suite paths but unique per mode.

---


## 7. Concrete Test Classes

### 7.1 PythonTest

**Location:** `python.py`
**Inherits from:** `Test`
**Role:** Intermediate base class for tests that run via pytest against a
Scylla cluster.

#### Constructor Additions

- **`path`**: set to `"python"` (the Python interpreter).
- **`core_args`**: set to `["-m", "pytest"]` (invoke pytest as a module).
- **`casename`**: optional case name within the test file (from `::` syntax).
- **`xmlout`**: path for JUnit XML output: `suite.log_dir / "xml" / "{uname}.xunit.xml"`.
- **`server_address`**: `None` initially; set to the cluster endpoint before test
  execution.
- **`server_log`** / **`server_log_filename`**: `None` initially.
  Only `server_log_filename` is populated from the cluster during `run_ctx()`;
  `server_log` is never assigned a non-None value.
- **`is_before_test_ok`** / **`is_after_test_ok`**: `False`; lifecycle flags to
  distinguish pre-test failures from test failures from post-test failures.

#### Key Methods

**`_prepare_pytest_params(options)`**: builds the comprehensive pytest command-line
argument list. Includes:
- `-s` (no output capture), `--log-level=DEBUG`, `-vv` (verbose)
- JUnit XML output config (`junit_family=xunit2`, `junit_suite_name`, `--junit-xml`)
- `-rs` (show reasons for skipped tests)
- `--run_id`, `--mode`, `--tmpdir`
- `--gather-metrics` (if enabled)
- `--alluredir` and `--allure-no-capture` / `--save-log-on-success`
- `-m=<markers>` (if set). When markers are active, exit code 5 (no tests
  selected) is also treated as valid.
- `--pytest_arg` (additional user-specified pytest arguments, shell-split)
- The test file path, constructed as `suite_path / shortname + suite.test_file_ext`.
  If `casename` is set, appends `::casename`.

**`run_ctx(options)`** (async context manager): the setup/teardown lifecycle
for a pool-based test:

1. Calls `_prepare_pytest_params()`.
2. Leases a cluster from `suite.clusters` pool via `await pool.get(logger)`.
3. Calls `cluster.before_test(uname)`.
4. If `prepare_cql` is configured and not yet executed for this cluster, runs
   the CQL statements via the first server's control connection. Marks
   `cluster.prepare_cql_executed` so they are not re-run.
5. Sets `server_address` to `cluster.endpoint()` and inserts `--host` and
   `--scylla-log-filename` arguments at the front of the args list.
6. Takes a log savepoint on the cluster.
7. Yields to the test body.
8. After the test: if the shortname is in `dirties_cluster`, marks the cluster
   dirty. Calls `cluster.after_test(uname, success)`.
9. On exception during setup or teardown: marks cluster dirty, logs diagnostic
   info about whether the failure was pre-test or post-test.
10. In `finally`: returns the cluster to the pool via `pool.put(cluster, is_dirty)`.

**`print_summary()`**: prints the test command and output log. If `server_log`
is set (non-None), also prints the first server's log.

---

### 7.2 TopologyTest

**Location:** `topology.py`
**Inherits from:** `PythonTest`
**Role:** Runs topology tests using a cluster manager instead of the shared pool.

Has a `status: bool` type annotation at class level (not initialized in
constructor).

The constructor simply delegates to `super().__init__()`.

---

## 8. Module-Level Infrastructure

All module-level functions and utilities are in `base.py` unless noted otherwise.

### 8.1 Global Initialization (`init_testsuite_globals`)

Creates the global `ArtifactRegistry` and `HostRegistry` instances and assigns
them to `TestSuite.artifacts` and `TestSuite.hosts` respectively. Must be called
once before any suite is used. Called from both `test.py` and `runner.py` during
session setup.

### 8.2 Environment Preparation

**`prepare_environment(tempdir_base, modes, gather_metrics, save_log_on_success, toxiproxy_byte_limit)`**:
Decorated with `@universalasync.async_to_sync_wraps` (can be called from sync
code). Calls `prepare_dirs()` then `start_3rd_party_services()`.

**`prepare_dirs(tempdir_base, modes, gather_metrics, save_log_on_success)`**:
- Sets up cgroups via `setup_cgroup()`.
- Prepares directories for logs, reports, LDAP instances.
- For each mode: creates directories for mode logs, `.reject` files, XML output,
  failed test artifacts, allure reports, and (if not using pytest runner) pytest
  output.
- If `save_log_on_success` is false, cleans old artifacts.

**`start_3rd_party_services(tempdir_base, toxiproxy_byte_limit)`** (async):
Starts four external services required by integration tests:
1. **LDAP server**: via `start_ldap()`. Registered as an exit artifact.
2. **MinIO server**: object storage. Registered as an exit artifact.
3. **Mock S3 server**: on port 2012. Registered as an exit artifact.
4. **S3 Proxy server**: on port 9002, proxying to MinIO with configurable retries
   and a random seed. Registered as an exit artifact.

All services lease their own IP addresses from the host registry.

### 8.3 Suite Config Lookup (`find_suite_config`)

**Signature:** `find_suite_config(path, config_filename) -> pathlib.Path`

Walks up the directory tree from `path` (relative to `TEST_DIR`) looking for a
file named `config_filename`. Returns the first match. Raises `FileNotFoundError`
if none is found up to the test root.

### 8.4 Single-Test Creation Bridge (`get_testpy_test`)

**Signature:** `async get_testpy_test(path, options, mode) -> Test`

Creates a single `Test` instance for a given file path:

1. Finds the suite config using `TEST_CONFIG_FILENAME` (`"test_config.yaml"`).
2. Creates/retrieves the suite via `TestSuite.opt_create()`.
3. If `options.exe_path` or `options.exe_url` is set, overrides `suite.scylla_exe`.
4. Calls `suite.add_test(shortname, casename=None)`.
5. Returns `suite.tests[-1]` (the newly added test).

This function is the primary bridge between the pytest fixture system and the
suite framework.

Uses `TEST_CONFIG_FILENAME` directly (no `suite.yaml` fallback).

### 8.5 Terminal Color Palette (`palette`)

A namespace class (not instantiated) providing color formatters for terminal
output. Each attribute is a `Callable[[Any], str]` that wraps its argument in
ANSI color codes if stdout is a TTY, or returns a plain string otherwise.

| Attribute | Color/Style |
|-----------|-------------|
| `ok` | Green, bright |
| `fail` | Red, bright |
| `new` | Blue |
| `skip` | Dim |
| `path` | Bright |
| `diff_in` | Green |
| `diff_out` | Red |
| `diff_mark` | Magenta |
| `warn` | Yellow |
| `crit` | Red, bright |

Also provides `nocolor(text)` static method that strips ANSI escape codes using
a compiled regex.

The formatters are created by `create_formatter(*decorators)`, a factory function
that returns either a colorizing function or a plain `str()` wrapper depending on
whether stdout is a TTY.

### 8.6 Log Reading (`read_log`)

**Signature:** `read_log(log_filename: pathlib.Path) -> str`

Reads a log file and returns its contents. Returns descriptive placeholder strings
if the file is not found (`"===Log {path} not found==="`), empty
(`"===Empty log output==="`), or unreadable due to OS errors.

### 8.7 Module Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `TEST_CONFIG_FILENAME` | `"test_config.yaml"` | Current config file name |
| `PYTEST_TESTS_LOGS_FOLDER` | `"pytest_tests_logs"` | Subdirectory for pytest log files |
| `output_is_a_tty` | `sys.stdout.isatty()` | Cached TTY detection |

---

## 9. Integration Points

### 9.1 CI Entry Point (`test.py`)

The `test.py` script at the repository root is the CI entry point for running tests.

`test.py` is a thin wrapper around `run_pytest()`:

1. Parses arguments.
2. Initializes suite globals and prepares the environment.
3. Calls `run_pytest()` via `run_all_tests()`.
4. Prints summary and handles coverage.

### 9.2 Pytest Plugin (`test/pylib/runner.py`)

The pytest-based runner integrates with the suite framework via:

**`testpy_test` fixture** (module-scoped): calls `get_testpy_test()` to create
a `Test` instance for the current test file. This is how the conftest files in
each test directory access the suite's cluster pool and configuration.

**`TestSuiteConfig`**: a lightweight parallel implementation used during pytest
collection (not test execution). It reads `test_config.yaml` to filter disabled
tests and apply mode restrictions without creating full `TestSuite` instances.
It walks up the directory tree to find the config file, similar to
`find_suite_config()`.

**Session setup**: calls `init_testsuite_globals()` and `prepare_environment()`
during pytest session start (gated by `--test-py-init` flag).

> **Note:** `runner.py` is only loaded as a pytest plugin when
> `TEST_RUNNER != "runpy"`.  In runpy mode (`test/cqlpy/run`,
> `test/alternator/run`, `test/rest_api/run`), `test/conftest.py` skips loading
> runner.py and provides its own session-scoped `testpy_test` fixture that
> returns `None`.  However, functions defined in runner.py (such as
> `testpy_test_fixture_scope`) are still importable via regular Python imports
> and are used by conftest files for fixture scoping.

### 9.3 Conftest Files

Each test directory has a `conftest.py` that bridges pytest fixtures to the suite
infrastructure:

| Directory | Key Integration |
|-----------|-----------------|
| `test/cqlpy/` | Uses `testpy_test` for cluster access via `run_ctx()` |
| `test/cluster/` | Uses `get_testpy_test()` to create a `ManagerClient` from the suite's cluster pool |
| `test/cql/` | Uses `get_testpy_test()` to get `output_path` from the suite's log dir |
| `test/alternator/` | Uses `add_host_option` |
| `test/rest_api/` | Uses `add_host_option` and `add_cql_connection_options` |
| `test/broadcast_tables/` | Uses `add_host_option` and `add_cql_connection_options` |
| `test/scylla_gdb/` | Uses `testpy_test` for cluster access via `run_ctx()` |

### 9.4 Artifact Lifecycle

The `ArtifactRegistry` manages two levels of cleanup:

- **Suite artifacts**: registered via `add_suite_artifact(suite, fn)`. Cleaned up
  when all tests in a suite complete. Includes cluster `stop` and `uninstall`.
- **Exit artifacts**: registered via `add_exit_artifact(suite, fn)`. Cleaned up
  at process exit. Includes third-party services (LDAP, MinIO, S3 mock, S3 proxy)
  and cluster `stop` as a safety net.

---

## 10. Data Flow Diagrams

### 10.1 Test Discovery (Pytest Path)

```
test_config.yaml
       |
       | TestSuiteConfig.from_pytest_node()
       v
   TestSuiteConfig (lightweight, collection-time only)
       |
       | is_test_disabled(build_mode, file_path)
       | filter disabled tests during collection
       v
   pytest_collect_file() multiplexes across (build_mode, run_id)
       |
       v
   pytest_collection_modifyitems()
       | suffix nodeid with .mode.run_id
       | sort by suite order (run_first promoted)
       v
   collected items ready for execution
```

### 10.2 Test Execution (Pytest Path)

```
testpy_test fixture
       |
       | get_testpy_test(path, options, mode)
       |   |
       |   | find_suite_config(path, TEST_CONFIG_FILENAME)
       |   | TestSuite.opt_create()  --> creates/caches TestSuite subclass
       |   | suite.add_test()        --> creates Test instance
       |   v
       | Test instance (with suite back-reference)
       |
       v
   conftest fixtures use testpy_test.suite for:
       | clusters pool, host registry, configuration
       v
   test function executes
       |
       v
   pytest_sessionfinish
       | TestSuite.artifacts.cleanup_before_exit()
```

### 10.3 Cluster Lifecycle (PythonTestSuite)

```
PythonTestSuite.__init__()
       |
       | Pool(pool_size, create_cluster, recycle_cluster)
       v
   clusters Pool
       |
       | test requests cluster via pool.get(logger)
       |   |
       |   | Pool has available cluster? --> return it
       |   | Pool empty? --> create_cluster(logger)
       |   |   |
       |   |   | ScyllaCluster(hosts, size, create_server)
       |   |   | register stop/uninstall as artifacts
       |   |   | cluster.install_and_start()
       |   |   | if start_exception: cleanup + raise
       |   |   v
       |   |   new ScyllaCluster
       |   v
       | cluster.before_test(uname)
       | execute prepare_cql (once per cluster)
       | set server_address, log filename
       | take log savepoint
       |
       | --- test executes ---
       |
       | if shortname in dirties_cluster: cluster.is_dirty = True
       | cluster.after_test(uname, success)
       | pool.put(cluster, is_dirty)
       |   |
       |   | is_dirty? --> recycle_cluster(cluster)
       |   |   |
       |   |   | close log files
       |   |   | cluster.stop()
       |   |   | close API client
       |   |   | release IPs
       |   |   | create fresh replacement via create_cluster
       |   |   v
       |   | not dirty? --> return to pool for reuse
       v
```

---

## 11. Appendix: Configuration Reference

### 11.1 Type-to-Class Mapping

| YAML `type` | Class Name | Special Handling |
|--------------|------------|------------------|
| `Python` | `PythonTestSuite` | `"Python".title()` + `"TestSuite"` |
| `Topology` | `TopologyTestSuite` | `"Topology".title()` + `"TestSuite"` |
| `Approval` | `CQLApprovalTestSuite` | Special case: `"Approval"` -> `"CQLApproval"` + `"TestSuite"` |

### 11.2 Active Configuration Files

The following `test_config.yaml` files have a `type` field and are processed by
the suite framework:

| Path | Type | Notable Config |
|------|------|----------------|
| `test/cqlpy/test_config.yaml` | `Python` | `dirties_cluster: [test_native_transport]`, extra cmdline for tablets and UDF |
| `test/rest_api/test_config.yaml` | `Python` | Extra cmdline for UDF and tablets |
| `test/alternator/test_config.yaml` | `Python` | Extensive config options for Alternator (streams, TTL, authorization, ports) |
| `test/broadcast_tables/test_config.yaml` | `Python` | Experimental broadcast-tables feature flag |
| `test/nodetool/test_config.yaml` | `Python` | `coverage: false` |
| `test/scylla_gdb/test_config.yaml` | `Python` | Minimal (type only) |
| `test/cluster/test_config.yaml` | `Topology` | `pool_size: 4`, `cluster.initial_size: 0`, extensive `run_first`/`skip_in_*`/`run_in_*` lists |
| `test/cql/test_config.yaml` | `Approval` | Extra cmdline for compact storage |

The following `test_config.yaml` files have **no `type` field** and are used by
other parts of the test infrastructure (e.g. Boost/unit test runner):

- `test/boost/test_config.yaml`
- `test/unit/test_config.yaml`
- `test/raft/test_config.yaml`
- `test/ldap/test_config.yaml`
- `test/vector_search/test_config.yaml`

No `suite.yaml` files currently exist in the project -- the migration to
`test_config.yaml` is complete.

### 11.3 Summary of Suite Characteristics

| Suite Class | Pattern | Test Class | Has Scylla Exe | Has Cluster Pool | Gentle Kill |
|-------------|---------|------------|----------------|------------------|-------------|
| `PythonTestSuite` | `*_test.py`, `*_tests.py`, `test_*.py` | `PythonTest` | Yes | Yes | No |
| `CQLApprovalTestSuite` | `*_test.cql` | `PythonTest` | Yes (inherited) | Yes (inherited) | No |
| `TopologyTestSuite` | (inherited from Python) | `TopologyTest` | Yes (inherited) | Yes (inherited, used by manager) | No |
| `ToolTestSuite` | `*_test.py`, `test_*.py` | `ToolTest` | No | No | No |
| `RunTestSuite` | `run` | `RunTest` | Yes | No | Yes |
