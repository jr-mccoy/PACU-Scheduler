import pandas as pd

from scheduler.assignment import apply_assignment, revert_assignment


def test_apply_assignment_updates_the_cell_and_counts():
    dt = pd.Timestamp("2026-01-05")
    sched = pd.DataFrame(index=[dt], data={"main": [None], "backup": [None]})
    main_counts = pd.Series({"Alice": 0})
    backup_counts = pd.Series({"Alice": 0})

    mutation = apply_assignment(
        schedule_df=sched,
        main_counts=main_counts,
        backup_counts=backup_counts,
        date=dt,
        role="main",
        nurse="Alice",
    )
    assert sched.at[dt, "main"] == "Alice"
    assert int(main_counts["Alice"]) == 1
    assert mutation.next_nurse == "Alice"


def test_revert_assignment_never_goes_negative():
    dt = pd.Timestamp("2026-01-05")
    sched = pd.DataFrame(index=[dt], data={"main": ["Alice"], "backup": [None]})
    main_counts = pd.Series({"Alice": 0})
    backup_counts = pd.Series({"Alice": 0})

    mutation = apply_assignment(
        schedule_df=sched,
        main_counts=main_counts,
        backup_counts=backup_counts,
        date=dt,
        role="main",
        nurse="Alice",
    )
    revert_assignment(
        schedule_df=sched,
        main_counts=main_counts,
        backup_counts=backup_counts,
        mutation=mutation,
    )
    assert int(main_counts["Alice"]) >= 0
