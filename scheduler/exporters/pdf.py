"""Landscape-letter PDF rendering for schedule variants.

The functions in this module are deliberately free of any reference to
``NurseScheduler``; they take the schedule dataframe and a
``calendar.Calendar`` instance directly so that PDF export can be unit
tested without constructing a full scheduler.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Mapping

import pandas as pd
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as _pdf_canvas

logger = logging.getLogger(__name__)


DEFAULT_PDF_FONT_SIZES: Mapping[str, int] = {
    "title": 32,
    "dow": 16,
    "dayno": 14,
    "name": 18,
}


def _is_blank(value) -> bool:
    """Mirror ``scheduler.legacy_core.is_empty`` semantics for cell rendering."""
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def draw_weekday_header(cvs, hdr_y_top, row_h, col_w, font_sizes=DEFAULT_PDF_FONT_SIZES):
    """Draw the Sun..Sat weekday header row."""
    cvs.setFont("Helvetica-Bold", font_sizes["dow"])
    for col, dow in enumerate(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]):
        x0 = 0.5 * cm + col * col_w
        cvs.rect(x0, hdr_y_top - row_h, col_w, row_h)
        cvs.drawCentredString(
            x0 + col_w / 2,
            hdr_y_top - row_h / 2 + font_sizes["dow"] / 3,
            dow,
        )


def draw_week_rows(
    cvs,
    weeks,
    year,
    month,
    sched_df,
    y_top,
    row_h,
    col_w,
    font_sizes=DEFAULT_PDF_FONT_SIZES,
):
    """Draw each week row with day numbers and main/backup names."""
    for week in weeks:
        for col, day in enumerate(week):
            x0 = 0.5 * cm + col * col_w
            cvs.rect(x0, y_top - row_h, col_w, row_h)

            if day:
                dt = pd.Timestamp(year=year, month=month, day=day)
                main = ""
                backup = ""
                if dt in sched_df.index:
                    main_raw = sched_df.at[dt, "main"]
                    backup_raw = sched_df.at[dt, "backup"]
                    main = "" if _is_blank(main_raw) else str(main_raw)
                    backup = "" if _is_blank(backup_raw) else str(backup_raw)

                cvs.setFont("Helvetica-Bold", font_sizes["dayno"])
                cvs.drawString(x0 + 2, y_top - font_sizes["dayno"] - 2, str(day))

                name_x = x0 + col_w / 2
                line_gap = font_sizes["name"] + 2
                first_line = y_top - row_h / 2 + line_gap / 2

                cvs.setFont("Helvetica", font_sizes["name"])
                cvs.drawCentredString(name_x, first_line, main)
                cvs.drawCentredString(name_x, first_line - line_gap, backup)
        y_top -= row_h


def export_variant_pdf(
    pdf_path: str,
    sched_df: pd.DataFrame,
    cal: calendar.Calendar,
    font_sizes: Mapping[str, int] = DEFAULT_PDF_FONT_SIZES,
) -> None:
    """Create a landscape-letter PDF containing every month in ``sched_df``."""
    page_w, page_h = landscape(letter)
    margin = 0.5 * cm
    title_h = 1.5 * cm
    col_w = (page_w - 2 * margin) / 7

    def draw_month(cvs, year: int, month: int) -> None:
        weeks = cal.monthdayscalendar(year, month)
        total_rows = len(weeks) + 1
        table_h = page_h - 2 * margin - title_h
        row_h = table_h / total_rows

        cvs.setFont("Helvetica-Bold", font_sizes["title"])
        cvs.drawCentredString(
            page_w / 2,
            page_h - margin - 0.6 * cm,
            f"{calendar.month_name[month]} {year}",
        )

        draw_weekday_header(cvs, page_h - margin - title_h, row_h, col_w, font_sizes)
        draw_week_rows(
            cvs,
            weeks,
            year,
            month,
            sched_df,
            page_h - margin - title_h - row_h,
            row_h,
            col_w,
            font_sizes,
        )

    c = _pdf_canvas.Canvas(pdf_path, pagesize=landscape(letter))
    start, end = sched_df.index.min(), sched_df.index.max()
    year, month = start.year, start.month

    while (year, month) <= (end.year, end.month):
        draw_month(c, year, month)
        c.showPage()
        month = 1 if month == 12 else month + 1
        year = year + 1 if month == 1 else year

    c.save()
    logger.info("Wrote PDF %s", pdf_path)
