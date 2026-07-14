"""Shared runtime flags, empty-cell semantics, and pair-debug streams."""

from __future__ import annotations

import atexit
import os

import numpy as np
import pandas as pd

MEASURE_PHASE_TIMES = True
ANALYSE_INITIAL_WEEKDAY_GAPS = True
GAP_REPORT_FILE = "weekday_gap_report.txt"
PERFORMANCE_PROFILING_REQUESTED = False
PERFORMANCE_PROFILE_JSON_DEFAULT = "performance_metrics.json"


def _env_flag(name: str, default: bool = False) -> bool:
    """Return True if the environment variable ``name`` evaluates to truthy."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _open_dbg(path: str, mode: str = "a"):
    """Safely open *path* for debug logging, tolerating OS lock failures."""

    try:
        return open(path, mode, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"[debug] unable to open {path!r}: {exc}")
        return None


def _dbg_pairs(msg: str = "") -> None:
    if _DBG_FILE_PAIRS:
        _DBG_FILE_PAIRS.write(msg + "\n")


def _dbg_variants(msg: str = "") -> None:
    if _DBG_FILE_VARIANTS:
        _DBG_FILE_VARIANTS.write(msg + "\n")


def _reject(nurse: str, reason: str) -> None:
    _dbg_pairs(f"    ✗ {nurse:15}  [{reason}]")


def _accept(nurse: str, tag: str) -> None:
    _dbg_pairs(f"    ✓ {nurse:15}  [{tag}]")


def _pair(fsf: str, sfs: str) -> None:
    _dbg_pairs(f"    → PAIR: {fsf:10} + {sfs}")


def _count_weekday_gaps(schedule_df) -> int:
    """
    Return number of empty cells (main + backup) Mon–Thu only,
    treating None/NaN/blank as empty using is_empty().
    """
    return _count_main_backup_empties(schedule_df, weekdays_only=True)


def is_empty(value) -> bool:
    """
    Check if a given value is considered empty or unassigned.
    Returns True if the value is None, NaN, empty string, or whitespace-only string.
    """
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    na_value = pd.isna(value)
    if isinstance(na_value, (bool, np.bool_)):
        return bool(na_value)
    return False


def _main_backup_empty_mask(
    schedule_df: pd.DataFrame, *, weekdays_only: bool = False
) -> pd.DataFrame:
    """Return a boolean empty-mask for ``main``/``backup`` cells."""
    if weekdays_only:
        day_mask = ~schedule_df["is_weekend"]  # Mon–Thu
        sub = schedule_df.loc[day_mask, ["main", "backup"]]
    else:
        sub = schedule_df[["main", "backup"]]
    mapper = getattr(sub, "map", None)  # pandas >= 2.1.0
    return mapper(is_empty) if callable(mapper) else sub.applymap(is_empty)


def _count_main_backup_empties(
    schedule_df: pd.DataFrame, *, weekdays_only: bool = False
) -> int:
    """Count empty ``main``/``backup`` cells using ``is_empty`` semantics."""
    return int(_main_backup_empty_mask(schedule_df, weekdays_only=weekdays_only).to_numpy().sum())

PERFORMANCE_PROFILING_REQUESTED = _env_flag("NSCHED_PROFILE", False)
PERFORMANCE_PROFILE_JSON_DEFAULT = os.environ.get(
    "NSCHED_PROFILE_JSON", "performance_metrics.json"
)

_DBG_MODE = os.environ.get("NSCHED_DEBUG", "").lower()
_DBG_FILE_PAIRS = (
    _open_dbg("debug_pairs.txt") if _DBG_MODE in {"pairs", "all"} else None
)
_DBG_FILE_VARIANTS = (
    _open_dbg("debug_variants.txt")
    if _DBG_MODE in {"variants", "all"}
    else None
)
for _fh in (_DBG_FILE_PAIRS, _DBG_FILE_VARIANTS):
    if _fh:
        atexit.register(_fh.close)
