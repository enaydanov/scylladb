# Scylla GDB Tests Conftest Design Document

This document describes `test/scylla_gdb/conftest.py`, the pytest configuration
and fixture file for the Scylla GDB test suite. It covers all fixtures, helper
functions, and their interactions with the suite framework.

**Related documents:**
- [Test Suite Framework Design](test-suite-design.md)
- [Pytest Runner Plugin Design](runner-design.md)

## Table of Contents

1. [Overview](#1-overview)
2. [Imports](#2-imports)
3. [Fixtures](#3-fixtures)
4. [Helper Function](#4-helper-function)
5. [Suite Framework Integration](#5-suite-framework-integration)

---

## 1. Overview

`test/scylla_gdb/conftest.py` provides fixtures for the Scylla GDB test suite
(suite type: `Python`). Tests in this directory validate Scylla's GDB debugging
extensions (`scylla-gdb.py`) by attaching GDB to a running Scylla process and
executing custom GDB commands.

The file is 75 lines. Key responsibilities:
- Lease a running Scylla server from the suite's cluster pool.
- Build GDB command lines for attaching to the Scylla process.
- Execute GDB commands via subprocess.

---

## 2. Imports

| Symbol | Source | Purpose |
|--------|--------|---------|
| `testpy_test_fixture_scope` | `test.pylib.runner` | Dynamic fixture scoping |
| `PythonTest` | `test.pylib.suite.python` | Type annotation for `testpy_test` |

Standard library: `os`, `subprocess`, `pytest`.

---

## 3. Fixtures

### 3.1 `scylla_server` (async, scope=dynamic)

**Scope:** `testpy_test_fixture_scope`

Provides a running Scylla server instance:

1. Enters `testpy_test.run_ctx(options=testpy_test.suite.options)` -- this
   leases a cluster from the `PythonTestSuite.clusters` pool.
2. The context manager yields a `cluster` object.
3. Yields the first server from `cluster.running.values()` via `next(iter(...))`.

The `run_ctx()` call manages the full cluster lifecycle: before_test,
log savepoint, and after_test on teardown.

Type annotation: `testpy_test: PythonTest | None`.

### 3.2 `gdb_cmd` (scope=module)

**Scope:** module

Builds a GDB command-line argument list for attaching to the Scylla process:

1. Resolves `scylla-gdb.py` path: relative to the test file's directory,
   `../../scylla-gdb.py` (i.e., repository root).
2. Resolves `gdb_utils.py` path: in the same directory as the test file.
3. Constructs the command:

```
gdb -q --batch --nx
    -se <scylla_server.exe>
    -p <scylla_server.cmd.pid>
    -ex "set python print-stack full"
    -x <scylla-gdb.py>
    -x <gdb_utils.py>
```

Key flags:
- `-q`: quiet (suppress GDB intro)
- `--batch`: exit after processing commands
- `--nx`: don't read `.gdbinit`
- `-se`: load symbol file (Scylla executable)
- `-p`: attach to PID
- `-x`: source Python extension files

Returns the command as a list of strings.

---

## 4. Helper Function

### `execute_gdb_command(gdb_cmd, scylla_command=None, full_command=None)`

**Not a fixture** -- a regular function called directly by test functions.

Executes a single GDB command attached to the running Scylla process.

**Parameters:**
- `gdb_cmd`: base argv list from the `gdb_cmd` fixture
- `scylla_command`: a Scylla GDB extension command (e.g., `"memory"`,
  `"threadqueues"`). Mutually exclusive with `full_command`.
- `full_command`: a raw GDB command string. Mutually exclusive with
  `scylla_command`.

**Behavior:**

- If `full_command` is provided: appends `-ex <full_command>` to the base command.
- If `scylla_command` is provided: appends
  `-ex "python gdb.execute('scylla <scylla_command>')"` -- executes the Scylla
  GDB command via GDB's Python interface.

Runs via `subprocess.run()` with `capture_output=True, text=True,
encoding="utf-8", errors="replace"`.

Returns the `subprocess.CompletedProcess` result (with `.stdout`, `.stderr`,
`.returncode`).

---

## 5. Suite Framework Integration

### Import Map

| Symbol | Source | Usage |
|--------|--------|-------|
| `testpy_test_fixture_scope` | `runner.py` | Scope for `scylla_server` fixture |
| `PythonTest` | `suite/python.py` | Type annotation in `scylla_server` |

### Cluster Lifecycle

```
scylla_server fixture
    |
    | testpy_test.run_ctx(options)
    |   |-- lease cluster from PythonTestSuite.clusters pool
    |   |-- cluster.before_test(uname)
    |   |-- yield cluster
    |   v
    | cluster.running.values()
    |   |-- get first running ScyllaServer
    |   v
    | yield server
    |
    | (teardown) run_ctx exit:
    |   |-- cluster.after_test(uname, success)
    |   |-- return cluster to pool
    v
gdb_cmd fixture
    |-- server.exe (executable path)
    |-- server.cmd.pid (process ID)
    |-- build GDB attach command
    v
test functions
    |-- execute_gdb_command(gdb_cmd, scylla_command="...")
    |-- assert on stdout/stderr
```

### Key Design Decisions

1. **Attach, don't launch:** GDB attaches to an already-running Scylla process
   via `-p <pid>`, rather than launching Scylla under GDB. This tests the GDB
   extensions against a process in its normal running state.

2. **Batch mode:** `--batch` ensures GDB exits after command execution, making
   it suitable for automated testing.

3. **Two command modes:** `scylla_command` wraps the command in `gdb.execute()`
   via Python to invoke Scylla GDB extension commands. `full_command` passes raw
   GDB commands for non-Scylla GDB operations.

4. **Module scope for gdb_cmd:** the GDB command template is module-scoped because
   it depends on the running server (which is session/module-scoped via
   `testpy_test_fixture_scope`). Each test that calls `execute_gdb_command()`
   extends the base command with its specific command.
