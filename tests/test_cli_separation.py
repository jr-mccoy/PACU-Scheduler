"""Phase 3 invariants — CLI presenter is no longer inside ``scheduler/``.

These guards fail the build if the CLI menu loop, terminal prompts, or
``NurseSchedulerUI`` constructor are reintroduced into the ``scheduler``
package or wired into the GUI as its backend service object.
"""

import pathlib
import re

import scheduler
from scheduler import (
    BackendService,
    SchedulerService,
    build_scheduler_service,
)


def test_scheduler_package_contains_no_input_calls():
    """``scheduler/**`` is algorithm code; ``input(`` belongs in ``cli/``."""
    scheduler_root = pathlib.Path(scheduler.__file__).resolve().parent
    # Match calls only, ignoring identifiers like ``user_input`` or docstrings
    # that reference ``input()`` in prose. ``\binput\s*\(`` catches both.
    pattern = re.compile(r"\binput\s*\(")
    offenders = []
    for path in scheduler_root.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            # Skip docstrings/comments lines that merely mention input().
            if stripped.startswith("#") or "``input(" in line:
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(scheduler_root.parent)}:{lineno}: {stripped}")
    assert not offenders, (
        "scheduler/** must not call input(); move CLI code to the `cli` package:\n"
        + "\n".join(offenders)
    )


def test_ui_does_not_import_nurse_scheduler_ui():
    """The GUI must compose the backend via ``build_scheduler_service``."""
    ui_root = pathlib.Path(__file__).resolve().parent.parent / "ui"
    pattern = re.compile(r"\bNurseSchedulerUI\b")
    offenders = []
    for path in ui_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(path.relative_to(ui_root.parent)))
    assert not offenders, (
        "ui/** must not reference NurseSchedulerUI; use build_scheduler_service: "
        + ", ".join(offenders)
    )


def test_scheduler_service_advertises_the_attributes_the_gui_relies_on():
    """``BackendService`` is a Protocol; verify ``SchedulerService`` matches it.

    Constructing a real :class:`SchedulerService` requires an initialised
    SQLite schema (see ``tests/test_scheduler_characterization.py``); for the
    purpose of guarding the public surface we inspect the class attributes
    instead of instantiating.
    """
    for attr in (
        "settings",
        "nurse_manager",
        "pre_scheduler",
        "weekend_history",
        "assignment_history",
        "weekend_history_service",
        "violation_history_service",
        "sync_assignment_history_with_weekend",
    ):
        assert (
            hasattr(SchedulerService, attr) or attr in SchedulerService.__init__.__code__.co_names
        ), f"SchedulerService missing {attr}"
    assert callable(SchedulerService.sync_assignment_history_with_weekend)
    assert callable(build_scheduler_service)
    # Protocol membership is structural — the protocol must list the same
    # attrs that the GUI actually reads on ``self.backend``.
    for attr in (
        "settings",
        "nurse_manager",
        "pre_scheduler",
        "weekend_history",
        "assignment_history",
        "sync_assignment_history_with_weekend",
    ):
        assert attr in BackendService.__annotations__ or hasattr(BackendService, attr), (
            f"BackendService protocol missing {attr}"
        )


def test_cli_package_owns_nurse_scheduler_ui():
    from cli import NurseSchedulerUI as CLI_NurseSchedulerUI
    from cli.nurse_scheduler_ui import NurseSchedulerUI as ModuleClass

    assert CLI_NurseSchedulerUI is ModuleClass
    assert CLI_NurseSchedulerUI.__module__ == "cli.nurse_scheduler_ui"
