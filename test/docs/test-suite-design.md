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
- **Single factory**: `TestSuite.opt_create()` accepts a `TestSuiteConfig`
  (which holds the already-parsed YAML) and instantiates a `TestSuite` directly
  (one instance per path/mode).
- **Three execution modes**: the `test.py` wrapper script, bare pytest with
  the `test/pylib/runner.py` plugin, and `run.py` scripts that start Scylla
  externally and invoke pytest with `SCYLLA_TEST_RUNNER=runpy`.
- **Cluster pooling**: test suites maintain a pool of reusable ScyllaDB cluster
  instances to amortize startup cost.

The framework lives in a single module:

| File               | Contents                                                |
|--------------------|---------------------------------------------------------|
| `suite.py`         | `TestSuite`, `Test`                                     |
| `terminal.py`      | `output_is_a_tty`, `create_formatter()`, `palette`      |

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
`TestSuite.opt_create(suite_config, options, mode)` acts as the single factory:

1. Takes a `TestSuiteConfig` (from `runner.py`) that already holds the parsed
   YAML config and the CLI-merged `extra_scylla_cmdline_options`.
2. Instantiates `TestSuite(path, suite_config.cfg, options, mode)`.
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
| `extra_scylla_cmdline_options` | `list[string]` or `string` | `[]` | `TestSuite.create_cluster()` → `ScyllaCluster.add_server()` | Additional Scylla command-line flags. Merged with test-level and CLI-level options. |
| `extra_scylla_config_options` | `mapping` | `{}` | `TestSuite.create_cluster()` → `ScyllaCluster.add_server()` | Additional Scylla config file options. Merged with defaults and test-level config. |
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
| `base_env` | Base environment dict. If coverage is needed, adds `LLVM_PROFILE_FILE`. |
| `scylla_exe` | Path to the Scylla executable for the current mode, resolved via `path_to(mode, "scylla")`. |

Note: `clusters` is a `@cached_property` (see Section 4.4), not set in `__init__`.

### 4.3 Concrete Methods

**`opt_create(suite_config, options, mode) -> TestSuite`** (static): factory method
described in Section 2.2. Takes a `TestSuiteConfig` from `runner.py` rather than
a raw file path, eliminating double YAML reads.

**`need_coverage() -> bool`**: returns `True` if coverage is enabled in options,
the current mode is in the coverage modes, and the suite config does not set
`coverage: false`.

### 4.4 Cluster Pool and Creation

**`clusters`** (`@cached_property` → `Pool`): lazily creates the cluster pool
on first access. The **pool size** is resolved with the following priority:
1. `options.cluster_pool_size` (CLI flag)
2. `CLUSTER_POOL_SIZE` environment variable
3. `cfg["pool_size"]`
4. Default: `2`

The pool's recycle callback delegates to `ScyllaCluster.recycle()`, which:
- Closes log files and cleans up maintenance socket directories for each server.
- Stops the cluster.
- Releases all leased IPs back to the host registry.

**`create_cluster(logger)`** (async method): the pool's build callback:

1. Creates a `ScyllaCluster` with the suite's config (host registry, initial
   cluster size, mode, command-line options, config options, environment).
   Server creation logic (command-line merging, config assembly, `ScyllaServer`
   construction) lives in `ScyllaCluster.add_server()`.
2. Registers `cluster.stop` as both a suite artifact and an exit artifact.
3. If `save_log_on_success` is false, also registers `cluster.uninstall` as a
   suite artifact.
4. Calls `install_and_start()` on the cluster.
5. If the cluster fails to start, cleans up (stop, close API, release IPs) and
   raises the start exception immediately, preventing the pool from returning a
   broken cluster.

---

## 5. Test Class Contract

**Location:** `suite.py`

### 5.1 Constructor

**Parameters:** `shortname: str`, `suite: TestSuite`, `run_id: int`

Sets the following instance state:

| Variable | Derivation |
|----------|------------|
| `id` | Set directly from the `run_id` parameter (passed by the `testpy_test` fixture from the pytest stash) |
| `shortname` | The `shortname` parameter |
| `suite` | Back-reference to parent suite |
| `uname` | Unique name: `"suite.shortname.id"` (with `/` replaced by `_`). If running under xdist, prefixed with the worker ID. |
| `success` | `False`; set to `True` by `run_ctx()` after the test body completes without exception |

### 5.2 Key Methods

**`run_ctx()`** (async context manager): the setup/teardown lifecycle
for a pool-based test:

1. Leases a cluster from `suite.clusters` pool via `await pool.get(logger)`.
2. Calls `cluster.before_test(uname)`.
3. Takes a log savepoint on the cluster.
4. Yields the cluster to the test body.
5. After the test: sets `success = True`, checks if the shortname is in
   `cfg["dirties_cluster"]` (marks the cluster dirty if so), and calls
   `cluster.after_test(uname, success)`.
