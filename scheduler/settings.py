"""Persistent shared scheduler settings."""

from __future__ import annotations

import json
import os

from .domain import DEFAULT_MAX_WEEKEND_VARIANTS

# Range offered by the GUI and CLI for ``max_weekend_variants``. 0 keeps its
# engine meaning of "unlimited".
MAX_WEEKEND_VARIANTS_RANGE = (0, 100_000)


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
        "max_weekend_variants": DEFAULT_MAX_WEEKEND_VARIANTS,
        "scoring_weights": {
            "rotation_rep": 0.30,
            "gaps": 0.20,
            "rot_viol": 0.15,
            "weekend_gap": 0.15,
            "balance": 0.10,
            "long_term": 0.10,
        },
    }

    def __init__(self, filename="settings.json"):
        base = os.path.expanduser("~")
        cfg_dir = os.path.join(base, ".nurse_scheduler")
        self.path = os.path.join(cfg_dir, filename)
        self._settings = self._read()

    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key):
        return self._settings.get(key, self.DEFAULTS[key])

    def update(self, mapping: dict) -> None:
        """Persist ``mapping`` to the settings file, keeping every other key.

        The file is re-read first so settings the GUI saved after this object
        was created are not overwritten with a stale copy. Only the given keys
        are written; defaults are left implicit so a later change to a
        default still reaches this user.
        """
        current = self._read()
        current.update(mapping)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._settings = current


__all__ = ["MAX_WEEKEND_VARIANTS_RANGE", "SharedSettings"]
