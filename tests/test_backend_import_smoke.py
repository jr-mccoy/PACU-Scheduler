"""Smoke tests for backend module entrypoint and compatibility shims."""

import scheduler
import NCSSQL55
import NCSSQL57


def test_stable_backend_public_api_exports_expected_symbols():
    required = [
        "NurseScheduler",
        "SchedulerConfig",
        "build_scheduler_from_settings",
        "SharedSettings",
        "NurseManager",
        "PreScheduler",
        "AssignmentHistory",
        "WeekendHistory",
        "_evaluate_variant_worker",
        "NurseSchedulerUI",
    ]

    missing = [name for name in required if not hasattr(scheduler, name)]
    assert not missing, f"scheduler package missing exports: {missing}"


def test_compatibility_shims_reexport_stable_entrypoint_symbols():
    assert NCSSQL55.NurseScheduler is scheduler.NurseScheduler
    assert NCSSQL55.SchedulerConfig is scheduler.SchedulerConfig
    assert NCSSQL57.build_scheduler_from_settings is scheduler.build_scheduler_from_settings
    assert NCSSQL57.SharedSettings is scheduler.SharedSettings