6. On exception during setup or teardown: marks cluster dirty, logs diagnostic
   info about whether the failure was pre-test or post-test.
7. In `finally`: returns the cluster to the pool via `pool.put(cluster, is_dirty)`.

---

## 6. Module-Level Infrastructure

Terminal output utilities (`output_is_a_tty`, `create_formatter()`, `palette`)
live in `test/pylib/terminal.py`.

The following functions have been moved to `runner.py` (see
[runner-design.md](runner-design.md)):
- `init_testsuite_globals()` -- creates `ArtifactRegistry` / `HostRegistry`
- `prepare_dir()` -- single directory preparation/cleanup
- `prepare_dirs()` -- creates the full directory tree for a test run
- `start_3rd_party_services()` -- starts LDAP, MinIO, S3 mock, S3 proxy
- `prepare_environment()` -- orchestrates dirs + services

### 6.3 Test Creation (in `testpy_test` fixture)

Test creation logic lives directly in the `testpy_test` fixture in `runner.py`:

1. Reads the `TestSuiteConfig` from the pytest stash (`TEST_SUITE` key).
2. Passes it directly to `TestSuite.opt_create()`, which uses the already-parsed
   YAML config (no double read).
3. If `options.exe_path` or `options.exe_url` is set, overrides `suite.scylla_exe`.
4. Creates a `Test` instance, passing the `run_id` from
   `request.node.stash[RUN_ID]` (set by `pytest_collect_file()`).
5. Returns the new `Test` instance.

Conftest fixtures that need `Test` attributes (e.g., `suite.log_dir`, `uname`)
take `testpy_test` as a fixture parameter rather than creating their own `Test`.

### 6.5 Terminal Color Palette (`test/pylib/terminal.py`)

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

| Constant | Value | Location | Description |
|----------|-------|----------|-------------|
| `PYTEST_TESTS_LOGS_FOLDER` | `"pytest_tests_logs"` | `runner.py` | Subdirectory for pytest log files |
| `output_is_a_tty` | `sys.stdout.isatty()` | `terminal.py` | Cached TTY detection |

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

**`testpy_test` fixture** (module-scoped): creates a `Test` instance for the
current test file by reading the suite path from the pytest stash, creating or
retrieving the `TestSuite` via `opt_create()`, and generating a unique test ID.
This is how the conftest files in each test directory access the suite's cluster
pool and configuration — they take `testpy_test` as a fixture parameter.

**`TestSuiteConfig`**: a lightweight parallel implementation used during pytest
collection (not test execution). It reads `test_config.yaml` to filter disabled
tests and apply mode restrictions without creating full `TestSuite` instances.
It walks up the pytest node tree to find the config file and stores
the result in the stash, which the `testpy_test` fixture then reads.

**Session setup**: calls `init_testsuite_globals()` and `prepare_environment()`
during pytest session start (unconditionally for non-xdist-worker processes
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
| `test/cluster/` | Uses `testpy_test` fixture for log paths and `ManagerClient` from the suite's cluster pool |
| `test/cql/` | Uses `testpy_test` fixture to get `output_path` from the suite's log dir |
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
       | suite_config = request.node.stash[TEST_SUITE]
       | TestSuite.opt_create(suite_config)  --> creates/caches TestSuite
       | Test(shortname, suite, run_id=stash[RUN_ID])
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
suite.clusters  (first access triggers @cached_property)
       |
       | Pool(pool_size, suite.create_cluster, cluster.recycle)
       v
   clusters Pool
       |
       | test requests cluster via pool.get(logger)
       |   |
       |   | Pool has available cluster? --> return it
       |   | Pool empty? --> suite.create_cluster(logger)
       |   |   |
       |   |   | ScyllaCluster(vardir, hosts, replicas, mode, opts...)
       |   |   | register stop/uninstall as artifacts
       |   |   | cluster.install_and_start()
       |   |   |   --> cluster.add_server() builds ScyllaServer inline
       |   |   |       (cmdline merging, config assembly, version check)
       |   |   | if start_exception: cleanup + raise
       |   |   v
       |   |   new ScyllaCluster
       |   v
       | cluster.before_test(uname)
       | record log filename
       | take log savepoint
       |
       | --- test executes ---
       |
       | if shortname in cfg["dirties_cluster"]: cluster.is_dirty = True
       | cluster.after_test(uname, success)
       | pool.put(cluster, is_dirty)
       |   |
       |   | is_dirty? --> cluster.recycle()
       |   |   |
       |   |   | close log files + maintenance sockets
       |   |   | cluster.stop()
       |   |   | release IPs
       |   |   | create fresh replacement via suite.create_cluster
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
