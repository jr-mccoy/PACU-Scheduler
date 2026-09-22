"""Shared pytest configuration.

Puts the repository root on ``sys.path`` so the test modules can import the
``scheduler``, ``cli``, and ``ui`` packages without the project being
installed. Keeping this in one place lets each test module start with its
imports instead of repeating the same path boilerplate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def qapp():
    """A QApplication for widget tests (skipped when PySide6 is unavailable)."""
    widgets = pytest.importorskip("PySide6.QtWidgets")
    return widgets.QApplication.instance() or widgets.QApplication([])
