import pandas as pd

from scheduler.assignment import apply_assignment, revert_assignment


def test_apply_assignment_updates_counts_and_last_assignment():
    dt = pd.Timestamp("2026-01-05")
    sched = pd.DataFrame(index=[dt], data={"main": [None], "backup": [None]})
    main_counts = pd.Series({"Alice": 0})
    backup_counts = pd.Series({"Alice": 0})
    last_assignment = {"Alice": None}

    mutation = apply_assignment(
        schedule_df=sched,
        main_counts=main_counts,
        backup_counts=backup_counts,
        last_assignment=last_assignment,
        date=dt,
        role="main",
        nurse="Alice",
    )
    assert sched.at[dt, "main"] == "Alice"
    assert int(main_counts["Alice"]) == 1
    assert last_assignment["Alice"] == dt
    assert mutation.next_nurse == "Alice"


def test_revert_assignment_never_goes_negative():
    dt = pd.Timestamp("2026-01-05")
    sched = pd.DataFrame(index=[dt], data={"main": ["Alice"], "backup": [None]})
    main_counts = pd.Series({"Alice": 0})
    backup_counts = pd.Series({"Alice": 0})
    last_assignment = {"Alice": dt}

    mutation = apply_assignment(
        schedule_df=sched,
        main_counts=main_counts,
        backup_counts=backup_counts,
        last_assignment=last_assignment,
        date=dt,
        role="main",
        nurse="Alice",
    )
    revert_assignment(
        schedule_df=sched,
        main_counts=main_counts,
        backup_counts=backup_counts,
        last_assignment=last_assignment,
        mutation=mutation,
    )
    assert int(main_counts["Alice"]) >= 0
