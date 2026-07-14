"""Small platform-specific helpers kept outside the scheduling engine."""

from __future__ import annotations

import subprocess
import sys


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
        ES_CONTINUOUS       = 0x80000000
        ES_DISPLAY_REQUIRED = 0x00000002
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_DISPLAY_REQUIRED
        )
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

__all__ = ["inhibit_sleep", "allow_sleep"]
