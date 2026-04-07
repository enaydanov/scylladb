# CQL Approval Tests Conftest Design Document

This document describes `test/cql/conftest.py`, the pytest configuration and
fixture file for the CQL approval test suite. It covers all fixtures, hooks, and
their interactions with the suite framework.

**Related documents:**
- [Test Suite Framework Design](test-suite-design.md)
- [Pytest Runner Plugin Design](runner-design.md)
- [CQL Python Tests (cqlpy) Conftest Design](cqlpy-design.md)

## Table of Contents

1. [Overview](#1-overview)
2. [Imports and Re-exports](#2-imports-and-re-exports)
3. [Command-Line Options](#3-command-line-options)
4. [Collection Hook](#4-collection-hook)
5. [Fixtures](#5-fixtures)
6. [Suite Framework Integration](#6-suite-framework-integration)

---

## 1. Overview

`test/cql/conftest.py` provides fixtures for the CQL approval test suite
. Tests in this directory are `.cql` files that contain
CQL statements and expected output. The test runner executes the CQL statements
and compares actual output against expected results.

The file is 82 lines. It is notably lightweight because it re-imports key
fixtures from `test/cqlpy/conftest.py` rather than redefining them.

---

## 2. Imports and Re-exports

### 2.1 Fixture Re-imports

Three fixtures are re-imported from the cqlpy conftest:

```python
from test.cqlpy.conftest import host, cql, this_dc
```

These fixtures become available in this conftest's scope without redefinition.
They provide the Scylla server address, CQL session, and datacenter name
respectively.

### 2.2 Other Imports

| Symbol | Source | Purpose |
|--------|--------|---------|
| `CQL_TEST_SUFFIX` | `test.pylib.cql_repl` | File suffix for CQL test files |
| `CqlFile` | `test.pylib.cql_repl` | Custom pytest collector for `.cql` files |
| `testpy_test_fixture_scope` | `test.pylib.runner` | Dynamic fixture scoping |
| `get_testpy_test` | `test.pylib.suite` | Creates Test instances |
| `add_host_option` | `test.pylib.runner` | CLI option helper |
| `add_cql_connection_options` | `test.pylib.runner` | CLI option helper |

---

## 3. Command-Line Options

### `pytest_addoption(parser: pytest.Parser)`

Registers host and CQL connection options:

1. `add_host_option(parser)` -- adds `--host` and `--port`.
2. `add_cql_connection_options(parser)` -- adds `--ssl`, `--auth_username`,
   `--auth_password`.

No additional local options are defined.

---

## 4. Collection Hook

### `pytest_collect_file(file_path: Path, parent: Collector) -> Collector | None`

Custom collection hook for CQL test files:

- If `file_path.name` ends with `CQL_TEST_SUFFIX` (e.g., `_test.cql`):
  returns `CqlFile.from_parent(parent=parent, path=file_path)`.
- Otherwise: returns `None`.

This enables pytest to discover and collect `.cql` files as test items. The
`CqlFile` class (from `test.pylib.cql_repl`) handles parsing the CQL file and
creating test items for each CQL statement block.

---

## 5. Fixtures

### 5.1 `cql_test_connection` (autouse)

Crash detection fixture, functionally similar to the cqlpy version but with a
different no-op CQL command:

- **After test:** executes `"use system"` (instead of cqlpy's
  `"BEGIN BATCH APPLY BATCH"`).
- On failure: calls `pytest.fail(f"Scylla appears to have crashed: {exc}")`.

This fixture has `autouse=True`, so it runs for every test including CQL file
tests. The autouse is important because CQL test files are not Python files and
cannot explicitly request fixtures.

### 5.2 `keyspace` (autouse, scope=dynamic)

**Scope:** `testpy_test_fixture_scope`, `autouse=True`

Creates a session-wide random keyspace for all CQL tests:

1. Generates `keyspace_name = f"test_{uuid.uuid4().hex}"`.
2. Creates the keyspace with RF=1 using `NetworkTopologyStrategy` and the
   current DC from `this_dc`.
3. Yields the keyspace name.
4. Drops the keyspace on teardown.

Autouse ensures every test in this directory runs within this keyspace context.

### 5.3 `output_path` (autouse, scope=module)

**Scope:** module, `autouse=True`

Provides the file path for `.reject` files (actual output that differed from
expected):

1. Calls `await get_testpy_test(path=request.path, options=request.config.option, mode=build_mode)`.
2. Returns `testpy_test.suite.log_dir / f"{testpy_test.uname}.reject"`.

This path is used by `CqlFile` to write the actual output when it differs from
the expected `.result` file, enabling diff-based debugging.

---

## 6. Suite Framework Integration

### Import Map

| Symbol | Source | Usage |
|--------|--------|-------|
| `host` (fixture) | `cqlpy/conftest.py` | Re-imported -- provides server address |
| `cql` (fixture) | `cqlpy/conftest.py` | Re-imported -- provides CQL session |
| `this_dc` (fixture) | `cqlpy/conftest.py` | Re-imported -- provides DC name |
| `testpy_test_fixture_scope` | `runner.py` | Scope for `keyspace` fixture |
| `get_testpy_test` | `suite.py` | `output_path` fixture |
| `add_host_option` | `runner.py` | `pytest_addoption` |
| `add_cql_connection_options` | `runner.py` | `pytest_addoption` |

### Relationship to CQL Approval Suite

CQL approval tests use `TestSuite` (configured via
`test/cql/test_config.yaml`).  The conftest provides the
runtime fixtures that the approval test runner needs: a CQL session, a keyspace,
and an output path for reject files.

### Data Flow

```
CQL test file (e.g., something_test.cql)
    |
    | pytest_collect_file()
    v
CqlFile collector
    |-- CQL statements + expected results
    v
keyspace fixture (autouse)
    |-- creates random keyspace
    v
cql fixture (from cqlpy)
    |-- CQL session connected to Scylla
    v
output_path fixture (autouse)
    |-- get_testpy_test() --> TestSuite instance
    |-- provides reject file path in suite log_dir
    v
CQL test execution
    |-- runs CQL statements
    |-- compares output to .result file
    |-- writes .reject file if different
    v
cql_test_connection fixture (teardown)
    |-- "use system" crash check
```
