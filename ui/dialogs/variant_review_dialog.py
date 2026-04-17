"""This module owns the variant review dialog implementation; legacy import remains temporary."""

from ..legacy import VariantReviewDialog as _LegacyVariantReviewDialog


class VariantReviewDialog(_LegacyVariantReviewDialog):
    """Compatibility subclass that exposes staged constructor hooks."""

    def __init__(self, parent, variants, weekend_history, assignment_history, backup):
        self._build_layout(parent, variants, weekend_history, assignment_history, backup)
        self._bind_signals()
        self._apply_initial_state()

    def _build_layout(self, parent, variants, weekend_history, assignment_history, backup):
        super().__init__(parent, variants, weekend_history, assignment_history, backup)

    def _bind_signals(self):
        """Signals are managed by the legacy implementation."""

    def _apply_initial_state(self):
        """Initial state is applied by the legacy implementation."""


__all__ = ["VariantReviewDialog"]
