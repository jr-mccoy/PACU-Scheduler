"""Render the GUI screens to PNG files for the README.

Runs the real Qt application against the same fictional roster
``scripts/demo.py`` seeds, using the ``offscreen`` platform plugin so no
display is needed, and grabs each screen in the stack. Regenerate with::

    python scripts/screenshots.py

Images land in ``docs/images/``. Nothing here reads a real database — the
roster is the invented one from the demo seed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

# Must be set before anything imports QtGui.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / "docs" / "images"
WINDOW_SIZE = (1400, 900)

# (stack page name, output file stem)
SCREENS: list[tuple[str, str]] = [
    ("main", "main-menu"),
    ("manage", "nurse-management"),
    ("generate", "schedule-generation"),
    ("weekend_history", "weekend-history"),
    ("advanced_stats", "rotation-stats"),
]


def _seed_database(directory: Path) -> None:
    """Write a seeded database where the GUI expects to find one."""
    from scripts.demo import _next_friday, seed
    from ui.config import DB_NAME

    start = _next_friday(date.today()) + timedelta(days=3)
    seed(str(directory / DB_NAME), start)


def _show_latest_recorded_month(screen) -> None:
    """The demo's weekend history sits weeks before today; show that month."""
    recorded = screen.wh.get_assignments()
    if recorded:
        latest = recorded[-1][0]
        screen.month, screen.year = latest.month, latest.year
        screen._refresh_all()


def capture() -> list[Path]:
    workdir = Path(tempfile.mkdtemp(prefix="pacu-screenshots-"))
    original_cwd = Path.cwd()
    written: list[Path] = []

    try:
        _seed_database(workdir)
        # The GUI opens DB_NAME relative to the working directory.
        os.chdir(workdir)

        from PySide6.QtWidgets import QApplication

        from ui import App, UiStyle

        app = QApplication.instance() or QApplication([])
        UiStyle.apply(app)

        window = App()
        window.resize(*WINDOW_SIZE)
        window.show()

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        for page, stem in SCREENS:
            window.switch_frame(page)
            if page == "weekend_history":
                _show_latest_recorded_month(window.weekend_history)
            app.processEvents()
            target = OUTPUT_DIR / f"{stem}.png"
            if not window.grab().save(str(target)):
                raise RuntimeError(f"Qt could not write {target}")
            written.append(target)
            print(f"wrote {target.relative_to(ROOT)}")
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(workdir, ignore_errors=True)

    return written


if __name__ == "__main__":
    capture()
