#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

"""Shared fixtures for test/pylib/suite/ unit tests."""

import argparse
import pathlib
from unittest.mock import patch

import pytest
import yaml

from test.pylib.suite.base import TestSuite


# ---------------------------------------------------------------------------
# Autouse fixture: reset class-level mutable state between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_suite_state():
    """Reset TestSuite class-level shared state before *and* after each test."""
    TestSuite.suites.clear()
    TestSuite._next_id.clear()
    yield
    TestSuite.suites.clear()
    TestSuite._next_id.clear()


# ---------------------------------------------------------------------------
# Mock argparse.Namespace that looks like the options various constructors expect
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_options(tmp_path: pathlib.Path) -> argparse.Namespace:
    """Minimal options namespace accepted by TestSuite / Test constructors."""
    return argparse.Namespace(
        tmpdir=str(tmp_path),
        coverage=False,
        coverage_modes=[],
        save_log_on_success=False,
        gather_metrics=False,
        markers=None,
        name=None,
        repeat=1,
        skip_patterns=None,
        # PythonTestSuite needs these
        cluster_pool_size=None,
        extra_scylla_cmdline_options="",
    )


# ---------------------------------------------------------------------------
# Sample YAML config dicts
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_cfg() -> dict:
    """A minimal test_config.yaml dict (type=Python)."""
    return {
        "type": "Python",
    }


@pytest.fixture
def complex_cfg() -> dict:
    """A more realistic config with disabled/flaky/run_in settings."""
    return {
        "type": "Python",
        "disable": ["broken_test"],
        "flaky": ["flaky_test"],
        "skip_in_debug": ["slow_test"],
        "skip_in_debug_modes": ["very_slow_test"],
        "run_in_release": ["release_only_test"],
        "run_in_debug": ["debug_only_test"],
        "run_first": ["important_test"],
        "no_parallel_cases": ["serial_test"],
        "pool_size": 3,
        "coverage": True,
    }


# ---------------------------------------------------------------------------
# Temporary suite directory with a test_config.yaml on disk
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_suite_dir(tmp_path: pathlib.Path, sample_cfg: dict) -> pathlib.Path:
    """Create a temporary suite directory containing test_config.yaml."""
    suite_dir = tmp_path / "my_suite"
    suite_dir.mkdir()
    config_file = suite_dir / "test_config.yaml"
    config_file.write_text(yaml.dump(sample_cfg), encoding="utf-8")
    return suite_dir
