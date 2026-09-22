"""Tests for sizing the variant-evaluation process pool to the machine."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import scheduler.platform as platform
from scheduler import NurseScheduler


@pytest.fixture
def machine(monkeypatch):
    """Pretend to be a given machine: cores, affinity, free memory, and OS."""

    def configure(*, physical=16, logical=22, available_mb=24_000, os_name="linux"):
        monkeypatch.delenv(platform.WORKER_COUNT_ENV, raising=False)
        monkeypatch.setattr(platform, "physical_core_count", lambda: physical)
        monkeypatch.setattr(platform, "usable_cpu_count", lambda: logical)
        monkeypatch.setattr(platform, "_available_memory_mb", lambda: available_mb)
        monkeypatch.setattr(platform.sys, "platform", os_name)

    return configure


def test_uses_physical_cores_less_one(machine):
    # e.g. a Core Ultra 7 155H: 16 cores, 22 threads.
    machine(physical=16, logical=22)
    assert platform.default_worker_count() == 15


def test_affinity_mask_limits_workers(machine):
    machine(physical=16, logical=4)
    assert platform.default_worker_count() == 3


def test_low_free_memory_limits_workers(machine):
    machine(physical=16, logical=22, available_mb=platform.WORKER_MEMORY_BUDGET_MB * 5)
    assert platform.default_worker_count() == 5


def test_unknown_memory_does_not_limit_workers(machine):
    machine(physical=8, logical=8, available_mb=None)
    assert platform.default_worker_count() == 7


def test_windows_pool_limit(machine):
    machine(physical=128, logical=256, available_mb=10**6, os_name="win32")
    assert platform.default_worker_count() == 61


def test_single_core_still_gets_one_worker(machine):
    machine(physical=1, logical=1, available_mb=10)
    assert platform.default_worker_count() == 1


def test_environment_override(machine, monkeypatch):
    machine(physical=16, logical=22)
    monkeypatch.setenv(platform.WORKER_COUNT_ENV, "6")
    assert platform.default_worker_count() == 6


def test_invalid_environment_override_is_ignored(machine, monkeypatch):
    machine(physical=16, logical=22)
    monkeypatch.setenv(platform.WORKER_COUNT_ENV, "lots")
    assert platform.default_worker_count() == 15


def test_physical_core_count_falls_back_without_psutil(monkeypatch):
    monkeypatch.setattr(platform, "psutil", None)
    monkeypatch.setattr(platform.os, "cpu_count", lambda: 12)
    assert platform.physical_core_count() == 12


def test_physical_core_count_falls_back_when_psutil_cannot_tell(monkeypatch):
    monkeypatch.setattr(platform, "psutil", SimpleNamespace(cpu_count=lambda logical=True: None))
    monkeypatch.setattr(platform.os, "cpu_count", lambda: 12)
    assert platform.physical_core_count() == 12


def test_engine_sizes_pool_from_machine_when_unspecified(monkeypatch):
    import scheduler.engine as engine

    monkeypatch.setattr(engine, "default_worker_count", lambda: 15)
    monkeypatch.setattr(engine, "usable_cpu_count", lambda: 22)
    assert NurseScheduler._resolve_worker_count(None, 500) == 15
    assert NurseScheduler._resolve_worker_count(None, 3) == 3


def test_engine_explicit_worker_count_is_capped_by_cpus(monkeypatch):
    import scheduler.engine as engine

    monkeypatch.setattr(engine, "usable_cpu_count", lambda: 4)
    assert NurseScheduler._resolve_worker_count(2, 500) == 2
    assert NurseScheduler._resolve_worker_count(32, 500) == 4
