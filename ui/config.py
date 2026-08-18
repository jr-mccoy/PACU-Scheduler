"""Application-level UI constants."""

import os

DB_NAME = "nurse_schedule.db"
CAL_BORDER = "#E9A9B8"
DEBUG_SAVE_VARIANTS = bool(int(os.environ.get("NSCHED_DEBUG_VARIANTS", "0")))

__all__ = ["DB_NAME", "CAL_BORDER", "DEBUG_SAVE_VARIANTS"]
