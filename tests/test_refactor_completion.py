"""Guards proving the deprecated monolith paths stay compatibility-only."""

from __future__ import annotations

import ast
from pathlib import Path

import scheduler


ROOT = Path(__file__).resolve().parents[1]


def _top_level_classes(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.name for node in tree.body if isinstance(node, ast.ClassDef)]


def test_backend_types_are_owned_by_focused_modules():
    expected_owners = {
        scheduler.ScheduleVariant: "scheduler.domain",
        scheduler.NurseScheduler: "scheduler.engine",
        scheduler.WeekendHistory: "scheduler.repositories",
        scheduler.NurseManager: "scheduler.repositories",
        scheduler.AssignmentHistory: "scheduler.repositories",
        scheduler.PreScheduler: "scheduler.repositories",
        scheduler.SharedSettings: "scheduler.settings",
        scheduler.PerformanceReport: "scheduler.profiling",
    }
    assert {
        cls: cls.__module__ for cls in expected_owners
    } == expected_owners


def test_legacy_modules_define_no_concrete_classes():
    assert _top_level_classes(ROOT / "scheduler" / "legacy_core.py") == []
    assert _top_level_classes(ROOT / "ui" / "legacy.py") == []


def test_root_compatibility_files_define_no_concrete_classes():
    for filename in ("NCSSQL55.py", "NCSSQL57.py", "NCSSQLGUIFINALIST57.py"):
        assert _top_level_classes(ROOT / filename) == []


def test_production_modules_do_not_import_legacy_implementations():
    offenders: list[str] = []
    for package in (ROOT / "scheduler", ROOT / "ui", ROOT / "cli"):
        for path in package.rglob("*.py"):
            if path.name == "legacy_core.py" or path.name == "legacy.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "from scheduler.legacy_core import" in text:
                offenders.append(str(path.relative_to(ROOT)))
            if "from .legacy import" in text or "from ..legacy import" in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
