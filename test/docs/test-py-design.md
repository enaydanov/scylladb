# test.py Design Document

This document describes the legacy async test runner `test.py` located at the
repository root. It covers the file's complete structure, behavior, and
interactions with the suite framework.

**Related document:** [Test Suite Framework Design](test-suite-design.md)

## Table of Contents

1. [Overview](#1-overview)
2. [Imports and Constants](#2-imports-and-constants)
3. [Command-Line Interface](#3-command-line-interface)
4. [ThreadsCalculator](#4-threadscalculator)
5. [Signal Handling](#5-signal-handling)
6. [Pytest Runner Bridge](#6-pytest-runner-bridge)
7. [Async Test Execution](#7-async-test-execution)
8. [Summary and Reporting](#8-summary-and-reporting)
9. [Coverage Processing](#9-coverage-processing)
10. [Main Orchestrator](#10-main-orchestrator)
11. [Entry Point](#11-entry-point)

---

## 1. Overview

> **Note:** `test.py` is one of three execution modes for the test suite.
> The other two are bare pytest (invoking pytest directly, with `runner.py`
> loaded as a plugin) and `run.py` scripts (`test/cqlpy/run`,
> `test/alternator/run`, `test/rest_api/run`) that start Scylla externally
> and set `SCYLLA_TEST_RUNNER=runpy`.

`test.py` is the legacy, top-level test runner for the ScyllaDB project. It is
an async Python script that:

1. Parses extensive command-line arguments (~30 options).
2. Calls `run_pytest()` -- **this is where all tests actually run**.
3. Processes code coverage data through a 5-stage pipeline.
4. Prints a summary with CPU utilization, failure details, and test counts.

`test.py` does not discover or execute tests directly.  It delegates entirely
to `run_pytest()`.

The file is approximately 763 lines and serves as the primary entry point when
running `./test.py` from the repository root.

---

## 2. Imports and Constants

### 2.1 Suite Framework Imports

From `test.pylib.suite.base`:
- `Test` -- referenced for `print_summary` method on SimpleNamespace shims
- `TestSuite` -- used for `opt_create()`, `test_count()`, `all_tests()`,
  `artifacts`, `hosts` class-level state
- `init_testsuite_globals` -- called once to set up global registries
- `palette` -- color formatting for terminal output
- `prepare_environment` -- initializes directories and third-party services

### 2.2 Other Test Infrastructure Imports

From `test/__init__.py`:
- `ALL_MODES` -- dict mapping mode names to CMake build types
- `HOST_ID` -- unique host identifier for parallel CI runs
- `TOP_SRC_DIR` -- repository root path
- `TEST_DIR` -- `TOP_SRC_DIR / "test"`
- `path_to` -- resolves paths to built executables
- `TESTPY_PREPARED_ENVIRONMENT` -- env var name signaling environment is ready

From `test.pylib.util`:
- `LogPrefixAdapter` -- logging adapter for prefixed log messages
- `get_configured_modes` -- reads build modes from `ninja mode_list`

External libraries: `argparse`, `asyncio`, `colorama`, `humanfriendly`,
`pytest`, `treelib`, `xml.etree.ElementTree`.

### 2.3 Constants

**`PYTEST_RUNNER_DIRECTORIES`** -- a list of 13 `pathlib.Path` objects
identifying directories whose tests are run via the in-process pytest pipeline:

```
test/boost, test/ldap, test/raft, test/unit, test/vector_search,
test/alternator, test/broadcast_tables, test/cql, test/cqlpy,
test/rest_api, test/nodetool, test/scylla_gdb, test/cluster
```

**`launch_time`** -- `time.monotonic()` captured at module load, used for CPU
utilization calculation.

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
| `--no-parallel-cases` | `bool` | `True` -> `False` | Disable parallel cases |
| `--cpus` | `str` | `None` | Taskset-format CPU affinity |
| `--log-level` | `choices` | `"INFO"` | Python log level |
| `-k` | `str` | `None` | Pytest expression filter |
| `--markers` | `str` | `None` | Pytest mark expression |
| `--coverage` | `bool` | `False` | Enable coverage processing |
| `--coverage-mode` | `list[str]` | `None` | Coverage for specific modes |
| `--coverage-keep-raw` | `bool` | `False` | Keep raw profiles |
| `--coverage-keep-indexed` | `bool` | `False` | Keep indexed profiles |
| `--coverage-keep-lcovs` | `bool` | `False` | Keep intermediate lcov traces |
| `--artifacts_dir_url` | `str` | `None` | URL prefix for CI artifact links |
| `--cluster-pool-size` | `int` | `None` | Override cluster pool size |
| `--manual-execution` | `bool` | `False` | Pause for manual test execution |
| `--byte-limit` | `int` | `randint(0,2000)` | Failure injection byte limit |
| `--skip-internet-dependent-tests` | `bool` | `False` | Skip internet-dependent tests |
| `--pytest-arg` | `str` | `None` | Extra pytest arguments |
| `--exe-path` | `str` | `False` | Custom executable path |
| `--exe-url` | `str` | `False` | URL to download executable |
| `--extra-scylla-cmdline-options` | `str` | `""` | Extra Scylla CLI options |
| `--random-seed` | `str` | `None` | Boost test RNG seed |

### 3.2 Post-Parse Validation

1. `--skip` and `-k` are mutually exclusive -- produces an error via
   `parser.error()` with colored output.
2. If no `--mode` specified, auto-detects via `get_configured_modes()` (reads
   `ninja mode_list`). On failure, prints a colored error message suggesting
   `./configure.py` first.
3. If no `--jobs` specified, computes via `ThreadsCalculator`:
   - If `--cpus` is set, runs `taskset -c <cpus> python3 -c 'print(len(os.sched_getaffinity(0)))'`
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

## 5. Signal Handling

### `setup_signal_handlers(loop, signaled)`

Registers handlers for `SIGINT` and `SIGTERM` on the asyncio event loop.

When a signal arrives:
1. Prints "Shutdown requested... Aborting tests:"
2. Stores the signal number in `signaled.signo`.
3. Sets the `signaled` asyncio Event.

Uses a lambda wrapper around `asyncio.create_task()` to avoid creating dangling
coroutines if no signal is ever delivered.

---

## 6. Pytest Runner Bridge

### `run_pytest(options: argparse.Namespace) -> tuple[int, list[SimpleNamespace]]`

Runs tests from `PYTEST_RUNNER_DIRECTORIES` via an in-process `pytest.main()`
call. This is **not** the same as the suite framework's per-test pytest
invocations -- this runs all pytest-discoverable tests in a single session.

### 6.1 File Selection

- If `options.name` is specified: filters names to those whose paths are relative
  to any `PYTEST_RUNNER_DIRECTORIES` entry. Supports `::` syntax for function
  selection.
- If no names specified: runs all tests under `TOP_SRC_DIR / 'test/'`.
- If no files match after filtering: logs a skip message and returns `(0, [])`.

### 6.2 Argument Construction

Always included: `--color=yes`, `--repeat=N`, `--mode=M` (for each mode).

In list-tests mode (`--list`): adds `--collect-only`, `--quiet`, `--no-header`.

In execution mode: adds:
- `--log-level=DEBUG`, `--junit-xml=<path>`, `-rf`
- `--test-py-init` (triggers suite framework initialization in the pytest plugin)
- `-n<jobs>` (xdist parallelism), `--tmpdir=<path>`, `--maxfail=<N>`
- `--alluredir=<path>`, `--dist=worksteal`

Conditional arguments are appended for: `--verbose`, `--quiet` (with
`-p no:sugar`), `--pytest-arg` (shell-split via `shlex.split`), `--random-seed`,
`--gather-metrics`, `--timeout`, `--session-timeout`, `--skip` (converted to
`-k` expression via `not pattern1 and not pattern2`), `-k`,
`--extra-scylla-cmdline-options`, `--save-log-on-success` / `--allure-no-capture`,
`--markers`.

### 6.3 JUnit XML Parsing

After `pytest.main()` returns, parses the JUnit XML output file
(`pytest_cpp_<HOST_ID>.xml`) to extract failed tests:

1. Finds the `<testsuite>` element and reads `tests` count.
2. For each `<testcase>` with an `<error>` or `<failure>` child:
   - Extracts the file path from `classname` (handles `.cc` vs `.py` extensions).
   - Extracts the test name from `name` (strips mode suffix after `.`).
   - Creates a `SimpleNamespace` with a `name` attribute and a `print_summary`
     attribute set to `Test.print_summary` (the base class method, used as an
     unbound function reference for polymorphic summary printing).

Returns `(total_tests, failed_tests)`.

---

## 7. Async Test Execution

### `run_all_tests(signaled, options) -> tuple[int, list[SimpleNamespace]] | None` (async)

This function is a thin wrapper:

1. **Pytest pipeline** (blocking): calls `run_pytest(options)` in a thread
   executor (`loop.run_in_executor(None, run_pytest, options)`) to avoid blocking
   the event loop while resource monitoring runs concurrently.

2. **Cleanup registration**: registers `TestSuite.hosts.cleanup` as an exit
   artifact on `TestSuite.artifacts`.

3. **Cleanup**: calls `TestSuite.artifacts.cleanup_before_exit()` in `finally`.

Returns `(total_tests, failed_pytest_tests)`.

---

## 8. Summary and Reporting

### `print_summary(options, failed_pytest_tests, total_tests_pytest)`

Prints a unified summary:

1. **CPU utilization**: computes from `resource.getrusage(RUSAGE_CHILDREN)` user +
   system time, divided by wall-time * CPU count.
2. **Total tests**: `total_tests_pytest`.
3. **Failure report**: if any failures exist, prints the list of failed test names
   and a summary count.
4. **No-tests warning**: if `total_tests == 0`, prints a warning message
   suggesting the user may need to use file paths with extensions, listing
   `PYTEST_RUNNER_DIRECTORIES` as the directories that require this.

### `open_log(tmpdir, log_file_name, log_level)`

Sets up Python logging:
- Creates the `tmpdir` directory (with `parents=True`).
- Configures `logging.basicConfig` to a file with format
  `"%(asctime)s.%(msecs)03d %(levelname)s> %(message)s"` and datefmt `"%H:%M:%S"`.
- Logs the full command line at `CRITICAL` level.

---

## 9. Coverage Processing

### `process_coverage(options)` (async)

A 5-stage pipeline for processing LLVM code coverage profiles into consolidated
lcov trace files. Uses `treelib.Tree` for hierarchical statistics tracking and
`humanfriendly` for human-readable size/time formatting.

### 9.1 Setup

- Computes concurrency as `max(int(cpu_count * 0.75), 1)`.
- Builds binary ID map via `coverage_utils.get_binary_ids_map()` for paths under
  each mode's `build/<mode>/scylla`, `build/<mode>/test`, `build/<mode>/seastar`.
- Identifies ran suites via `{test.suite for test in TestSuite.all_tests() if test.suite.need_coverage()}`.
- Loads exclusion patterns from `coverage_excludes.txt`.

### 9.2 Five Stages (per suite)

**Stage 1 -- Raw to Indexed**: `coverage_utils.merge_profiles()` converts
`.profraw` files to indexed profiles. Optionally deletes raw profiles
(`--coverage-keep-raw`).

**Stage 2 -- Indexed to LCOV**: `coverage_utils.profdata_to_lcov()` converts
indexed profiles to lcov trace files using the binary ID map and source
exclusions. Optionally deletes indexed profiles (`--coverage-keep-indexed`).

**Stage 3 -- Suite LCOV Merge**: `coverage_utils.lcov_combine_traces()` merges
per-test lcov files into a single per-suite trace file. If only one trace file
exists, renames it instead of merging. Optionally deletes intermediate lcovs
(`--coverage-keep-lcovs`).

**Stage 4 -- Mode LCOV Merge**: Combines all suite trace files for each mode
into a single `<mode>_coverage.info`.

**Stage 5 -- Total Merge**: Combines all mode trace files into a single
`test_coverage.info`.

### 9.3 Reporting

- Generates a textual report via `lcov --summary` and `lcov --list` to
  `test_coverage_report.txt`.
- Logs hierarchical statistics tree and coverage summary.

> **Note:** `process_coverage()` is currently non-functional because
> `TestSuite.all_tests()` returns an empty iterator (no `suite.yaml` files
> exist).  It is a candidate for extraction or removal in future cleanup.

---

## 10. Main Orchestrator

### `main() -> int` (async)

The top-level async function that orchestrates the entire test run:

1. **Parse CLI**: `options = parse_cmd_line()`.
2. **List mode**: if `--list`, calls `run_pytest(options)` for collection listing,
   then returns 0.
3. **Open log**: creates the main log file at `tmpdir/test.py.<modes>.log`.
4. **Initialize globals**: `init_testsuite_globals()`.
5. **Prepare environment**: `await prepare_environment(...)` with `tempdir_base`,
   `modes`, `gather_metrics`, `save_log_on_success`, `toxiproxy_byte_limit`.
6. **Set environment flag**: `os.environ[TESTPY_PREPARED_ENVIRONMENT] = '1'`
   (signals to the pytest plugin that test.py has already prepared the
   environment).
7. **Resource watcher**: starts `run_resource_watcher()` with signaled and stop
   events.
8. **Signal handlers**: calls `setup_signal_handlers()`.
9. **Run tests**: `total_tests_pytest, failed_pytest_tests = await run_all_tests(signaled, options)`.
10. **Stop resource watcher**: sets stop event, waits with 5-second timeout.
11. **Handle signals**: if signaled, returns `-signaled.signo`.
12. **Print summary**: `print_summary(options, failed_pytest_tests, total_tests_pytest)`.
13. **Legacy coverage**: if `"coverage"` build mode is active, calls
    `coverage.generate_coverage_report()`.
14. **LLVM coverage**: if `--coverage`, calls `await process_coverage(options)`.
15. **Exit code**: returns `0` if no failures (`failed_pytest_tests` is empty),
    `1` otherwise.

---

## 11. Entry Point

The `if __name__ == "__main__"` block:

1. Calls `colorama.init()` for cross-platform ANSI color support.
2. Removes `SCYLLA_CONF` and `SCYLLA_HOME` from `os.environ` if present
   (gh-16583: prevents inherited client host ScyllaDB environment from breaking
   tests).
3. Checks Python version >= 3.11, exits with error if not met.
4. Calls `sys.exit(asyncio.run(main()))`.

---

## Appendix: Suite Framework Integration Points

### Functions/Classes Imported from Suite Framework

| Symbol | Source | Usage in test.py |
|--------|--------|-----------------|
| `Test` | `base.py` | `run_pytest()` -- `Test.print_summary` as unbound method reference |
| `TestSuite` | `base.py` | Factory (`opt_create`), class state (`artifacts`, `hosts`) |
| `init_testsuite_globals` | `base.py` | `main()` -- one-time global setup |
| `palette` | `base.py` | Multiple locations -- color formatting |
| `prepare_environment` | `base.py` | `main()` -- directory and service setup |

### Data Flow Between test.py and Suite Framework

```
test.py                          Suite Framework
-------                          ---------------
parse_cmd_line()
      |
      v
run_all_tests()
      |--- run_pytest(options) ---------> pytest.main() with --test-py-init
      |                                      |
      |                                      v
      |                                   runner.py plugin
      |                                      |--- init_testsuite_globals()
      |                                      |--- prepare_environment()
      |                                      |--- testpy_test fixture
      |                                      |      get_testpy_test() -> TestSuite.opt_create()
      |                                      |--- conftest fixtures use testpy_test.suite
      |                                      v
      |--- TestSuite.artifacts.cleanup() --> exit cleanup
      v
print_summary()
      |--- test.print_summary() -----------> per-test output (for failures)
      v
process_coverage()
      |--- TestSuite.all_tests() ----------> filter suites with need_coverage()
                                             (currently returns empty -- non-functional)
```
