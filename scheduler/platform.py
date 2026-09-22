"""Small platform-specific helpers kept outside the scheduling engine."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional in minimal environments
    psutil = None

logger = logging.getLogger(__name__)

# Environment override for the number of variant-evaluation processes.
WORKER_COUNT_ENV = "PACU_MAX_WORKERS"

# Memory reserved per evaluation process when sizing the pool. A worker peaks
# around 70 MB on the demo roster; the budget leaves room for larger rosters
# and longer horizons, and only binds on machines that are short on free RAM.
WORKER_MEMORY_BUDGET_MB = 256

# ProcessPoolExecutor refuses more than 61 workers on Windows.
_WINDOWS_MAX_WORKERS = 61


def usable_cpu_count() -> int:
    """Logical CPUs this process may run on, honouring any affinity mask."""
    if hasattr(os, "sched_getaffinity"):
        try:
            return max(1, len(os.sched_getaffinity(0)))
        except OSError:
            pass
    if psutil is not None:
        try:
            return max(1, len(psutil.Process().cpu_affinity()))
        except Exception:
            pass
    return os.cpu_count() or 1


def physical_core_count() -> int:
    """Physical cores, falling back to the logical count when unknown."""
    if psutil is not None:
        try:
            cores = psutil.cpu_count(logical=False)
        except Exception:
            cores = None
        if cores:
            return cores
    return os.cpu_count() or 1


def _available_memory_mb() -> float | None:
    if psutil is None:
        return None
    try:
        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:
        return None


def default_worker_count() -> int:
    """How many variant-evaluation processes to run on this machine.

    Variant evaluation is CPU-bound pure Python, so it scales with physical
    cores; hyper-threaded siblings add little. One core is left free so the
    GUI and the rest of the machine stay responsive. The count is further
    capped by free memory and by the Windows pool limit. Set
    ``PACU_MAX_WORKERS`` to override the automatic choice.
    """
    override = os.environ.get(WORKER_COUNT_ENV, "").strip()
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            logger.warning("Ignoring %s=%r: not an integer.", WORKER_COUNT_ENV, override)

    workers = min(physical_core_count(), usable_cpu_count()) - 1

    available_mb = _available_memory_mb()
    if available_mb is not None:
        workers = min(workers, int(available_mb // WORKER_MEMORY_BUDGET_MB))

    if sys.platform.startswith("win"):
        workers = min(workers, _WINDOWS_MAX_WORKERS)

    return max(1, workers)


def inhibit_sleep():
    """
    Prevent the screen from going to sleep. Returns a handle
    you must pass to allow_sleep() when you’re done.
    """
    if sys.platform == "darwin":
        # macOS: caffeinate will keep display & system awake
        return subprocess.Popen(["caffeinate", "-dims"])
    elif sys.platform.startswith("win"):
        # Windows: SetThreadExecutionState DISPLAY_REQUIRED
        import ctypes

        ES_CONTINUOUS = 0x80000000
        ES_DISPLAY_REQUIRED = 0x00000002
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED)
        return None
    else:
        # Linux and others: no‐op (you could integrate a DBus inhibit here)
        return None


def allow_sleep(handle):
    """
    Undo whatever inhibit_sleep() did.
    """
    if sys.platform == "darwin":
        if handle:
            handle.terminate()
    elif sys.platform.startswith("win"):
        import ctypes

        ES_CONTINUOUS = 0x80000000
        # drop the DISPLAY_REQUIRED bit
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


__all__ = [
    "WORKER_COUNT_ENV",
    "allow_sleep",
    "default_worker_count",
    "inhibit_sleep",
    "physical_core_count",
    "usable_cpu_count",
]
