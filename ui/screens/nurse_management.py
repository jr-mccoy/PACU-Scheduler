"""This module owns the nurse management screen implementation; legacy import remains temporary."""

from ..legacy import NurseManagementScreen as _LegacyNurseManagementScreen


class NurseManagementScreen(_LegacyNurseManagementScreen):
    """Compatibility subclass that exposes staged constructor hooks."""

    def __init__(self, parent):
        self._build_layout(parent)
        self._bind_signals()
        self._apply_initial_state()

    def _build_layout(self, parent):
        super().__init__(parent)

    def _bind_signals(self):
        """Signals are already bound by the legacy implementation."""

    def _apply_initial_state(self):
        """Initial state is applied by the legacy implementation."""


__all__ = ["NurseManagementScreen"]
