"""Top-level exports for GUI dialogs.

The subclass dialog modules import from ``ui.legacy``, which in turn
imports ``ToolDialog`` from this package during its own module load.
To avoid a circular import we expose the subclass dialogs via
``__getattr__`` so they are only resolved on first attribute access,
after ``ui.legacy`` has finished loading.
"""

from .tool_dialog import ToolDialog

__all__ = [
    "ToolDialog",
    "SettingsDialog",
    "CompactSettingsDialog",
    "VariantReviewDialog",
]


def __getattr__(name: str):
    if name == "CompactSettingsDialog":
        from .compact_settings_dialog import CompactSettingsDialog

        return CompactSettingsDialog
    if name == "SettingsDialog":
        from .settings_dialog import SettingsDialog

        return SettingsDialog
    if name == "VariantReviewDialog":
        from .variant_review_dialog import VariantReviewDialog

        return VariantReviewDialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
