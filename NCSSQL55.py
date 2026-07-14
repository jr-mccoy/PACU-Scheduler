"""Deprecated compatibility facade for legacy ``NCSSQL55`` imports."""

from warnings import warn as _warn

from scheduler import *  # noqa: F403 - compatibility surface is intentional
from scheduler import __all__ as __all__

_warn(
    "NCSSQL55 is deprecated and will be removed in a future release. "
    "Import from scheduler instead.",
    DeprecationWarning,
    stacklevel=2,
)


def __getattr__(name: str):
    """Preserve lazy access to CLI names moved out of the backend package."""
    import scheduler

    return getattr(scheduler, name)
