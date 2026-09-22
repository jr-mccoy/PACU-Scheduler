"""Platform detection, backend-debug preferences, and dialog sizing."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLayout

from scheduler import (
    configure_assignment_debug_logger,
    configure_pair_variant_debug,
)

logger = logging.getLogger(__name__)


def is_android_platform() -> bool:
    """Return True when running inside any Android/Python-for-Android build."""
    platform_plugin = os.environ.get("QT_QPA_PLATFORM", "").lower()
    return (
        sys.platform == "android"
        or "ANDROID_ROOT" in os.environ
        or "ANDROID_DATA" in os.environ
        or "ANDROID_STORAGE" in os.environ
        or "ANDROID_ARGUMENT" in os.environ
        or platform_plugin == "android"
        or hasattr(sys, "getandroidapilevel")  # p4a convenience
    )


def _coerce_env_flag(value: str | None) -> bool | None:
    """Map common truthy/falsey strings to bool; return None when unknown."""
    if value is None:
        return None
    val = value.strip().lower()
    if val in {"1", "true", "yes", "on", "t", "threads", "thread"}:
        return True
    if val in {"0", "false", "no", "off", "f", "process", "processes", "proc"}:
        return False
    return None


def _get_setting(settings: Any, key: str, default: Any) -> Any:
    """Safely fetch ``key`` from ``settings`` whether it is a dict or AppSettings."""
    if settings is None:
        return default
    try:
        if hasattr(settings, "get"):
            return settings.get(key)
        if isinstance(settings, dict):
            return settings.get(key, default)
    except Exception:
        return default
    return default


def _normalize_debug_mode(value: Any) -> str:
    """Return a supported NSCHED_DEBUG mode (off/pairs/variants/all)."""
    if not isinstance(value, str):
        return "off"
    mode = value.strip().lower()
    return mode if mode in {"off", "pairs", "variants", "all"} else "off"


def _normalize_bool(value: Any, *, default: bool = False) -> bool:
    """Coerce assorted truthy/falsey representations to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        parsed = _coerce_env_flag(value)
        if parsed is not None:
            return parsed
    return default


def apply_backend_debug_preferences(settings: Any) -> None:
    """
    Configure scheduler backend debug helpers based on persisted settings.

    * ``debug_variant_logging`` controls NSCHED_DEBUG (pairs/variants/all/off)
    * ``assignment_debug_enabled`` toggles the structured AssignmentDebugLogger

    Set ``NSCHED_FORCE_THREAD_POOL=1`` to force ThreadPoolExecutor, 0 to force
    process pools regardless of platform detection (useful for debugging).
    """

    try:
        debug_mode = _normalize_debug_mode(_get_setting(settings, "debug_variant_logging", "off"))
        assignment_enabled = _normalize_bool(
            _get_setting(settings, "assignment_debug_enabled", True),
            default=True,
        )
    except Exception as exc:  # pragma: no cover - defensive guard for GUI use
        logger.warning("Could not read debug settings: %s", exc)
        return

    backend_mode = "" if debug_mode == "off" else debug_mode
    if backend_mode:
        os.environ["NSCHED_DEBUG"] = backend_mode
    else:
        os.environ.pop("NSCHED_DEBUG", None)

    try:
        configure_pair_variant_debug(backend_mode)
    except Exception as exc:  # pragma: no cover - errors shouldn't stop GUI
        logger.warning("Could not configure NSCHED_DEBUG: %s", exc)

    os.environ["DEBUG_SCHED"] = "1" if assignment_enabled else "0"
    try:
        configure_assignment_debug_logger(assignment_enabled)
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not configure the assignment debug logger: %s", exc)


def tune_dialog(root_layout: QLayout, spacing: int = 12) -> None:
    """
    Recursively apply `spacing` to every QVBox/HBox/Form layout in `root_layout`,
    *except* the private layout Qt creates inside each QDialogButtonBox.
    Works on PySide6 (findChildren must take ONE type).
    """
    for lay in root_layout.findChildren(QLayout, options=Qt.FindChildrenRecursively):
        # skip the invisible layout that lives inside a button-box
        if isinstance(lay.parentWidget(), QDialogButtonBox):
            continue
        if isinstance(lay, (type(root_layout),)):  # quick self-check
            lay.setSpacing(spacing)
        elif lay.__class__.__name__ in ("QVBoxLayout", "QHBoxLayout", "QFormLayout"):
            lay.setSpacing(spacing)


def _runtime_base_dir() -> str:
    """Directory the script is running from (fallback to CWD)."""
    try:
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
        if base and os.path.isdir(base):
            return base
    except Exception:
        pass
    return os.getcwd()


def _open_external(path: str) -> bool:
    """
    Try to open a file in the platform default app.
    On Android (Pydroid), fall back to `am start` if QDesktopServices fails.
    Returns True if we *think* the viewer launched.
    """
    try:
        url = QUrl.fromLocalFile(path)
        ok = QDesktopServices.openUrl(url)
        if ok:
            return True
    except Exception:
        pass

    # Fallback for Android: try an intent
    try:
        if is_android_platform():
            import mimetypes
            import subprocess

            mt, _ = mimetypes.guess_type(path)
            if not mt:
                # crude guess by extension
                mt = "text/html" if path.lower().endswith(".html") else "application/pdf"
            cmd = f'am start -a android.intent.action.VIEW -d "file://{path}" -t "{mt}"'
            subprocess.run(cmd, shell=True, check=False)
            return True
    except Exception:
        return False
    return False


def adjust_dialog_for_android(dialog):
    """
    Helper function to adjust any dialog's size for Android screens.
    Call this after creating a dialog but before showing it.
    """
    if sys.platform == "android" or "ANDROID_ROOT" in os.environ:
        screen = QApplication.primaryScreen()
        if screen:
            screen_size = screen.size()

            # Calculate appropriate size for high-DPI display
            max_width = int(screen_size.width() * 0.92)
            max_height = int(screen_size.height() * 0.85)

            # Get current size hint or current size
            current_width = (
                dialog.sizeHint().width() if dialog.sizeHint().isValid() else dialog.width()
            )
            current_height = (
                dialog.sizeHint().height() if dialog.sizeHint().isValid() else dialog.height()
            )

            # Constrain to screen limits
            new_width = min(current_width, max_width)
            new_height = min(current_height, max_height)

            dialog.resize(new_width, new_height)


__all__ = [
    "is_android_platform",
    "apply_backend_debug_preferences",
    "tune_dialog",
    "adjust_dialog_for_android",
]
