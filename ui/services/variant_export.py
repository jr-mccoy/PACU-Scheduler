"""Export-only service helpers for variant schedules."""

from __future__ import annotations

import calendar
import json
import mimetypes
import os
import subprocess
import sys
from datetime import datetime

from PySide6.QtCore import QStandardPaths, QUrl
from PySide6.QtGui import QDesktopServices

from ..presenters.variant_review_presenter import _build_html_for_top_variants


def _is_android_platform() -> bool:
    platform_plugin = os.environ.get("QT_QPA_PLATFORM", "").lower()
    return (
        sys.platform == "android"
        or "ANDROID_ROOT" in os.environ
        or "ANDROID_DATA" in os.environ
        or "ANDROID_STORAGE" in os.environ
        or "ANDROID_ARGUMENT" in os.environ
        or platform_plugin == "android"
        or hasattr(sys, "getandroidapilevel")
    )


def _runtime_base_dir() -> str:
    try:
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
        if base and os.path.isdir(base):
            return base
    except Exception:
        pass
    return os.getcwd()


def _open_external(path: str) -> bool:
    """Open file in default app/browser; fallback to Android intent when needed."""
    try:
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        if ok:
            return True
    except Exception:
        pass

    try:
        if _is_android_platform():
            mt, _ = mimetypes.guess_type(path)
            if not mt:
                mt = "text/html" if path.lower().endswith(".html") else "application/pdf"
            cmd = f'am start -a android.intent.action.VIEW -d "file://{path}" -t "{mt}"'
            subprocess.run(cmd, shell=True, check=False)
            return True
    except Exception:
        return False
    return False


def _write_variant_debug_dump(out_dir, payload, *, owner=None):
    path = os.path.join(out_dir, "variant_debug.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    if owner and hasattr(owner, "_debug_variant_dump"):
        try:
            delattr(owner, "_debug_variant_dump")
        except Exception:
            pass
    return path


def _save_outputs_for_variants(variants, scheduler, *, top_n=5, debug_save_variants=False) -> str:
    base_dir = _runtime_base_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(base_dir, f"Schedules_{ts}")
    os.makedirs(out_dir, exist_ok=True)

    try:
        html = _build_html_for_top_variants(variants, max_variants=top_n)
        with open(os.path.join(out_dir, "variants_calendar.html"), "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as exc:
        print(f"[export] HTML calendar failed: {exc}")

    try:
        cal = calendar.Calendar(firstweekday=6)
        for rank, var in enumerate(variants[:top_n], 1):
            df = var[3] if len(var) >= 4 else var[2]
            pdf_path = os.path.join(out_dir, f"variant_{rank}.pdf")
            try:
                scheduler._export_variant_pdf(pdf_path, df.copy(deep=True), cal)
            except Exception as ex:
                print(f"[export] PDF for variant {rank} failed: {ex}")
    except Exception as exc:
        print(f"[export] PDF batch failed: {exc}")

    if debug_save_variants:
        snapshot = getattr(scheduler, "_debug_variant_dump", None)
        if snapshot is not None:
            try:
                dest = _write_variant_debug_dump(out_dir, snapshot, owner=scheduler)
                print(f"[export] wrote variant debug snapshot to {dest}")
            except Exception as exc:
                print(f"[export] failed to write variant debug snapshot: {exc}")
                try:
                    if hasattr(scheduler, "_debug_variant_dump"):
                        delattr(scheduler, "_debug_variant_dump")
                except Exception:
                    pass

    return out_dir


def export_variants_calendar_html(variants, max_variants=5, filename_prefix="schedule_variants"):
    html = _build_html_for_top_variants(variants, max_variants=max_variants)

    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or os.getcwd()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(docs, f"{filename_prefix}_{ts}.html")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    _open_external(out_path)
    return out_path


def export_top_variants_pdfs(variants, scheduler, out_prefix="schedule_variant"):
    cal = calendar.Calendar(firstweekday=6)
    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or os.getcwd()

    paths = []
    for rank, var in enumerate(variants, 1):
        df = var[3] if len(var) >= 4 else var[2]
        pdf_path = os.path.join(docs, f"{out_prefix}_{rank}.pdf")
        try:
            scheduler._export_variant_pdf(pdf_path, df, cal)
            paths.append(pdf_path)
        except Exception as exc:
            print(f"[pdf export] failed for variant {rank}: {exc}")

    if paths:
        _open_external(paths[0])
    return paths


__all__ = [
    "_build_html_for_top_variants",
    "_save_outputs_for_variants",
    "export_variants_calendar_html",
    "export_top_variants_pdfs",
]
