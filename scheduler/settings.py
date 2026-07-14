"""Persistent shared scheduler settings."""

from __future__ import annotations

import json
import os


class SharedSettings:
    """
    Read the GUI settings JSON (~/.nurse_scheduler/settings.json) so the CLI
    uses the same configuration. Falls back to known defaults if missing.
    """
    DEFAULTS = {
        "weekend_gap_days": 28,
        "min_days_between_assignments": 2,
        "main_score_factor": 10,
        "backup_score_factor": 10,
        "availability_penalty": 10,
        "history_window_days": 30,
        "allow_post_weekend_wednesday_main": False,
        "allow_post_weekend_wednesday_backup": True,
        "allow_post_weekend_thursday_main": True,
        "allow_post_weekend_thursday_backup": True,
        "allow_one_day_weekday_gap": False,
        "scoring_weights": {
            "rotation_rep": 0.30,
            "gaps":         0.20,
            "rot_viol":     0.15,
            "weekend_gap":  0.15,
            "balance":      0.10,
            "long_term":    0.10,
        },
    }

    def __init__(self, filename="settings.json"):
        base = os.path.expanduser("~")
        cfg_dir = os.path.join(base, ".nurse_scheduler")
        self.path = os.path.join(cfg_dir, filename)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._settings = json.load(f)
        except Exception:
            self._settings = {}

    def get(self, key):
        return self._settings.get(key, self.DEFAULTS[key])

__all__ = ["SharedSettings"]
