"""Debug logging and assignment diagnostics with stable object identity."""

from __future__ import annotations

import atexit
import csv
import datetime
import json
import os
import pathlib
import threading
from contextlib import suppress
from typing import Any

from . import runtime as _runtime

_open_dbg = _runtime._open_dbg
_dbg_pairs = _runtime._dbg_pairs
_dbg_variants = _runtime._dbg_variants
_reject = _runtime._reject
_accept = _runtime._accept
_pair = _runtime._pair

# Opt-in: when enabled this writes an assignment_debug_*.jsonl/.csv pair into
# the working directory for every run, which is a diagnostic aid rather than
# something an ordinary run should leave behind.
_DEBUG = bool(int(os.getenv("DEBUG_SCHED", "0")))
_LOCK = threading.Lock()
_LOG_FILE_CACHE: dict[str, str] = {}


class AssignmentDebugLogger:
    """Structured assignment logger that writes JSONL and CSV side by side."""

    CSV_FIELDS = [
        "timestamp",
        "context",
        "phase",
        "date",
        "role",
        "final_pick",
        "eligible",
        "candidate_stats",
        "rejections",
        "counts_main",
        "counts_backup",
        "counts_total",
        "history_main",
        "history_backup",
        "note",
        "extra",
    ]

    def __init__(self, enabled: bool, *, directory: pathlib.Path | None = None) -> None:
        self.enabled = bool(enabled)
        self._json_handle: Any | None = None
        self._csv_handle: Any | None = None
        self._csv_writer: csv.DictWriter | None = None
        self.json_path: pathlib.Path | None = None
        self.csv_path: pathlib.Path | None = None

        if not self.enabled:
            return

        base_dir = pathlib.Path(directory) if directory else pathlib.Path.cwd()
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        base_name = f"assignment_debug_{timestamp}"

        self.json_path = base_dir / f"{base_name}.jsonl"
        self.csv_path = base_dir / f"{base_name}.csv"

        self._json_handle = _runtime._open_dbg(str(self.json_path), "a")
        self._csv_handle = _runtime._open_dbg(str(self.csv_path), "a")

        if not self._json_handle or not self._csv_handle:
            # Could not open one or both handles (likely due to Windows file
            # locking when workers fork). Disable structured logging so the
            # scheduler continues instead of hanging forever.
            self.enabled = False
            self.close()
            return

        self._csv_writer = csv.DictWriter(self._csv_handle, fieldnames=self.CSV_FIELDS)
        if self._csv_handle.tell() == 0:
            self._csv_writer.writeheader()

        atexit.register(self.close)

    def __getstate__(self) -> dict:
        """Drop the open file handles so the logger survives pickling.

        ``ScheduleVariant`` holds a reference to this logger, and variants are
        pickled to worker processes during parallel evaluation. File objects
        cannot be pickled, so without this the whole variant fails to send and
        every worker dies with ``cannot pickle '_io.TextIOWrapper' object``.

        Handles are dropped rather than reopened because a worker must not
        write through them anyway: concurrent writes to a shared handle
        interleave, and on Windows the second process cannot open the file at
        all. An unpickled logger therefore arrives disabled, and the parent
        keeps writing the assignment diagnostics it collected itself.
        """
        state = self.__dict__.copy()
        state["enabled"] = False
        state["_json_handle"] = None
        state["_csv_handle"] = None
        state["_csv_writer"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)

    def close(self) -> None:
        """Close both debug files."""
        if self._json_handle:
            try:
                self._json_handle.close()
            finally:
                self._json_handle = None
        if self._csv_handle:
            try:
                self._csv_handle.close()
            finally:
                self._csv_handle = None
                self._csv_writer = None

    @staticmethod
    def _stringify(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, (str, int, float)):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        return json.dumps(value, default=str, sort_keys=True)

    def log(self, payload: dict) -> None:
        """Write a payload to JSONL/CSV if debugging is enabled."""
        if not self.enabled or not payload:
            return

        record = payload.copy()
        record.setdefault("timestamp", datetime.datetime.now().isoformat())

        with _LOCK:
            assert (
                self._json_handle is not None
                and self._csv_writer is not None
                and self._csv_handle is not None
            )
            self._json_handle.write(json.dumps(record, default=str) + "\n")

            row = {field: self._stringify(record.get(field)) for field in self.CSV_FIELDS}
            self._csv_writer.writerow(row)
            self._json_handle.flush()
            self._csv_handle.flush()


def _ts():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def log(kind: str, payload: dict):
    """Write one JSON line to <kind>_dump_<timestamp>.log."""
    if not _DEBUG:
        return
    line = json.dumps(payload, default=str)
    with _LOCK:
        fname = _LOG_FILE_CACHE.setdefault(kind, f"{kind}_dump_{_ts()}.log")
        with open(fname, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


ASSIGNMENT_DEBUG_LOGGER = AssignmentDebugLogger(enabled=_DEBUG)


def configure_pair_variant_debug(mode: str) -> None:
    """Reconfigure pair/variant debug streams without stale aliases."""
    normalized = mode if mode in {"", "pairs", "variants", "all"} else ""
    for attr in ("_DBG_FILE_PAIRS", "_DBG_FILE_VARIANTS"):
        with suppress(Exception):
            handle = getattr(_runtime, attr, None)
            if handle:
                handle.close()
        setattr(_runtime, attr, None)
    _runtime._DBG_MODE = normalized
    if normalized in {"pairs", "all"}:
        _runtime._DBG_FILE_PAIRS = _runtime._open_dbg("debug_pairs.txt")
    if normalized in {"variants", "all"}:
        _runtime._DBG_FILE_VARIANTS = _runtime._open_dbg("debug_variants.txt")


def configure_assignment_debug_logger(enabled: bool) -> None:
    """Reconfigure the shared logger while preserving imported references."""
    global _DEBUG
    _DEBUG = bool(enabled)
    replacement = AssignmentDebugLogger(enabled=_DEBUG)
    ASSIGNMENT_DEBUG_LOGGER.close()
    ASSIGNMENT_DEBUG_LOGGER.__dict__.clear()
    ASSIGNMENT_DEBUG_LOGGER.__dict__.update(replacement.__dict__)


__all__ = [
    "AssignmentDebugLogger",
    "ASSIGNMENT_DEBUG_LOGGER",
    "_open_dbg",
    "_dbg_pairs",
    "_dbg_variants",
    "_reject",
    "_accept",
    "_pair",
    "log",
    "configure_pair_variant_debug",
    "configure_assignment_debug_logger",
]
