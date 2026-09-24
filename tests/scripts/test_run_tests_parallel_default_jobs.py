"""Regression: -j/--jobs default must not oversubscribe cpu_count.

Multiple kanban tasks each dispatch scripts/run_tests.sh concurrently.
A per-invocation `cpu_count() * N` default compounds across concurrent
invocations and can drive the host's total worker count into the
hundreds regardless of how many CPUs it actually has. The default must
track cpu_count() 1:1 unless the operator explicitly raises it via
HERMES_TEST_WORKERS.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_tests_parallel.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_tests_parallel_default_jobs_under_test", _MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner_module(monkeypatch):
    monkeypatch.delenv("HERMES_TEST_WORKERS", raising=False)
    return _load_module()


def test_default_jobs_does_not_exceed_cpu_count(runner_module, monkeypatch):
    """No implicit oversubscription factor (e.g. the old `* 2`)."""
    monkeypatch.delenv("HERMES_TEST_WORKERS", raising=False)
    cpu_count = os.cpu_count() or 4
    assert runner_module._default_jobs() <= cpu_count
    assert runner_module._default_jobs() == cpu_count


def test_default_jobs_respects_explicit_env_override(runner_module, monkeypatch):
    """HERMES_TEST_WORKERS remains a full override, e.g. for CI (set to 96)."""
    monkeypatch.setenv("HERMES_TEST_WORKERS", "96")
    assert runner_module._default_jobs() == 96


def test_default_jobs_falls_back_to_four_when_cpu_count_unknown(
    runner_module, monkeypatch
):
    monkeypatch.delenv("HERMES_TEST_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert runner_module._default_jobs() == 4
