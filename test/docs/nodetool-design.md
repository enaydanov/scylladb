# Nodetool Tests Conftest Design Document

This document describes `test/nodetool/conftest.py`, the pytest configuration
and fixture file for the nodetool test suite. It covers all fixtures, helper
functions, and their interactions with the suite framework.

**Related documents:**
- [Test Suite Framework Design](test-suite-design.md)
- [Pytest Runner Plugin Design](runner-design.md)

## Table of Contents

1. [Overview](#1-overview)
2. [Imports and Module-Level Code](#2-imports-and-module-level-code)
3. [Command-Line Options](#3-command-line-options)
4. [Types](#4-types)
5. [Fixtures](#5-fixtures)
6. [Helper Functions](#6-helper-functions)
7. [Suite Framework Integration](#7-suite-framework-integration)

---

## 1. Overview

`test/nodetool/conftest.py` provides fixtures for the nodetool test suite
(suite type: `Python`). Tests in this directory validate nodetool commands
against a mock REST API server, supporting both Scylla's native nodetool and
Cassandra's JMX-based nodetool.

The file is 239 lines. Key responsibilities:
- Lease server addresses from the suite's host registry.
- Start and manage a REST API mock server.
- Optionally start a JMX bridge for Cassandra nodetool compatibility.
- Provide a `nodetool` invoker fixture that constructs and executes nodetool
  commands with proper arguments for the selected implementation.

---

## 2. Imports and Module-Level Code

### 2.1 Key Imports

From suite framework:
- `testpy_test_fixture_scope` from `test.pylib.runner` -- dynamic fixture scoping

From test infrastructure:
- `TOP_SRC_DIR`, `path_to` from `test/__init__.py`
- REST API mock helpers: `set_expected_requests`, `expected_request`,
  `get_expected_requests`, `get_unexpected_requests`, `expected_requests_manager`
  from `test.nodetool.rest_api_mock`
- `Test` from `test.pylib.db.model` -- **not** from the suite framework (this is
  used for type annotation of `testpy_test` parameter)

### 2.2 Notable Import

The `Test` type imported here is from `test.pylib.db.model`, not from
`test.pylib.suite.base`. The `testpy_test` parameter in the `server_address`
fixture is annotated as `None | Test` where `Test` refers to this DB model type.

---

## 3. Command-Line Options

### `pytest_addoption(parser)`

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--nodetool` | `choices["scylla", "cassandra"]` | `"scylla"` | Nodetool implementation to test against |
| `--nodetool-path` | `str` | `None` | Path to nodetool binary |
| `--jmx-path` | `str` | `None` | Path to JMX binary (Cassandra mode only) |
| `--run-within-unshare` | `bool` | `False` | Set up `lo` network in unshare namespace |

---

## 4. Types

### `ServerAddress`

A `NamedTuple` with two fields:
- `ip: str` -- IP address
- `port: int` -- port number

---

## 5. Fixtures

### 5.1 `server_address` (async, scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Provides an IP address and port for the REST API mock server:

- **With `--run-within-unshare`:** sets up the `lo` network interface (tries
  `ip link set lo up`, falls back to `/sbin/ifconfig lo up`) and uses a fixed
  address `127.0.0.1:12345`.
- **With testpy_test:** leases an IP from `testpy_test.suite.hosts` (the suite's
  `HostRegistry`) and generates a random port in `[10000, 65535]`.
- **Without testpy_test (bare pytest):** generates a random IP in the
  `127.x.x.x` range and a random port in `[10000, 65535]`.

On teardown: if testpy_test is not None, releases the leased IP back to the
host registry.

### 5.2 `rest_api_mock_server` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Starts the REST API mock server as a subprocess:

1. Spawns `rest_api_mock.py` with the server address IP and port.
2. Polls for readiness with 0.1s intervals for up to 5 seconds:
   - Checks if the process has terminated (raises `CalledProcessError`).
   - Calls `get_expected_requests()` to verify the server is responding.
   - Handles `ConnectionError` (server not ready) and `HTTPError` with 404
     (server up but endpoint not ready).
3. On timeout: terminates the server and raises `TimeoutExpired`.
4. Yields the `server_address` tuple.
5. On teardown: terminates and waits for the server process.

### 5.3 `jmx` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Starts the JMX bridge for Cassandra nodetool compatibility:

- **Scylla mode (`--nodetool=scylla`):** yields immediately (no JMX needed).
- **Cassandra mode:**
  1. Resolves JMX path: from `--jmx-path` option or defaults to
     `TOP_SRC_DIR / "tools" / "jmx" / "scripts" / "scylla-jmx"`.
  2. Sets working directory to `jmx_path.parent.parent`.
  3. Configures expected requests for JMX startup:
     - `GET /column_family/` (returns mock column family list)
     - `GET /stream_manager/` (returns empty list)
  4. Generates a random JMX port (avoiding the API port).
  5. Starts the JMX process with `-a`, `-p`, `-ja`, `-jp` arguments.
  6. Waits for JMX readiness by polling `get_expected_requests()` until the
     startup requests are consumed (up to 5 seconds).
  7. Yields `(jmx_ip, jmx_port)` tuple.
  8. On teardown: terminates and waits for the JMX process.

JMX IP is hardcoded to `"127.0.0.1"` (the launcher script ignores the host
parameter).

### 5.4 `nodetool_path` (scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Resolves the nodetool binary path:

- **Scylla mode:** returns `path_to(build_mode, "scylla")`.
- **Cassandra mode with `--nodetool-path`:** returns the absolute path.
- **Cassandra mode without path:** returns
  `TOP_SRC_DIR / "java" / "bin" / "nodetool"`.

### 5.5 `scylla_only` (scope=function)

Skips the test if `--nodetool` is not `"scylla"`.

### 5.6 `cassandra_only` (scope=function)

Skips the test if `--nodetool` is not `"cassandra"`.

### 5.7 `nodetool` (scope=module)

The main fixture -- returns an invoker function for executing nodetool commands.

**Invoker signature:** `invoker(method, *args, expected_requests=None, check_return_code=True)`

**Behavior:**

1. Enters `expected_requests_manager(rest_api_mock_server, expected_requests or [])`.
2. Splits `args` by `"--"` delimiter into `before` and `after` lists via
   `split_list()`.
3. Constructs the command based on nodetool type:
   - **Scylla:** `[nodetool_path, "nodetool", method] + before + ["--logger-log-level", "scylla-nodetool=trace", "-h", api_ip, "-p", api_port] + after`
   - **Cassandra:** `[nodetool_path, "-h", jmx_ip, "-p", jmx_port, method] + list(args)`
4. Sets sanitizer environment:
   - `UBSAN_OPTIONS`: `halt_on_error=1:abort_on_error=1:suppressions=<path>`
   - `ASAN_OPTIONS`: `disable_coredump=0:abort_on_error=1:detect_stack_use_after_return=1`
5. Runs via `subprocess.run()` with `capture_output=True, text=True`.
6. Writes stdout/stderr to `sys.stdout`/`sys.stderr`.
7. Validates:
   - If `check_return_code`: calls `res.check_returncode()`.
   - Asserts no unconsumed expected requests remain.
   - Asserts no unexpected requests were received.
8. Returns the `subprocess.CompletedProcess` result.

---

## 6. Helper Functions

### `split_list(l, delim) -> tuple[list, list]`

Splits a list at the first occurrence of `delim`:
- Returns `(before, after)` where `after` includes the delimiter.
- If `delim` is not found: returns `(l, [])`.

Used to separate nodetool arguments before and after `"--"` (which separates
nodetool-level args from Scylla-level args in Scylla nodetool).

---

## 7. Suite Framework Integration

### Import Map

| Symbol | Source | Usage |
|--------|--------|-------|
| `testpy_test_fixture_scope` | `runner.py` | Scope for 4 fixtures |
| `path_to` | `test/__init__.py` | `nodetool_path` fixture |
| `TOP_SRC_DIR` | `test/__init__.py` | `jmx` fixture, `nodetool` fixture |

### Host Registry Interaction

The `server_address` fixture is the only direct interaction with the suite
framework's host management:

```
server_address fixture
    |
    | testpy_test is not None?
    |   YES: ip = await testpy_test.suite.hosts.lease_host()
    |   NO:  ip = random 127.x.x.x
    |
    | port = random [10000, 65535]
    |
    | yield ServerAddress(ip, port)
    |
    | (teardown) if testpy_test:
    |     await testpy_test.suite.hosts.release_host(ip)
```

### Key Design Decisions

1. **No cluster needed:** unlike cqlpy or cluster tests, nodetool tests do not
   start a Scylla cluster. They use a mock REST API server that simulates
   Scylla's HTTP management interface.

2. **Dual nodetool support:** the fixture chain supports both Scylla's native
   nodetool (direct REST API calls) and Cassandra's JMX-based nodetool (via a
   JMX bridge that proxies to the REST API).

3. **Coverage disabled:** the suite's `test_config.yaml` sets `coverage: false`
   because nodetool tests don't exercise the Scylla binary directly.

4. **Module-scoped invoker:** the `nodetool` fixture is module-scoped (not
   function-scoped), so the JMX bridge and mock server persist across all tests
   in a module.
