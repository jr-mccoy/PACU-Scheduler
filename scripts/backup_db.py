"""Copy the scheduling database to a dated backup file, keeping the newest few.

Uses SQLite's online backup, so the copy is consistent even while the web
server or desktop app is writing. Run nightly by the timer that
``deploy/install-service.sh`` installs; see ``docs/web-server.md``.

    python scripts/backup_db.py --db nurse_schedule.db --dest ~/pacu-backups
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path


def backup(db: Path, dest: Path, keep: int, today: date | None = None) -> Path:
    """Write ``dest/<name>-YYYY-MM-DD.db`` and delete all but the newest ``keep``."""
    if not db.is_file():
        raise FileNotFoundError(f"No database at {db}")
    dest.mkdir(parents=True, exist_ok=True)
    stamp = (today or date.today()).isoformat()
    target = dest / f"{db.stem}-{stamp}.db"
    partial = target.with_suffix(".db.partial")
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    partial.replace(target)

    backups = sorted(dest.glob(f"{db.stem}-????-??-??.db"))
    for old in backups[: max(len(backups) - keep, 0)]:
        old.unlink()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="nurse_schedule.db", help="database to back up")
    parser.add_argument("--dest", default="~/pacu-backups", help="backup folder")
    parser.add_argument("--keep", type=int, default=30, help="how many daily copies to keep")
    args = parser.parse_args(argv)
    try:
        target = backup(Path(args.db).expanduser(), Path(args.dest).expanduser(), max(args.keep, 1))
    except (OSError, sqlite3.Error) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Backed up to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
