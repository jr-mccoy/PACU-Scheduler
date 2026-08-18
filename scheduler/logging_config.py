"""Logging setup for the application entry points.

The ``scheduler`` package logs through ``logging.getLogger(__name__)`` and
never configures the root logger itself — that is an application decision,
not a library one. The entry points (``main.py`` for the GUI, ``python -m
cli`` for the terminal UI) call :func:`configure_logging` once at startup.

Verbosity comes from the ``PACU_LOG_LEVEL`` environment variable so it can
be raised without touching either entry point:

.. code-block:: console

   PACU_LOG_LEVEL=DEBUG python main.py
"""

from __future__ import annotations

import logging
import os

ENV_VAR = "PACU_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def resolve_level(raw: str | None = None) -> int:
    """Translate a level name into a ``logging`` constant.

    Falls back to :data:`DEFAULT_LEVEL` for an unset or unrecognized name so
    a typo in the environment never crashes startup.
    """
    name = (raw if raw is not None else os.environ.get(ENV_VAR, "")).strip().upper()
    if not name:
        name = DEFAULT_LEVEL
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.getLevelName(DEFAULT_LEVEL)


def configure_logging(level: str | None = None) -> int:
    """Attach a stderr handler to the root logger and return the level used.

    Safe to call more than once: ``force=True`` replaces any handler a
    previous call installed rather than stacking duplicates.
    """
    resolved = resolve_level(level)
    logging.basicConfig(
        level=resolved,
        format=_LOG_FORMAT,
        datefmt=_DATE_FORMAT,
        force=True,
    )
    return resolved
