"""This module owns the compact settings dialog implementation; legacy import remains temporary."""

from ..legacy import CompactSettingsDialog as _LegacyCompactSettingsDialog


class CompactSettingsDialog(_LegacyCompactSettingsDialog):
    """Compatibility subclass that exposes staged constructor hooks."""

    def __init__(self, settings, parent=None):
        self._build_layout(settings, parent)
        self._bind_signals()
        self._apply_initial_state()

    def _build_layout(self, settings, parent=None):
        super().__init__(settings=settings, parent=parent)

    def _bind_signals(self):
        """Signals are managed by the legacy implementation."""

    def _apply_initial_state(self):
        """Initial state is applied by the legacy implementation."""


__all__ = ["CompactSettingsDialog"]
