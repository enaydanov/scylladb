# Test Suite Framework Design Document

This document describes the test suite framework located in `test/pylib/suite.py`.

## Table of Contents

1. [Introduction](#1-introduction)
2. [Architecture Overview](#2-architecture-overview)
3. [Configuration Schema](#3-configuration-schema-test_configyaml)
4. [TestSuite Class Contract](#4-testsuite-class-contract)
5. [Test Class Contract](#5-test-class-contract)
6. [Module-Level Infrastructure](#6-module-level-infrastructure)
7. [Integration Points](#7-integration-points)
8. [Data Flow Diagrams](#8-data-flow-diagrams)
9. [Appendix: Configuration Reference](#9-appendix-configuration-reference)

---

## 1. Introduction

The test suite framework provides a unified mechanism for discovering, configuring,
and executing ScyllaDB integration and functional tests across multiple build modes
(`debug`, `release`, `dev`, `sanitize`, `coverage`).

Key characteristics:

- **YAML-driven**: each test directory declares its configuration in
  a `test_config.yaml` file.
- **Single factory**: `TestSuite.opt_create()` loads the YAML config and
  instantiates a `TestSuite` directly (one instance per path/mode).
- **Three execution modes**: the `test.py` wrapper script, bare pytest with
  the `test/pylib/runner.py` plugin, and `run.py` scripts that start Scylla
  externally and invoke pytest with `SCYLLA_TEST_RUNNER=runpy`.
- **Cluster pooling**: test suites maintain a pool of reusable ScyllaDB cluster
  instances to amortize startup cost.

The framework lives in a single module:

| File               | Contents                                                |
|--------------------|---------------------------------------------------------|
| `suite.py`         | `TestSuite`, `Test`, module-level utilities             |

---

## 2. Architecture Overview

### 2.1 Class Structure

```
TestSuite                             suite.py
Test                                  suite.py
```

`TestSuite` is the only suite class.  `Test` is the only test class.
All test directories use `test_config.yaml` with no `type` field required.

### 2.2 Factory Pattern

Suite instantiation is never done directly by callers. Instead,
`TestSuite.opt_create(config, options, mode)` acts as the single factory:

1. Loads `test_config.yaml` via `load_cfg()`.
2. Instantiates `TestSuite(path, cfg, options, mode)`.
3. Caches the instance in `TestSuite.suites` (a class-level dict keyed
   by `path + "/" + mode`), ensuring exactly one suite instance per directory/mode
   combination.

---

## 3. Configuration Schema (`test_config.yaml`)

Each test directory may contain a `test_config.yaml` that configures the suite.
The framework walks up the directory tree from the test file to find the nearest
config.

### 3.1 Complete Key Reference

| Key | Type | Default | Consumed By | Description |
|-----|------|---------|-------------|-------------|
| `disable` | `list[string]` | `[]` | `TestSuiteConfig` (runner.py) | Test shortnames to unconditionally disable. |
| `skip_in_<mode>` | `list[string]` | `[]` | `TestSuiteConfig` (runner.py) | Tests to skip in a specific build mode. `<mode>` is one of `debug`, `release`, `dev`, `sanitize`, `coverage`. |
| `skip_in_debug_modes` | `list[string]` | `[]` | `TestSuiteConfig` (runner.py) | Tests to skip in all debug modes (as defined by the `DEBUG_MODES` constant). |
| `run_in_<mode>` | `list[string]` | `[]` | `TestSuiteConfig` (runner.py) | Tests that should only run in a specific mode. A test listed in `run_in_X` but not in `run_in_<current_mode>` is disabled. |
| `run_first` | `list[string]` | `[]` | `TestSuiteConfig` (runner.py) | Tests to prioritize (sorted to the front of the execution list). |
| `no_parallel_cases` | `list[string]` | `[]` | `TestSuiteConfig` (runner.py) | Tests whose cases should not run in parallel. |
| `flaky` | `list[string]` | `[]` | `TestSuiteConfig` (runner.py) | Tests that are known-flaky (retained in config for historical tracking). |
| `coverage` | `bool` | `true` | `TestSuite.need_coverage()` | Whether to enable code coverage for this suite. |
| `cluster` | `mapping` | `{"initial_size": 1}` | `TestSuite.__init__` | Cluster configuration. Sub-key `initial_size` controls the number of nodes. |
| `pool_size` | `int` | `2` | `TestSuite.__init__` | Number of clusters in the reuse pool. Overridden by CLI `--cluster-pool-size` or env `CLUSTER_POOL_SIZE`. |
| `dirties_cluster` | `list[string]` | `[]` | `TestSuite.__init__` | Tests that leave the cluster in a dirty state (requiring recycle). |
| `extra_scylla_cmdline_options` | `list[string]` or `string` | `[]` | `TestSuite.get_cluster_factory()` | Additional Scylla command-line flags. Merged with test-level and CLI-level options. |
| `extra_scylla_config_options` | `mapping` | `{}` | `TestSuite.get_cluster_factory()` | Additional Scylla config file options. Merged with defaults and test-level config. |
| `prepare_cql` | `string` or `list[string]` | `null` | `Test.run_ctx()` | CQL statements to execute once per cluster before tests run. |
| `custom_args` | `mapping[string, list[string]]` | `{}` | (Boost/unit suites, outside this framework) | Per-test custom arguments. Not consumed by the Python suite classes. |

### 3.2 Disabled-Test Resolution Algorithm

The `TestSuiteConfig` class in `runner.py` computes `disabled_tests` as the union of:

1. All tests in `cfg["disable"]`.
2. All tests in `cfg["skip_in_<current_mode>"]`.
3. If the current mode is a debug mode: all tests in `cfg["skip_in_debug_modes"]`.
4. For every mode `M` other than the current mode: tests in `cfg["run_in_M"]` that
   are **not** also in `cfg["run_in_<current_mode>"]`.

This means `run_in_<mode>` acts as an opt-in list: if a test appears in any
`run_in_*` directive, it will only run in the modes where it is explicitly listed.
Tests not mentioned in any `run_in_*` directive run in all modes.

---

## 4. TestSuite Class Contract

**Location:** `suite.py`

### 4.1 Class-Level State

| Attribute | Type | Description |
|-----------|------|-------------|
| `suites` | `dict[str, TestSuite]` | Global registry of all suite instances, keyed by `"path/mode"`. Serves as a singleton cache. |
| `artifacts` | `ArtifactRegistry` | Global artifact/cleanup registry. Set once by `init_testsuite_globals()`. |
| `hosts` | `HostRegistry` | Global host/IP registry for leasing network addresses. Set once by `init_testsuite_globals()`. |
| `_next_id` | `defaultdict(int)` | Per-test-key monotonic counter for generating unique IDs. |

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
| `base_env` | Base environment dict. If coverage is needed, adds `LLVM_PROFILE_FILE`. |
| `scylla_exe` | Path to the Scylla executable for the current mode, resolved via `path_to(mode, "scylla")`. |
| `dirties_cluster` | Set of test shortnames from `cfg["dirties_cluster"]`. Tests in this set cause their cluster to be marked dirty after execution. |
| `create_cluster` | An async factory function returned by `get_cluster_factory()`. |
| `clusters` | A `Pool` instance parameterized with `pool_size`, the `create_cluster` factory, and a recycler function. |

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

### 4.3 Concrete Methods

**`next_id(test_key) -> int`**: generates a unique monotonic ID for each test key.
If `options.run_id` is set (pytest mode), uses that fixed value. Otherwise
increments the per-key counter. This ensures repeated runs of the same test
(via `--repeat`) get distinct IDs for result differentiation.

**`load_cfg(path: Path) -> dict`** (static): loads a YAML file, validates it
produces a dict, and returns it. Raises `RuntimeError` if parsing fails.

**`opt_create(config, options, mode) -> TestSuite`** (static): factory method
described in Section 2.2.

**`need_coverage() -> bool`**: returns `True` if coverage is enabled in options,
the current mode is in the coverage modes, and the suite config does not set
`coverage: false`.

### 4.4 Cluster Factory (`get_cluster_factory`)

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

---

## 5. Test Class Contract

**Location:** `suite.py`

### 5.1 Constructor

**Parameters:** `test_no: int`, `shortname: str`, `suite: TestSuite`

Sets the following instance state:

| Variable | Derivation |
|----------|------------|
| `id` | The `test_no` parameter |
| `name` | `suite.name + "/" + shortname` (with extension stripped at the dot) |
| `shortname` | The `shortname` parameter |
| `mode` | `suite.mode` |
| `suite` | Back-reference to parent suite |
| `uname` | Unique name: `"suite.shortname.id"` (with `/` replaced by `_`). If running under xdist, prefixed with the worker ID. |
| `success` | `False` |
| `time_start` | `0` |
| `time_end` | `0` |
| `server_address` | `None` initially; set to the cluster endpoint before test execution |
| `server_log_filename` | `None` initially; populated from the cluster during `run_ctx()` |
| `is_before_test_ok` / `is_after_test_ok` | `False`; lifecycle flags to distinguish pre-test failures from test failures from post-test failures |

### 5.2 Key Methods

**`run_ctx()`** (async context manager): the setup/teardown lifecycle
for a pool-based test:

1. Leases a cluster from `suite.clusters` pool via `await pool.get(logger)`.
2. Calls `cluster.before_test(uname)`.
3. If `prepare_cql` is configured and not yet executed for this cluster, runs
   the CQL statements via the first server's control connection. Marks
   `cluster.prepare_cql_executed` so they are not re-run.
4. Sets `server_address` to `cluster.endpoint()` and `server_log_filename` to
   `cluster.server_log_filename()`.
5. Takes a log savepoint on the cluster.
6. Yields to the test body.
7. After the test: if the shortname is in `dirties_cluster`, marks the cluster
   dirty. Calls `cluster.after_test(uname, success)`.
8. On exception during setup or teardown: marks cluster dirty, logs diagnostic
   info about whether the failure was pre-test or post-test.
9. In `finally`: returns the cluster to the pool via `pool.put(cluster, is_dirty)`.

---

## 6. Module-Level Infrastructure

All module-level functions and utilities are in `suite.py`.

### 6.1 Global Initialization (`init_testsuite_globals`)

Creates the global `ArtifactRegistry` and `HostRegistry` instances and assigns
them to `TestSuite.artifacts` and `TestSuite.hosts` respectively. Must be called
once before any suite is used. Called from both `test.py` and `runner.py` during
session setup.

### 6.2 Environment Preparation

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

### 6.3 Suite Config Lookup (`find_suite_config`)

**Signature:** `find_suite_config(path, config_filename) -> pathlib.Path`

Walks up the directory tree from `path` (relative to `TEST_DIR`) looking for a
file named `config_filename`. Returns the first match. Raises `FileNotFoundError`
if none is found up to the test root.

### 6.4 Single-Test Creation Bridge (`get_testpy_test`)

**Signature:** `async get_testpy_test(path, options, mode) -> Test`

Creates a single `Test` instance for a given file path:

1. Finds the suite config using `TEST_CONFIG_FILENAME` (`"test_config.yaml"`).
2. Creates/retrieves the suite via `TestSuite.opt_create()`.
3. If `options.exe_path` or `options.exe_url` is set, overrides `suite.scylla_exe`.
4. Creates a `Test` instance directly with a monotonic ID from `suite.next_id()`.
5. Returns the new `Test` instance.

This function is the primary bridge between the pytest fixture system and the
suite framework.

Uses `TEST_CONFIG_FILENAME` directly (no `suite.yaml` fallback).

### 6.5 Terminal Color Palette (`palette`)

A namespace class (not instantiated) providing color formatters for terminal
output. Each attribute is a `Callable[[Any], str]` that wraps its argument in
ANSI color codes if stdout is a TTY, or returns a plain string otherwise.

| Attribute | Color/Style |
|-----------|-------------|
| `fail` | Red, bright |
| `diff_in` | Green |
| `diff_out` | Red |
| `diff_mark` | Magenta |

The formatters are created by `create_formatter(*decorators)`, a factory function
that returns either a colorizing function or a plain `str()` wrapper depending on
whether stdout is a TTY.

### 6.6 Module Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `TEST_CONFIG_FILENAME` | `"test_config.yaml"` | Current config file name |
| `PYTEST_TESTS_LOGS_FOLDER` | `"pytest_tests_logs"` | Subdirectory for pytest log files |
| `output_is_a_tty` | `sys.stdout.isatty()` | Cached TTY detection |

---

## 7. Integration Points

### 7.1 CI Entry Point (`test.py`)

The `test.py` script at the repository root is the CI entry point for running tests.

`test.py` is a thin compatibility wrapper that delegates to `runner.py`:

1. Parses arguments (forwarding unknown args to pytest).
2. Calls `run_pytest()` which invokes pytest with `runner.py` as its plugin.
3. Returns the pytest exit code.

### 7.2 Pytest Plugin (`test/pylib/runner.py`)

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
during pytest session start (gated by `TESTPY_PREPARED_ENVIRONMENT` env var
to prevent double-initialization when test.py has already prepared the environment).

> **Note:** `runner.py` is only loaded as a pytest plugin when
> `TEST_RUNNER != "runpy"`.  In runpy mode (`test/cqlpy/run`,
> `test/alternator/run`, `test/rest_api/run`), `test/conftest.py` skips loading
> runner.py and provides its own session-scoped `testpy_test` fixture that
> returns `None`.  However, functions defined in runner.py (such as
> `testpy_test_fixture_scope`) are still importable via regular Python imports
> and are used by conftest files for fixture scoping.

### 7.3 Conftest Files

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

### 7.4 Artifact Lifecycle

The `ArtifactRegistry` manages two levels of cleanup:

- **Suite artifacts**: registered via `add_suite_artifact(suite, fn)`. Cleaned up
  when all tests in a suite complete. Includes cluster `stop` and `uninstall`.
- **Exit artifacts**: registered via `add_exit_artifact(suite, fn)`. Cleaned up
  at process exit. Includes third-party services (LDAP, MinIO, S3 mock, S3 proxy)
  and cluster `stop` as a safety net.

---

## 8. Data Flow Diagrams

### 8.1 Test Discovery (Pytest Path)

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

### 8.2 Test Execution (Pytest Path)

```
testpy_test fixture
       |
       | get_testpy_test(path, options, mode)
       |   |
       |   | find_suite_config(path, TEST_CONFIG_FILENAME)
       |   | TestSuite.opt_create()  --> creates/caches TestSuite
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

### 8.3 Cluster Lifecycle

```
TestSuite.__init__()
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

## 9. Appendix: Configuration Reference

### 9.1 Active Configuration Files

The following `test_config.yaml` files are processed by the suite framework:

| Path | Notable Config |
|------|----------------|
| `test/cqlpy/test_config.yaml` | `dirties_cluster: [test_native_transport]`, extra cmdline for tablets and UDF |
| `test/rest_api/test_config.yaml` | Extra cmdline for UDF and tablets |
| `test/alternator/test_config.yaml` | Extensive config options for Alternator (streams, TTL, authorization, ports) |
| `test/broadcast_tables/test_config.yaml` | Experimental broadcast-tables feature flag |
| `test/nodetool/test_config.yaml` | `coverage: false` |
| `test/scylla_gdb/test_config.yaml` | Minimal (empty dict) |
| `test/cluster/test_config.yaml` | `pool_size: 4`, `cluster.initial_size: 0`, extensive `run_first`/`skip_in_*`/`run_in_*` lists |
| `test/cql/test_config.yaml` | Extra cmdline for compact storage |

The following `test_config.yaml` files have **no `type` field** and are used by
other parts of the test infrastructure (e.g. Boost/unit test runner):

- `test/boost/test_config.yaml`
- `test/unit/test_config.yaml`
- `test/raft/test_config.yaml`
- `test/ldap/test_config.yaml`
- `test/vector_search/test_config.yaml`

No `suite.yaml` files currently exist in the project -- the migration to
`test_config.yaml` is complete.

### 9.2 Summary of Suite Characteristics

| Suite Class | Test Class | Has Scylla Exe | Has Cluster Pool |
|-------------|------------|----------------|------------------|
| `TestSuite` | `Test` | Yes | Yes |
