# Cluster (Topology) Tests Conftest Design Document

This document describes `test/cluster/conftest.py`, the pytest configuration and
fixture file for the cluster/topology test suite. It covers all fixtures, hooks,
helper functions, and their interactions with the suite framework.

**Related documents:**
- [Test Suite Framework Design](test-suite-design.md)
- [Pytest Runner Plugin Design](runner-design.md)

## Table of Contents

1. [Overview](#1-overview)
2. [Imports and Module-Level Code](#2-imports-and-module-level-code)
3. [Command-Line Options](#3-command-line-options)
4. [Hooks](#4-hooks)
5. [Helper Functions and Classes](#5-helper-functions-and-classes)
6. [Fixtures](#6-fixtures)
7. [Suite Framework Integration](#7-suite-framework-integration)

---

## 1. Overview

`test/cluster/conftest.py` provides fixtures for the topology/cluster test suite
(suite type: `Topology`). Tests in this directory validate cluster topology
operations (node add/remove, replace, decommission, etc.) using a
`ScyllaClusterManager` that provides full lifecycle control over multi-node
clusters.

The file is 396 lines. Key responsibilities:
- Start and manage a `ScyllaClusterManager` in a thread pool executor.
- Provide a `ManagerClient` fixture that handles test lifecycle (before/after).
- Capture logs and backtraces on test failure.
- Decode backtraces via `seastar-addr2line`.
- Provide CQL, random tables, and cluster preparation fixtures.

---

## 2. Imports and Module-Level Code

### 2.1 Key Imports

From suite framework:
- `testpy_test_fixture_scope` from `test.pylib.runner` -- dynamic fixture scoping
- `get_testpy_test` from `test.pylib.suite.base` -- creates Test instances
- `add_cql_connection_options` from `test.pylib.suite.python` -- CLI options

From test infrastructure:
- `TOP_SRC_DIR`, `path_to` from `test/__init__.py`
- `ScyllaClusterManager`, `ScyllaVersionDescription`, `get_scylla_2025_1_description`
  from `test.pylib.scylla_cluster`
- `ManagerClient` from `test.pylib.manager_client`
- `RandomTables` from `test.pylib.random_tables`
- `unique_name` from `test.pylib.util`
- `run_async` from `test.pylib.async_cql`
- `KeyProvider`, `make_key_provider_factory` from `test.pylib.encryption_provider`

From Cassandra driver: `Cluster`, `Session`, `ConsistencyLevel`,
`ExecutionProfile`, `EXEC_PROFILE_DEFAULT`, various policies, auth providers.

### 2.2 Module-Level Side Effects

1. Monkey-patches `Session.run_async = run_async` for convenience (adds async
   CQL execution to driver sessions).
2. Prints driver name and version.
3. Creates `conn_logger` at INFO level for connection debugging.

### 2.3 Type Annotations (TYPE_CHECKING only)

- `IPAddress` from `test.pylib.internal_types`
- `Test` from `test.pylib.suite.base`
- `EndPoint` from `cassandra.connection`

---

## 3. Command-Line Options

### `pytest_addoption(parser)`

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--manager-api` | `str` | `None` | Manager Unix socket path |
| (CQL connection options) | | | Via `add_cql_connection_options()` |
| `--skip-internet-dependent-tests` | `bool` | `False` | Skip internet-dependent tests |
| `--artifacts_dir_url` | `str` | `None` | URL for CI artifact links |

---

## 4. Hooks

### 4.1 `pytest_runtest_makereport(item, call)`

Decorated with `@pytest.hookimpl(tryfirst=True, hookwrapper=True)`.

Stores the test report in `item.stash[PHASE_REPORT_KEY]` after each test phase.
This allows the `manager` fixture to access the test result during teardown.

**`PHASE_REPORT_KEY`**: `pytest.StashKey[dict[str, pytest.CollectReport]]()` --
constant defined at module level.

---

## 5. Helper Functions and Classes

### 5.1 `decode_backtrace(build_mode: str, input: str)` (async)

Decodes Scylla backtraces into human-readable form:
1. Resolves the Scylla executable path via `path_to(build_mode, "scylla")`.
2. Runs `seastar/scripts/seastar-addr2line -e <executable>` as a subprocess.
3. Passes the raw backtrace via stdin.
4. Returns combined stdout + stderr.

### 5.2 `CustomConnection`

Subclass of `Cluster.connection_class` that adds debug logging:
- `send_msg()`: logs outgoing messages at DEBUG level.
- `process_msg()`: logs incoming messages at DEBUG level.

Uses `conn_logger` (module-level, INFO threshold, so DEBUG messages are only
captured when the logger level is lowered).

### 5.3 `cluster_con(hosts, port, use_ssl, auth_provider, load_balancing_policy)`

Factory function that creates a Cassandra driver `Cluster` object:

**Parameters:**
- `hosts: list[IPAddress | EndPoint]` -- contact points (must be non-empty)
- `port: int` -- default 9042
- `use_ssl: bool` -- default False
- `auth_provider` -- default None
- `load_balancing_policy` -- default `RoundRobinPolicy()`

**Configuration:**
- Two execution profiles:
  - `EXEC_PROFILE_DEFAULT`: round-robin, LOCAL_QUORUM, LOCAL_SERIAL, 200s timeout
  - `'whitelist'`: token-aware with whitelist round-robin to specified hosts
- Protocol version: 4 (hardcoded)
- `connect_timeout`, `control_connection_timeout`: 200s
- `idle_heartbeat_timeout`: 200s
- `max_schema_agreement_wait`: 20s (must be 2-3x smaller than request_timeout)
- `reconnection_policy`: `ExponentialReconnectionPolicy(1.0, 4.0)`
- `connection_class`: `CustomConnection` (for debug logging)
- SSL: TLS v1.2 only if `use_ssl` is True

Returns the `Cluster` object (not yet connected).

---

## 6. Fixtures

### 6.1 `manager_api_sock_path` (async, scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Provides the Unix socket path for the ScyllaClusterManager API:

- **Without testpy_test (bare pytest):** yields `--manager-api` option value.
- **With testpy_test:** starts a `ScyllaClusterManager` in a thread pool:
  1. Creates a temp directory for the socket (`/tmp/manager-<random>/api`).
  2. Creates `start_event` and `stop_event` (multiprocessing Events).
  3. Defines `run_manager()` async function:
     - Creates `ScyllaClusterManager` with `test_uname`, `clusters` (from suite),
       `base_dir` (suite log_dir), and `sock_path`.
     - Calls `await mgr.start()`, signals start_event.
     - Waits for stop_event in an executor.
     - Calls `await mgr.stop()` in finally.
  4. Submits `asyncio.run(run_manager())` to a `ThreadPoolExecutor(max_workers=1)`.
  5. Waits for start_event, yields socket path.
  6. On teardown: sets stop_event, calls `future.result()` to propagate errors.

### 6.2 `manager_internal` (async, scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Prepares a factory function for creating `ManagerClient` instances:
1. Reads `port`, `ssl`, `auth_username`, `auth_password` from options.
2. Creates `PlainTextAuthProvider` if credentials are provided.
3. Returns a lambda that creates `ManagerClient` with the socket path, port,
   SSL, auth, and `cluster_con` as the connection generator.

### 6.3 `manager` (async, scope=function)

**Scope:** function

The main test fixture for cluster topology tests. Each test gets a fresh
manager client instance:

**Setup:**
1. Creates a `Test` instance via `get_testpy_test()` for log path resolution.
2. Computes test-specific log paths:
   - `test_log = log_dir / "<suite_name>.<test_case_name>.log"`
   - `test_py_log_test = log_dir / "<test_log_stem>_cluster.log"`
3. Calls `manager_internal()` to create a `ManagerClient`.
4. Calls `await manager_client.before_test(test_case_name, test_log)`.

**Yield:** the `ManagerClient` instance.

**Teardown:**
1. Retrieves the test report from `request.node.stash[PHASE_REPORT_KEY]`.
2. Checks for failure: `report.when == "call" and report.failed`.
3. Calls `manager_client.check_all_errors()`:
   - If `check_nodes_for_errors` marker is present: checks all errors.
4. On failure or errors:
   - Creates `failed_test` directory under suite log_dir.
   - Gathers related logs via `manager_client.gather_related_logs()`.
   - Writes stacktrace to `stacktrace.txt`.
   - If `--artifacts_dir_url` is set: computes relative path from `testlog/`,
     constructs full URL, records as `TEST_LOGS` property (visible in Jenkins).
5. Calls `manager_client.after_test(test_case_name, not failed)`.
6. In `finally`: calls `await manager_client.stop()`.
7. Post-cleanup checks:
   - If cluster status indicates `server_broken` and test didn't fail:
     calls `pytest.fail()` with the broken reason.
   - If errors found: formats a detailed message per server including:
     - Critical errors (count + text)
     - Backtraces (count, decoded via `decode_backtrace()`, saved to file)
     - Errors (count + text)
     - Core dumps (count + paths)
     - Writes `found_errors.txt` and calls `pytest.fail()` if test didn't fail.

### 6.4 `cql` (scope=function)

Yields `manager.cql` -- the CQL session from the manager client.

### 6.5 `random_tables` (async, scope=function)

Creates a `RandomTables` instance for schema mutation testing:
1. Reads `replication_factor` from `@pytest.mark.replication_factor(N)` marker
   (default: 3).
2. Reads `enable_tablets` from `@pytest.mark.enable_tablets(bool)` marker
   (default: None).
3. Creates `RandomTables(test_name, manager, unique_name, rf, None, enable_tablets)`.
4. Yields the tables object.
5. On teardown: checks if test failed (via `PHASE_REPORT_KEY`) or cluster is
   dirty (`manager.is_dirty()`). Only drops tables if neither is true.

### 6.6 `prepare_3_nodes_cluster` (autouse, scope=function)

Autouse fixture that adds 3 nodes if the test has the
`@pytest.mark.prepare_3_nodes_cluster` marker. Calls
`await manager.servers_add(3)`.

### 6.7 `prepare_3_racks_cluster` (autouse, scope=function)

Autouse fixture that adds 3 nodes across racks if the test has the
`@pytest.mark.prepare_3_racks_cluster` marker. Calls
`await manager.servers_add(3, auto_rack_dc="dc1")`.

### 6.8 `internet_dependency_enabled` (scope=function)

Skips the test if `--skip-internet-dependent-tests` is set.

### 6.9 `scylla_2025_1` (async, scope=function)

Returns a `ScyllaVersionDescription` for ScyllaDB 2025.1 via
`get_scylla_2025_1_description(build_mode)`. Depends on
`internet_dependency_enabled`.

### 6.10 `key_provider` (async, scope=function, parametrized)

Parametrized over `list(KeyProvider)` (all encryption provider types).
Creates an encryption key provider factory via `make_key_provider_factory()`.
Uses `async with` for proper cleanup.

---

## 7. Suite Framework Integration

### Import Map

| Symbol | Source | Usage |
|--------|--------|-------|
| `testpy_test_fixture_scope` | `runner.py` | Scope for `manager_api_sock_path`, `manager_internal` |
| `get_testpy_test` | `suite/base.py` | `manager` fixture -- creates Test for log paths |
| `add_cql_connection_options` | `suite/python.py` | `pytest_addoption` |
| `Test` (TYPE_CHECKING) | `suite/base.py` | Type annotation for `testpy_test` |
| `path_to` | `test/__init__.py` | `decode_backtrace` -- resolve scylla executable |
| `TOP_SRC_DIR` | `test/__init__.py` | `decode_backtrace` -- seastar-addr2line path |

### Cluster Manager Lifecycle

```
testpy_test fixture (from runner.py)
    |-- get_testpy_test() --> TopologyTestSuite instance
    v
manager_api_sock_path fixture
    |-- testpy_test.suite.clusters (cluster pool from TopologyTestSuite)
    |-- testpy_test.suite.log_dir (log directory)
    |-- ScyllaClusterManager(test_uname, clusters, base_dir, sock_path)
    |-- start manager in ThreadPoolExecutor
    v
manager_internal fixture
    |-- creates ManagerClient factory with socket path, port, SSL, auth
    v
manager fixture (per-test)
    |-- get_testpy_test() for log path computation
    |-- manager_client = manager_internal()
    |-- await manager_client.before_test(name, log_path)
    |-- yield manager_client
    |-- (teardown) check errors, gather logs, after_test, stop
    v
test functions
    |-- use manager.cql, manager.servers_add(), etc.
```

### Key Design Decisions

1. **Two levels of testpy_test access**: the `manager_api_sock_path` fixture
   receives `testpy_test` from the runner's fixture for cluster pool access. The
   `manager` fixture independently calls `get_testpy_test()` for log path
   computation, creating a second Test instance.

2. **Thread pool for manager**: `ScyllaClusterManager` runs its own asyncio event
   loop in a separate thread via `ThreadPoolExecutor`. This isolates the manager's
   async operations from pytest's event loop.

3. **Error detection is post-test**: error checking (`check_all_errors`) runs
   during fixture teardown, not during the test itself. This catches server-side
   issues (backtraces, critical errors, core dumps) even when the test itself
   passes.

4. **Failed test directory structure**: `log_dir / "failed_test" / <test_case_name>/`
   contains `stacktrace.txt`, `found_errors.txt`, decoded backtrace files, and
   gathered server logs.
