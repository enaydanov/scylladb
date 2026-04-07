#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-present ScyllaDB
#
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

from __future__ import annotations

import argparse
import math
import shlex
import textwrap
from random import randint

import pytest

import colorama
import logging
import multiprocessing
import os
import pathlib
import subprocess
import sys

from scripts import coverage
from test import ALL_MODES, HOST_ID, TOP_SRC_DIR, path_to, TEST_DIR
from test.pylib.suite import palette
from test.pylib.util import get_configured_modes

PYTEST_RUNNER_DIRECTORIES = [
    TEST_DIR / 'boost',
    TEST_DIR / 'ldap',
    TEST_DIR / 'raft',
    TEST_DIR / 'unit',
    TEST_DIR / 'vector_search',
    TEST_DIR / 'alternator',
    TEST_DIR / 'broadcast_tables',
    TEST_DIR / 'cql',
    TEST_DIR / 'cqlpy',
    TEST_DIR / 'rest_api',
    TEST_DIR / 'nodetool',
    TEST_DIR / 'scylla_gdb',
    TEST_DIR / 'cluster',
]


class ThreadsCalculator:
    """
    The ThreadsCalculator class calculates the number of jobs that can be run concurrently based on system
    memory and CPU constraints. It allows resource reservation and configurable parameters for
    flexible job scheduling in various modes, such as `debug`.
    """

    def __init__(self,
                 modes: list[str],
                 min_system_memory_reserve: float = 5e9,
                 max_system_memory_reserve: float = 8e9,
                 system_memory_reserve_fraction = 16,
                 max_test_memory: float = 5e9,
                 test_memory_fraction: float = 8.0,
                 debug_test_memory_multiplier: float = 1.5,
                 debug_cpus_per_test_job=1.5,
                 non_debug_cpus_per_test_job: float =1.0,
                 non_debug_max_test_memory: float = 4e9
                 ):
        sys_mem = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        test_mem = min(sys_mem / test_memory_fraction, max_test_memory)
        if "debug" in modes:
            test_mem *= debug_test_memory_multiplier
        system_memory_reserve = int(min(
            max(sys_mem / system_memory_reserve_fraction, min_system_memory_reserve),
            max_system_memory_reserve,
        ))
        available_mem = max(0, sys_mem - system_memory_reserve)
        is_debug = "debug" in modes
        test_mem = min(
            sys_mem / test_memory_fraction,
            max_test_memory if is_debug else non_debug_max_test_memory,
        )
        if is_debug:
            test_mem *= debug_test_memory_multiplier
        self.cpus_per_test_job = (
            debug_cpus_per_test_job if is_debug else non_debug_cpus_per_test_job
        )
        self.default_num_jobs_mem = max(1, int(available_mem // test_mem))

    def get_number_of_threads(self, nr_cpus: int) -> int:
        default_num_jobs_cpu = max(1, math.ceil(nr_cpus / self.cpus_per_test_job))
        return min(self.default_num_jobs_mem, default_num_jobs_cpu)



def parse_cmd_line() -> argparse.Namespace:
    """ Print usage and process command line options. """
    parser = argparse.ArgumentParser(description='Scylla test runner', formatter_class=argparse.RawTextHelpFormatter)

    directories = '\n'.join(f" - {str(item.relative_to(TOP_SRC_DIR))}" for item in PYTEST_RUNNER_DIRECTORIES)
    name_help = textwrap.dedent("""\
        Can be empty. List of test names or path to test files, to look for.
        There are two runners: test.py and pytest.

        test.py works in the following way:
        Each name is used as a substring to look for in the path to test file,
        e.g. "mem" will run all tests that have "mem" in their name in all
        suites, "nodetool/test_compact" will only enable tests starting with
        "test_compact" in "nodetool" suite, and 
        "nodetool/test_compact::test_all_keyspaces" to narrow down to a 
        certain test case.

        pytest runner works in the following way:
        provide the path to the test file for execution or path to the directory
        to narrow you can use function name 'test/boost/aggregate_fcts_test.cc::test_aggregate_avg'

        Pytest directories are:
        """) + directories + "\n\nDefault: run all tests in all suites."

    parser.add_argument(
        "name",
        nargs="*",
        action="store",
        help=name_help,
    )
    parser.add_argument("--tmpdir", action="store", default=str(TOP_SRC_DIR / "testlog"),
                        help="Path to temporary test data and log files.  The data is further segregated per build mode.")
    parser.add_argument("--gather-metrics", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-failures", type=int, default=0,
                        help="Maximum number of failures to tolerate before cancelling rest of tests.")
    parser.add_argument('--mode', choices=ALL_MODES, action="append", dest="modes",
                        help="Run only tests for given build mode(s)")
    parser.add_argument('--repeat', action="store", default="1", type=int,
                        help="number of times to repeat test execution")
    parser.add_argument('--timeout', action="store", default="3600", type=int,
                        help="timeout value for single test execution")
    parser.add_argument('--session-timeout', action="store", default="24000", type=int,
                        help="timeout value for test.py/pytest session execution")
    parser.add_argument('--verbose', '-v', action='store_true', default=False,
                        help='Verbose reporting')
    parser.add_argument('--quiet', '-q', action='store_true', default=False,
                        help='Quiet reporting')
    parser.add_argument('--jobs', '-j', action="store", type=int,
                        help="Number of jobs to use for running the tests")
    parser.add_argument('--save-log-on-success', "-s", default=False,
                        dest="save_log_on_success", action="store_true",
                        help="Save test log output on success and skip cleanup before the run.")
    parser.add_argument('--list', dest="list_tests", action="store_true", default=False,
                        help="Print list of tests instead of executing them")
    parser.add_argument('--skip',
                        dest="skip_patterns", action="append",
                        help="Skip tests which match the provided pattern")
    parser.add_argument('--cpus', action="store",
                        help="Run the tests on those CPUs only (in taskset"
                        " acceptable format). Consider using --jobs too")
    parser.add_argument('-k', metavar="EXPRESSION", action="store",
                        help=f"Supported only for tests in {[str(d) for d in PYTEST_RUNNER_DIRECTORIES]} "
                        "directories. Only run tests which match the given substring expression. An expression is a Python evaluable expression where all names are "
                        "substring-matched against test names and their parent classes. Example: -k 'test_method or test_other' matches all test functions and "
                        "classes whose name contains 'test_method' or 'test_other', while -k 'not test_method' matches those that don't contain 'test_method' "
                        "in their names. -k 'not test_method and not test_other' will eliminate the matches. Additionally keywords are matched to classes and "
                        "functions containing extra names in their 'extra_keyword_matches' set, as well as functions which have names assigned directly to "
                        "them. The matching is case-insensitive.")
    parser.add_argument('--markers', action='store', metavar='MARKEXPR',
                        help="Only run tests that match the given mark expression. The syntax is the same "
                             "as in pytest, for example: --markers 'mark1 and not mark2'. The parameter "
                             "is only supported by python tests for now, other tests ignore it. "
                             "By default, the marker filter is not applied and all tests will be run without exception."
                             "To exclude e.g. slow tests you can write --markers 'not slow'.")
    parser.add_argument('--coverage', action = 'store_true', default = False,
                        help="When running code instrumented with coverage support"
                             "Will route the profiles to `tmpdir`/mode/coverage/`suite` and post process them in order to generate "
                             "lcov file per suite, lcov file per mode, and an lcov file for the entire run, "
                             "The lcov files can eventually be used for generating coverage reports")
    parser.add_argument("--coverage-mode",action = 'append', type = str, dest = "coverage_modes",
                        help = "Collect and process coverage only for the modes specified. implies: --coverage, default: All built modes")
    parser.add_argument("--cluster-pool-size", action="store", default=None, type=int,
                        help="Set the pool_size for PythonTest and its descendants. Alternatively environment variable "
                             "CLUSTER_POOL_SIZE can be used to achieve the same")
    parser.add_argument('--byte-limit', action="store", default=randint(0, 2000), type=int,
                        help="Specific byte limit for failure injection (random by default)")
    parser.add_argument("--pytest-arg", action='store', type=str,
                        default=None, dest="pytest_arg",
                        help="Additional command line arguments to pass to pytest, for example ./test.py --pytest-arg=\"-v -x\"")
    parser.add_argument('--exe-path', default=False,
                     dest="exe_path", action="store",
                     help="Path to the executable to run. Not working with `mode`")
    parser.add_argument('--exe-url', default=False,
                     dest="exe_url", action="store",
                     help="URL to download the relocatable executable. Not working with `mode`")
    scylla_additional_options = parser.add_argument_group('Additional options for Scylla tests')
    scylla_additional_options.add_argument('--extra-scylla-cmdline-options', action="store", default="", type=str,
                                           help="Passing extra scylla cmdline options for all tests. Options should be space separated:"
                                                "'--logger-log-level raft=trace --default-log-level error'")

    boost_group = parser.add_argument_group('boost suite options')
    boost_group.add_argument('--random-seed', action="store",
                             help="Random number generator seed to be used by boost tests")

    args = parser.parse_args()

    if args.skip_patterns and args.k:
        parser.error(palette.fail('arguments --skip and -k are mutually exclusive, please use only one of them'))

    if not args.modes:
        try:
            args.modes = get_configured_modes()
        except Exception:
            print(palette.fail("Failed to read output of `ninja mode_list`: please run ./configure.py first"))
            raise

    if not args.jobs:
        if not args.cpus:
            nr_cpus = multiprocessing.cpu_count()
        else:
            nr_cpus = int(subprocess.check_output(
                ['taskset', '-c', args.cpus, 'python3', '-c',
                 'import os; print(len(os.sched_getaffinity(0)))']))
        args.jobs = ThreadsCalculator(args.modes).get_number_of_threads(nr_cpus)

    if not args.coverage_modes and args.coverage:
        args.coverage_modes = list(args.modes)
        if "coverage" in args.coverage_modes:
            args.coverage_modes.remove("coverage")
        if not args.coverage_modes:
            args.coverage = False
    elif args.coverage_modes:
        if "coverage" in args.coverage_modes:
            raise RuntimeError("'coverage' mode is not allowed in --coverage-mode")
        missing_coverage_modes = set(args.coverage_modes).difference(set(args.modes))
        if len(missing_coverage_modes) > 0:
            raise RuntimeError(f"The following modes weren't built or ran (using the '--mode' option): {missing_coverage_modes}")
        args.coverage = True

    args.tmpdir = os.path.abspath(args.tmpdir)

    return args


def run_pytest(options: argparse.Namespace) -> int:
    # When tests are executed in parallel on different hosts, we need to distinguish results from them.
    # So HOST_ID needed to not overwrite results from different hosts during Jenkins will copy to one directory.

    temp_dir = pathlib.Path(options.tmpdir).absolute()
    report_dir =  temp_dir / 'report'
    junit_output_file = report_dir / f'pytest_cpp_{HOST_ID}.xml'
    files_to_run = []
    if options.name:
        for name in options.name:
            file_name = name
            if '::' in name:
                file_name, _ = name.split('::', maxsplit=1)
            if any((TOP_SRC_DIR / file_name).is_relative_to(x) for x in PYTEST_RUNNER_DIRECTORIES):
                files_to_run.append(name)
    else:
        files_to_run = [ TOP_SRC_DIR / 'test/']
    if not files_to_run:
        logging.info('Skipping pytest execution because no tests were selected for pytest.')
        return 0
    args = [
        '--color=yes',
        f'--repeat={options.repeat}',
        *[f'--mode={mode}' for mode in options.modes],
    ]
    if options.list_tests:
        args.extend(['--collect-only', '--quiet', '--no-header'])
    else:
        args.extend([
            "--log-level=DEBUG",  # Capture logs
            f'--junit-xml={junit_output_file}',
            "-rf",
            f'-n{options.jobs}',
            f'--tmpdir={temp_dir}',
            f'--maxfail={options.max_failures}',
            f'--alluredir={report_dir / f"allure_{HOST_ID}"}',
            f'--dist=worksteal',
        ])
    if options.verbose:
        args.append('-v')
    if options.quiet:
        args.append('--quiet')
        args.extend(['-p','no:sugar'])
    if options.pytest_arg:
        # If pytest_arg is provided, it should be a string with arguments to pass to pytest
        args.extend(shlex.split(options.pytest_arg))
    if options.random_seed:
        args.append(f'--random-seed={options.random_seed}')
    if options.gather_metrics:
        args.append('--gather-metrics')
    if options.timeout:
        args.append(f'--timeout={options.timeout}')
    if options.session_timeout:
        args.append(f'--session-timeout={options.session_timeout}')
    if options.skip_patterns:
        args.append(f'-k={" and ".join([f"not {pattern}" for pattern in options.skip_patterns])}')
    if options.k:
        args.append(f'-k={options.k}')
    if options.extra_scylla_cmdline_options:
        args.append(f'--extra-scylla-cmdline-options={options.extra_scylla_cmdline_options}')
    if not options.save_log_on_success:
        args.append('--allure-no-capture')
    else:
        args.append('--save-log-on-success')
    if options.markers:
        args.append(f'-m={options.markers}')
    if options.coverage:
        args.append('--coverage')
        for mode in (options.coverage_modes or []):
            args.append(f'--coverage-mode={mode}')
    args.append(f'--byte-limit={options.byte_limit}')
    if options.exe_path:
        args.append(f'--exe-path={options.exe_path}')
    if options.exe_url:
        args.append(f'--exe-url={options.exe_url}')
    args.extend(files_to_run)
    return pytest.main(args=args)


def main() -> int:

    options = parse_cmd_line()

    if options.list_tests:
        run_pytest(options)
        return 0

    exit_code = run_pytest(options)

    if 'coverage' in options.modes:
        coverage.generate_coverage_report(path_to("coverage", "tests"))

    # Note: failure codes must be in the ranges 0-124, 126-127,
    #       to cooperate with git bisect's expectations
    return exit_code


if __name__ == "__main__":
    colorama.init()

    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required to run this program")
        sys.exit(-1)
    sys.exit(main())
