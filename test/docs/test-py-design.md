# test.py Design Document

This document describes the thin synchronous test wrapper `test.py` located at
the repository root. It covers the file's complete structure, behavior, and
integration with the pytest plugin (`runner.py`).

**Related document:** [Test Suite Framework Design](test-suite-design.md)

## Table of Contents

1. [Overview](#1-overview)
2. [Imports and Constants](#2-imports-and-constants)
3. [Command-Line Interface](#3-command-line-interface)
4. [ThreadsCalculator](#4-threadscalculator)
5. [Pytest Runner Bridge](#5-pytest-runner-bridge)
6. [Main Orchestrator](#6-main-orchestrator)
7. [Entry Point](#7-entry-point)

---

## 1. Overview

> **Note:** `test.py` is one of three execution modes for the test suite.
> The other two are bare pytest (invoking pytest directly, with `runner.py`
> loaded as a plugin) and `run.py` scripts (`test/cqlpy/run`,
> `test/alternator/run`, `test/rest_api/run`) that start Scylla externally
> and set `SCYLLA_TEST_RUNNER=runpy`.

`test.py` is a thin, synchronous wrapper around `pytest.main()` that provides
CI-compatible argument parsing and resource-aware job count computation. It:

1. Parses command-line arguments (~25 options).
2. Computes optimal `-j` (via `ThreadsCalculator`) when not specified.
3. Calls `run_pytest()` — assembles pytest arguments and invokes `pytest.main()`.
4. Optionally generates a coverage report for the `"coverage"` build mode.

`test.py` does **not** perform environment setup, test discovery, or result
parsing. All of those responsibilities are handled by `runner.py` (the pytest
plugin) which is loaded automatically when pytest runs.

The file is approximately 334 lines.

---

## 2. Imports and Constants

### 2.1 Suite Framework Imports

From `test.pylib.suite.base`:
- `palette` — color formatting for terminal output (used in `parse_cmd_line`
  error messages)

From `test/__init__.py`:
- `ALL_MODES` — dict mapping mode names to CMake build types
- `HOST_ID` — unique host identifier for parallel CI runs
- `TOP_SRC_DIR` — repository root path
- `TEST_DIR` — `TOP_SRC_DIR / "test"`
- `path_to` — resolves paths to built executables

From `test.pylib.util`:
- `get_configured_modes` — reads build modes from `ninja mode_list`

External libraries: `argparse`, `colorama`, `pytest`.

### 2.2 Constants

**`PYTEST_RUNNER_DIRECTORIES`** — a list of 13 `pathlib.Path` objects
identifying directories whose tests are run via the in-process pytest pipeline:

```
test/boost, test/ldap, test/raft, test/unit, test/vector_search,
test/alternator, test/broadcast_tables, test/cql, test/cqlpy,
test/rest_api, test/nodetool, test/scylla_gdb, test/cluster
```

---

## 3. Command-Line Interface

### 3.1 `parse_cmd_line() -> argparse.Namespace`

Creates an `ArgumentParser` with `RawTextHelpFormatter` and configures the
following arguments:

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `name` (positional) | `list[str]` | `[]` | Test names or paths to filter |
| `--tmpdir` | `str` | `TOP_SRC_DIR / "testlog"` | Temp/log directory |
| `--gather-metrics` | `BooleanOptionalAction` | `True` | Enable resource metrics gathering |
| `--max-failures` | `int` | `0` (unlimited) | Stop after N failures |
| `--mode` | `choices(ALL_MODES)`, append | (auto-detect) | Build modes to test |
| `--repeat` | `int` | `1` | Repeat each test N times |
| `--timeout` | `int` | `3600` | Per-test timeout in seconds |
| `--session-timeout` | `int` | `24000` | Total session timeout in seconds |
| `--verbose` / `-v` | `bool` | `False` | Verbose output |
| `--quiet` / `-q` | `bool` | `False` | Quiet output |
| `--jobs` / `-j` | `int` | (computed) | Concurrent test count |
| `--save-log-on-success` / `-s` | `bool` | `False` | Keep logs of passing tests |
| `--list` | `bool` | `False` | List tests without executing |
| `--skip` | `list[str]` | `None` | Skip pattern list |
| `--cpus` | `str` | `None` | Taskset-format CPU affinity |
| `-k` | `str` | `None` | Pytest expression filter |
| `--markers` | `str` | `None` | Pytest mark expression |
| `--coverage` | `bool` | `False` | Enable coverage processing |
| `--coverage-mode` | `list[str]` | `None` | Coverage for specific modes |
| `--cluster-pool-size` | `int` | `None` | Override cluster pool size |
| `--byte-limit` | `int` | `randint(0,2000)` | Failure injection byte limit |
| `--pytest-arg` | `str` | `None` | Extra pytest arguments |
| `--exe-path` | `str` | `False` | Custom executable path |
| `--exe-url` | `str` | `False` | URL to download executable |
| `--extra-scylla-cmdline-options` | `str` | `""` | Extra Scylla CLI options |
| `--random-seed` | `str` | `None` | Boost test RNG seed |

### 3.2 Post-Parse Validation

1. `--skip` and `-k` are mutually exclusive — produces an error via
   `parser.error()` with colored output.
2. If no `--mode` specified, auto-detects via `get_configured_modes()` (reads
   `ninja mode_list`). On failure, prints a colored error message suggesting
   `./configure.py` first.
3. If no `--jobs` specified, computes via `ThreadsCalculator`:
   - If `--cpus` is set, runs `taskset -c <cpus> python3 -c '...'`
     to get the effective CPU count.
   - Otherwise uses `multiprocessing.cpu_count()`.
4. Coverage mode validation:
   - If `--coverage` without `--coverage-mode`: uses all requested modes except
     `"coverage"`. If no modes remain, disables coverage.
   - If `--coverage-mode` without `--coverage`: implies `--coverage`.
   - `"coverage"` mode is explicitly forbidden in `--coverage-mode`.
   - Validates that all `--coverage-mode` values are in `--mode`.
5. Converts `--tmpdir` to absolute path.

---

## 4. ThreadsCalculator

**Purpose:** Computes the number of concurrent test jobs based on system memory
and CPU constraints.

### Constructor Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `modes` | (required) | List of build modes being tested |
| `min_system_memory_reserve` | `5e9` | Minimum bytes reserved for OS |
| `max_system_memory_reserve` | `8e9` | Maximum bytes reserved for OS |
| `system_memory_reserve_fraction` | `16` | Fraction of total memory to reserve |
| `max_test_memory` | `5e9` | Max memory per test (debug) |
| `test_memory_fraction` | `8.0` | Fraction of total memory per test |
| `debug_test_memory_multiplier` | `1.5` | Memory multiplier for debug mode |
| `debug_cpus_per_test_job` | `1.5` | CPUs per job in debug mode |
| `non_debug_cpus_per_test_job` | `1.0` | CPUs per job in non-debug mode |
| `non_debug_max_test_memory` | `4e9` | Max memory per test (non-debug) |

### Algorithm

1. Read total system memory via `os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")`.
2. Compute `system_memory_reserve = clamp(sys_mem/fraction, min_reserve, max_reserve)`.
3. `available_mem = max(0, sys_mem - system_memory_reserve)`.
4. Compute `test_mem` based on whether `"debug"` is in modes:
   - Debug: `min(sys_mem/8, 5e9) * 1.5`
   - Non-debug: `min(sys_mem/8, 4e9)`
5. `default_num_jobs_mem = max(1, available_mem // test_mem)`.
6. Store `cpus_per_test_job` (1.5 for debug, 1.0 for non-debug).

### `get_number_of_threads(nr_cpus: int) -> int`

Returns `min(default_num_jobs_mem, max(1, ceil(nr_cpus / cpus_per_test_job)))`.

---

## 5. Pytest Runner Bridge

### `run_pytest(options: argparse.Namespace) -> int`

Runs tests via an in-process `pytest.main()` call and returns the exit code.

### 5.1 File Selection

- If `options.name` is specified: filters names to those whose paths are relative
  to any `PYTEST_RUNNER_DIRECTORIES` entry. Supports `::` syntax for function
  selection.
- If no names specified: runs all tests under `TOP_SRC_DIR / 'test/'`.
- If no files match after filtering: logs a skip message and returns `0`.

### 5.2 Argument Construction

Always included: `--color=yes`, `--repeat=N`, `--mode=M` (for each mode).

In list-tests mode (`--list`): adds `--collect-only`, `--quiet`, `--no-header`.

In execution mode: adds:
- `--log-level=DEBUG`, `--junit-xml=<path>`, `-rf`
- `-n<jobs>` (xdist parallelism), `--tmpdir=<path>`, `--maxfail=<N>`
- `--alluredir=<path>`, `--dist=worksteal`

Conditional arguments are appended for: `--verbose`, `--quiet` (with
`-p no:sugar`), `--pytest-arg` (shell-split via `shlex.split`), `--random-seed`,
`--gather-metrics`, `--timeout`, `--session-timeout`, `--skip` (converted to
`-k` expression via `not pattern1 and not pattern2`), `-k`,
`--extra-scylla-cmdline-options`, `--save-log-on-success` / `--allure-no-capture`,
`--markers`.

### 5.3 Return Value

Returns the integer exit code from `pytest.main()`.

---

## 6. Main Orchestrator

### `main() -> int`

The top-level function that orchestrates the test run:

1. **Parse CLI**: `options = parse_cmd_line()`.
2. **List mode**: if `--list`, calls `run_pytest(options)` for collection listing,
   then returns 0.
3. **Run tests**: `exit_code = run_pytest(options)`.
4. **Legacy coverage**: if `"coverage"` build mode is active, calls
   `coverage.generate_coverage_report()`.
5. **Exit code**: returns `exit_code` from pytest.

---

## 7. Entry Point

The `if __name__ == "__main__"` block:

1. Calls `colorama.init()` for cross-platform ANSI color support.
2. Checks Python version >= 3.11, exits with error if not met.
3. Calls `sys.exit(main())`.

Note: `SCYLLA_CONF` and `SCYLLA_HOME` environment cleanup (gh-16583) was
moved to `test/conftest.py` `pytest_configure` hook, so it runs for all
execution modes (test.py, bare pytest, and run.py).

---

## Appendix: Integration with runner.py

test.py delegates all of the following to the pytest plugin (`runner.py`):

| Responsibility | Handled by |
|----------------|-----------|
| Environment setup (dirs, services) | `pytest_sessionstart` → `prepare_environment()` |
| Resource monitoring | `_start_resource_watcher()` / `_stop_resource_watcher()` in `pytest_sessionstart`/`pytest_sessionfinish` |
| Test discovery and collection | pytest + `pytest_collect_file` multiplexing |
| Mode/repeat multiplexing | `pytest_collect_file` + `pytest_collection_modifyitems` |
| Suite config filtering | `TestSuiteConfig.from_pytest_node()` |
| Failure log capture | `pytest_runtest_makereport` |
| JUnit XML customization | `pytest_runtest_logreport` |
| Artifact cleanup | `pytest_sessionfinish` → `TestSuite.artifacts.cleanup_before_exit()` |
| Logging setup | `pytest_configure` |
| `SCYLLA_CONF`/`SCYLLA_HOME` cleanup | `test/conftest.py` `pytest_configure` |
