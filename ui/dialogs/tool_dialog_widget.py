"""Concrete base tool dialog module."""

from ..legacy import ToolDialog as _LegacyToolDialog


class ToolDialog(_LegacyToolDialog):
    """Compatibility subclass that exposes staged constructor hooks."""

    def __init__(self, parent=None, title=None):
        self._build_layout(parent, title)
        self._bind_signals()
        self._apply_initial_state()

    def _build_layout(self, parent=None, title=None):
        super().__init__(parent=parent, title=title)

    def _bind_signals(self):
        """Signals are managed by the legacy implementation."""

    def _apply_initial_state(self):
        """Initial state is applied by the legacy implementation."""


__all__ = ["ToolDialog"]
