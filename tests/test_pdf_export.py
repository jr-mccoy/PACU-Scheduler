"""Tests for the extracted PDF export module."""

from __future__ import annotations

import calendar
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler.exporters.pdf import (
    DEFAULT_PDF_FONT_SIZES,
    draw_week_rows,
    export_variant_pdf,
)


class _RecordingCanvas:
    """Stand-in for ``reportlab.pdfgen.canvas.Canvas`` for unit tests."""

    def __init__(self) -> None:
        self.centered_text: list[str] = []
        self.draw_strings: list[str] = []

    def rect(self, *_args, **_kwargs) -> None:
        return None

    def setFont(self, *_args, **_kwargs) -> None:
        return None

    def drawString(self, _x, _y, text) -> None:
        self.draw_strings.append(str(text))

    def drawCentredString(self, _x, _y, text) -> None:
        self.centered_text.append(text)


def _fixture_schedule_df() -> pd.DataFrame:
    dt_blank = pd.Timestamp("2026-01-03")
    dt_filled = pd.Timestamp("2026-01-04")
    return pd.DataFrame(
        index=[dt_blank, dt_filled],
        data={"main": [None, "Alice"], "backup": [None, "Bob"]},
    )


def test_draw_week_rows_blank_cells_render_empty_string():
    canvas = _RecordingCanvas()
    draw_week_rows(
        canvas,
        weeks=[[0, 0, 0, 0, 0, 0, 3], [4, 0, 0, 0, 0, 0, 0]],
        year=2026,
        month=1,
        sched_df=_fixture_schedule_df(),
        y_top=100,
        row_h=20,
        col_w=20,
    )

    assert "None" not in canvas.centered_text
    assert "" in canvas.centered_text
    assert "Alice" in canvas.centered_text
    assert "Bob" in canvas.centered_text


def test_export_variant_pdf_produces_valid_multi_page_pdf(tmp_path: Path):
    schedule = pd.DataFrame(
        index=pd.date_range("2026-01-02", "2026-02-05", freq="D"),
        columns=["main", "backup"],
        data="",
    )
    schedule.loc[pd.Timestamp("2026-01-09"), "main"] = "Alice"
    schedule.loc[pd.Timestamp("2026-01-09"), "backup"] = "Bob"

    pdf_path = tmp_path / "variant.pdf"
    cal = calendar.Calendar(firstweekday=6)
    export_variant_pdf(str(pdf_path), schedule, cal)

    assert pdf_path.exists()
    payload = pdf_path.read_bytes()
    assert payload.startswith(b"%PDF-")
    assert payload.rstrip().endswith(b"%%EOF")
    pages = len(re.findall(rb"/Type\s*/Page\b", payload))
    assert pages == 2, f"expected 2 pages (Jan + Feb), got {pages}"


def test_legacy_method_delegates_to_module_function():
    """The thin ``NurseScheduler._draw_week_rows`` shim must use the new path."""
    from NCSSQL57 import NurseScheduler  # noqa: WPS433 - intentional shim use

    src = NurseScheduler._draw_week_rows.__code__
    assert "_draw_week_rows_fn" in src.co_names


def test_default_font_sizes_match_legacy_constants():
    from NCSSQL57 import NurseScheduler

    assert dict(DEFAULT_PDF_FONT_SIZES) == NurseScheduler.PDF_FONT_SIZES
