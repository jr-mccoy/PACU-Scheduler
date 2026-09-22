"""Persistent GUI settings with atomic writes."""

from __future__ import annotations

import json
import os

from scheduler import DEFAULT_MAX_WEEKEND_VARIANTS


class AppSettings:
    DEFAULTS = {
        # UI
        "theme": "dark",
        "font_size": 12,
        "accent_color": "#5C8DBC",
        "show_gif": True,
        "calendar_grid": True,
        # Core scheduling
        "weekend_gap_days": 28,
        "min_days_between_assignments": 2,
        "main_score_factor": 10,
        "backup_score_factor": 10,
        "availability_penalty": 10,
        "history_window_days": 30,
        "history_duration_months": 6,
        # Analysis / Debug.  The two reports below are written into each run's
        # export folder; they are opt-in because they add work to every run.
        "measure_phase_times": False,
        "analyse_initial_weekday_gaps": False,
        "gap_report_file": "weekday_gap_report.txt",
        "debug_variant_logging": "off",
        "assignment_debug_enabled": True,
        # Post-weekend weekday relaxations
        "allow_post_weekend_wednesday_main": False,
        "allow_post_weekend_wednesday_backup": True,
        "allow_post_weekend_thursday_main": True,
        "allow_post_weekend_thursday_backup": True,
        # One-day weekday gap fallback (Mon–Wed / Tue–Thu, never next to the
        # nurse's own weekend).  The master toggle allows any roles; the two
        # narrower toggles allow only Backup+Backup or Main+Backup pairs.
        "allow_one_day_weekday_gap": False,
        "allow_midweek_pair_backup_only": False,
        "allow_midweek_pair_mixed": False,
        # Beam cap on weekend variant branching (0 = unlimited)
        "max_weekend_variants": DEFAULT_MAX_WEEKEND_VARIANTS,
        # Canonical scorer weights
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
        self.path = self._get_config_path(filename)
        self._settings: dict = {}
        self.load()

    def _get_config_path(self, filename):
        base = os.path.expanduser("~")
        cfg_dir = os.path.join(base, ".nurse_scheduler")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, filename)

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self._settings = json.load(f)
        except Exception:
            self._settings = {}

    def _atomic_save(self, data: dict):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def save(self):
        self._atomic_save(self.all())

    def get(self, key):
        return self._settings.get(key, self.DEFAULTS[key])

    def set(self, key, value):
        # Kept for compatibility; writes immediately
        self._settings[key] = value
        self.save()

    def bulk_set(self, mapping: dict):
        """Set many keys and persist ONCE (prevents partial writes)."""
        self._settings.update(mapping or {})
        self.save()

    def all(self) -> dict:
        d = self.DEFAULTS.copy()
        d.update(self._settings)
        return d


__all__ = ["AppSettings"]
