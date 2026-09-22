"""Plain-text report of how many weekday slots each variant left unfilled.

Every evaluated variant records ``early_gaps`` (empty Main/Backup slots
right after the first weekday assignment pass) and ``gaps`` (what remained
after gap-fill and rebalancing). The report lines the two up so a manager
can see whether the repair passes, or the constraints, are the bottleneck.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime


def write_gap_report(candidates: Iterable, path: str | os.PathLike[str]) -> str:
    """Write the report for ``(idx, stats, counts, schedule)`` tuples to *path*.

    Rows are ordered by variant index. Returns the path written.
    """
    rows = []
    for candidate in candidates:
        idx, stats = candidate[0], candidate[1]
        early = stats.get("early_gaps")
        final = stats.get("gaps")
        filled = early - final if isinstance(early, int) and isinstance(final, int) else None
        rows.append((int(idx), early, final, filled))
    rows.sort()

    def cell(value) -> str:
        return "-" if value is None else str(value)

    lines = [
        f"Weekday gap report — generated {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "Initial = empty slots after the first weekday pass; "
        "Final = empty slots in the finished variant.",
        "",
        f"{'Variant':>8}  {'Initial':>8}  {'Final':>8}  {'Filled':>8}",
    ]
    lines += [
        f"{idx + 1:>8}  {cell(early):>8}  {cell(final):>8}  {cell(filled):>8}"
        for idx, early, final, filled in rows
    ]
    if rows:
        finals = [r[2] for r in rows if isinstance(r[2], int)]
        if finals:
            lines += ["", f"Variants: {len(rows)}   Best final: {min(finals)}"]

    path = os.fspath(path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


__all__ = ["write_gap_report"]
