"""Theme and calendar/header helpers."""

from __future__ import annotations

import warnings


def shade_color(hex_rgb: str, k: float) -> str:
    """
    Darken (k<1) or lighten (k>1) a ``#RRGGBB`` colour by factor ``k``.

    Returns normalized uppercase ``#RRGGBB`` output when input is valid;
    returns the input unchanged when validation fails.
    """
    if not isinstance(hex_rgb, str):
        return hex_rgb
    if not hex_rgb.startswith("#") or len(hex_rgb) != 7:
        return hex_rgb
    try:
        r = int(hex_rgb[1:3], 16)
        g = int(hex_rgb[3:5], 16)
        b = int(hex_rgb[5:7], 16)
    except ValueError:
        return hex_rgb
    r = max(0, min(255, int(r * k)))
    g = max(0, min(255, int(g * k)))
    b = max(0, min(255, int(b * k)))
    return f"#{r:02X}{g:02X}{b:02X}"


def _shade(hex_rgb: str, k: float) -> str:
    """Deprecated wrapper; use :func:`shade_color`."""
    warnings.warn(
        "ui.theme._shade() is deprecated; use ui.theme.shade_color() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return shade_color(hex_rgb, k)


def __getattr__(name: str):
    """
    Lazy-bridge to legacy theme exports to avoid import cycles during migration.
    """
    if name in {
        "CAL_BORDER",
        "UiStyle",
        "_apply_header",
        "_apply_pink_header",
        "apply_theme_to_calendar",
        "themed_file",
        "themed_icon",
    }:
        from . import legacy as _legacy

        return getattr(_legacy, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "shade_color",
    "_shade",
    "CAL_BORDER",
    "themed_file",
    "themed_icon",
    "_apply_header",
    "_apply_pink_header",
    "apply_theme_to_calendar",
    "UiStyle",
]
