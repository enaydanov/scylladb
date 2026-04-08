# CQL Python Tests (cqlpy) Conftest Design Document

This document describes `test/cqlpy/conftest.py`, the pytest configuration and
fixture file for the CQL Python test suite. It covers all fixtures, hooks, and
their interactions with the suite framework.

**Related documents:**
- [Test Suite Framework Design](test-suite-design.md)
- [Pytest Runner Plugin Design](runner-design.md)

## Table of Contents

1. [Overview](#1-overview)
2. [Imports and Module-Level Code](#2-imports-and-module-level-code)
3. [Command-Line Options](#3-command-line-options)
4. [Fixtures](#4-fixtures)
5. [Suite Framework Integration](#5-suite-framework-integration)

---

## 1. Overview

`test/cqlpy/conftest.py` provides 18 fixtures for the CQL Python test suite
(suite type: `Python`). Tests in this directory validate CQL behavior by
connecting to a running Scylla instance via the Cassandra Python driver.

The file is 276 lines. Key responsibilities:
- Lease a Scylla cluster from the suite's pool via `testpy_test.run_ctx()`.
- Establish and manage CQL sessions.
- Detect Scylla crashes after each test.
- Provide common keyspace fixtures (vnodes, tablets, parametrizable).
- Provide skip/xfail fixtures for Scylla-only and Cassandra-bug tests.
- Provide utility fixtures (random seeds, paths, temp directories).

---

## 2. Imports and Module-Level Code

### 2.1 Key Imports

From suite framework:
- `testpy_test_fixture_scope` from `test.pylib.runner` -- dynamic fixture scoping
- `Test` from `test.pylib.suite` -- type annotation for `testpy_test`
- `add_host_option`, `add_cql_connection_options`, `add_s3_options` from
  `test.pylib.runner` -- CLI option registration helpers

From local utilities (`test.cqlpy.util`):
- `unique_name`, `new_test_keyspace`, `keyspace_has_tablets`, `cql_session`,
  `local_process_id`, `is_scylla`, `config_value_context`

From local module:
- `scylla_log` from `test.cqlpy.nodetool` -- logs messages to Scylla via CQL

### 2.2 Module-Level Side Effect

Prints `f"Driver name {DRIVER_NAME}, version {DRIVER_VERSION}"` at import time
using constants from `cassandra.connection`.

---

## 3. Command-Line Options

### `pytest_addoption(parser)`

Delegates to three helper functions and adds one local option:

1. `add_host_option(parser)` -- adds `--host` and `--port` options.
2. `add_cql_connection_options(parser)` -- adds `--ssl`, `--auth_username`,
   `--auth_password`.
3. `parser.addoption('--no-minio', ...)` -- signals to skip S3 tests.
4. `add_s3_options(parser)` -- adds S3-related options.

---

## 4. Fixtures

### 4.1 `host` (async, scope=dynamic)

**Scope:** `testpy_test_fixture_scope` (module or session)

The core fixture that provides a Scylla server address:

- **With testpy_test (test.py mode):** enters `testpy_test.run_ctx(options=testpy_test.suite.options)`, which leases a cluster from the pool, runs `before_test()`/`after_test()`, and yields the server address. The `async with` block keeps the cluster leased for the duration of the scope.
- **Without testpy_test (bare pytest):** yields `request.config.getoption("--host")`, using the user-provided host address.

Type annotation: `testpy_test: Test | None`.

### 4.2 `cql` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Creates a CQL session connected to the `host` fixture's address:

1. Gets port from `--port` option.
2. Creates a session via `cql_session()` with SSL and auth options from CLI.
3. Default credentials: `"cassandra"` / `"cassandra"`.
4. On `NoHostAvailable`: calls `pytest.exit()` with `INTERNAL_ERROR` return code
   to abort the entire session rather than reporting individual test failures.
5. Yields the session, then calls `session.shutdown()`.

### 4.3 `cql_test_connection` (autouse, scope=function)

**Scope:** function, `autouse=True`

Crash detection fixture that runs before and after every test:

- **Before test:** calls `scylla_log()` to log the test start to Scylla's log.
  Checks `cql_test_connection.scylla_crashed` class attribute -- if True,
  skips with "Server down".
- **After test:** executes `"BEGIN BATCH APPLY BATCH"` as a no-op CQL command.
  On failure, sets `scylla_crashed = True` and calls `pytest.fail()`.
- Logs the test end to Scylla's log.

The `scylla_crashed` flag is a module-level attribute on the function object,
initialized to `False`.

### 4.4 `this_dc` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Returns the current datacenter name by querying
`SELECT data_center FROM system.local`.

### 4.5 `test_keyspace_tablets` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Creates a temporary tablets-enabled keyspace:
- If not Scylla or tablets not available: yields `None`.
- Otherwise: creates keyspace with `TABLETS = {'enabled': true}`,
  yields name, drops on teardown.

### 4.6 `test_keyspace_vnodes` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Creates a temporary vnodes (non-tablets) keyspace:
- If tablets available: creates with `TABLETS = {'enabled': false}`.
- Otherwise: creates a regular keyspace.
- Yields name, drops on teardown.

### 4.7 `test_keyspace` (scope=dynamic, parametrizable)

**Scope:** `testpy_test_fixture_scope`

A parametrizable keyspace fixture:
- If `request.param == "vnodes"`: yields `test_keyspace_vnodes`.
- If `request.param == "tablets"`: yields `test_keyspace_tablets` (skips if None).
- If parameter is unrecognized: calls `pytest.fail()` with an error message.
- If no parameter: creates a fresh keyspace, yields name, drops on teardown.

Tests use `@pytest.mark.parametrize("test_keyspace", ["vnodes", "tablets"], indirect=True)`
to run against both keyspace types.

### 4.8 `scylla_only` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Skips the test if not running against Scylla (detected via `is_scylla(cql)`).

### 4.9 `cassandra_bug` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Marks the test as `xfail` if running against Cassandra. Detection: checks if
any table in `system_schema.tables` for `keyspace_name = 'system'` contains
"scylla" in the name.

### 4.10 `driver_bug_1` (scope=function)

Skips the test if the Cassandra driver version is too old (before empty page
handling fix). Thresholds:
- Scylla driver: < 3.24.5
- Datastax driver: <= 3.25.0

### 4.11 `random_seed` (scope=function)

Provides a seeded random number generator for reproducible tests:
1. Saves current `random` state.
2. Seeds with `time.time()`, prints the seed.
3. Yields the seed value.
4. Restores original random state on teardown.

### 4.12 `scylla_path` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Finds the local Scylla executable path:
1. Gets PID via `local_process_id(cql)`.
2. Reads `/proc/<pid>/exe` symlink.
3. Validates by running `<path> --list-tools`.
4. Skips test at each step if the check fails.

### 4.13 `scylla_data_dir` (scope=module)

Returns Scylla's data directory by querying
`SELECT value FROM system.config WHERE name = 'data_file_directories'` and
JSON-parsing the result. Skips if the query fails.

### 4.14 `temp_workdir` (scope=function)

Creates a temporary directory via `tempfile.TemporaryDirectory()`, yields the
path, cleans up automatically.

### 4.15 `has_tablets` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Returns a boolean indicating whether tablets are enabled by creating a test
keyspace and checking via `keyspace_has_tablets()`.

### 4.16 `skip_without_tablets` (scope=function)

Depends on `scylla_only` and `has_tablets`. Skips if tablets are not available.

### 4.17 `compact_storage` (scope=function)

Temporarily enables the deprecated `WITH COMPACT STORAGE` feature via
`config_value_context(cql, 'enable_create_table_with_compact_storage', 'true')`.
Falls through silently on Cassandra (where it's enabled by default).

### 4.18 `skip_s3_tests`

Skips S3-related tests if `--no-minio` option is set.

---

## 5. Suite Framework Integration

### Import Map

| Symbol | Source | Usage |
|--------|--------|-------|
| `testpy_test_fixture_scope` | `runner.py` | Dynamic scope for 10 fixtures |
| `Test` | `suite.py` | Type annotation in `host` fixture |
| `add_host_option` | `runner.py` | `pytest_addoption` |
| `add_cql_connection_options` | `runner.py` | `pytest_addoption` |
| `add_s3_options` | `runner.py` | `pytest_addoption` |

### Cluster Lifecycle

The `host` fixture is the integration point with the suite's cluster pool:

```
host fixture
    |
    | testpy_test is not None?
    |   YES: async with testpy_test.run_ctx(options)
    |          |-- lease cluster from TestSuite.clusters pool
    |          |-- cluster.before_test(uname)
    |          |-- yield cluster.endpoint()
    |          |-- cluster.after_test(uname, success)
    |          |-- return cluster to pool
    |   NO:  yield request.config.getoption("--host")
    v
cql fixture
    |-- cql_session(host, port, ssl, username, password)
    v
test functions
    |-- use cql session
    v
cql_test_connection fixture (teardown)
    |-- "BEGIN BATCH APPLY BATCH"
    |-- detect crash
```

### Fixture Scope Chain

Most fixtures use `testpy_test_fixture_scope`, creating a consistent scope
chain: `host` -> `cql` -> `this_dc` -> `has_tablets` -> `test_keyspace_*` ->
`test_keyspace`. All are effectively module-scoped (one Scylla session per test
file).
