"""Debug logging and helper functions for schedule assignment diagnostics."""

from contextlib import suppress

from . import legacy_core as _legacy_core
from .legacy_core import (
    _open_dbg,
    _dbg_pairs,
    _dbg_variants,
    _reject,
    _accept,
    _pair,
)


def configure_pair_variant_debug(mode: str) -> None:
    """Reconfigure the pair/variant debug file handles on the backend.

    ``mode`` is one of ``""``, ``"pairs"``, ``"variants"`` or ``"all"``.
    Any previously opened handles are closed before the new ones are created.
    """

    with suppress(Exception):
        fh = getattr(_legacy_core, "_DBG_FILE_PAIRS", None)
        if fh:
            fh.close()
    with suppress(Exception):
        fh = getattr(_legacy_core, "_DBG_FILE_VARIANTS", None)
        if fh:
            fh.close()

    _legacy_core._DBG_MODE = mode
    _legacy_core._DBG_FILE_PAIRS = (
        _legacy_core._open_dbg("debug_pairs.txt")
        if mode in {"pairs", "all"}
        else None
    )
    _legacy_core._DBG_FILE_VARIANTS = (
        _legacy_core._open_dbg("debug_variants.txt")
        if mode in {"variants", "all"}
        else None
    )


def configure_assignment_debug_logger(enabled: bool) -> None:
    """Swap in a fresh ``AssignmentDebugLogger`` with ``enabled`` toggled."""

    _legacy_core._DEBUG = enabled
    logger = getattr(_legacy_core, "ASSIGNMENT_DEBUG_LOGGER", None)
    if logger:
        with suppress(Exception):
            logger.close()
    _legacy_core.ASSIGNMENT_DEBUG_LOGGER = _legacy_core.AssignmentDebugLogger(
        enabled=enabled
    )


__all__ = [
    "_open_dbg",
    "_dbg_pairs",
    "_dbg_variants",
    "_reject",
    "_accept",
    "_pair",
    "configure_pair_variant_debug",
    "configure_assignment_debug_logger",
]
