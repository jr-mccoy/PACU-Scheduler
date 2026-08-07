"""Smoke tests for the stable backend package entrypoint."""

import scheduler


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
        "BackendService",
        "SchedulerService",
        "build_scheduler_service",
        "ensure_schema",
    ]

    missing = [name for name in required if not hasattr(scheduler, name)]
    assert not missing, f"scheduler package missing exports: {missing}"
