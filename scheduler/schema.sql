-- Canonical schema for the PACU scheduler database.
--
-- Every statement is idempotent, so this file is safe to run against both a
-- brand-new database and an existing one. `ensure_schema()` in
-- `scheduler/repositories.py` executes it before any repository touches the
-- database, which is what lets a fresh clone bootstrap itself with no seed
-- data checked into the repository.

CREATE TABLE IF NOT EXISTS nurses (
    nurse_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT UNIQUE NOT NULL,
    is_prn        INTEGER DEFAULT 0 CHECK (is_prn IN (0, 1)),
    is_late_shift INTEGER DEFAULT 0 CHECK (is_late_shift IN (0, 1)),
    joined_date   DATE DEFAULT CURRENT_DATE,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS unavailable_dates (
    nurse_id INTEGER,
    date     DATE,
    PRIMARY KEY (nurse_id, date),
    FOREIGN KEY (nurse_id) REFERENCES nurses(nurse_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pre_scheduled_assignments (
    date            DATE PRIMARY KEY,
    main_nurse_id   INTEGER,
    backup_nurse_id INTEGER,
    note            TEXT,
    FOREIGN KEY (main_nurse_id) REFERENCES nurses(nurse_id) ON DELETE SET NULL,
    FOREIGN KEY (backup_nurse_id) REFERENCES nurses(nurse_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS schedule_history (
    date            DATE PRIMARY KEY,
    main_nurse_id   INTEGER,
    backup_nurse_id INTEGER,
    FOREIGN KEY (main_nurse_id) REFERENCES nurses(nurse_id) ON DELETE SET NULL,
    FOREIGN KEY (backup_nurse_id) REFERENCES nurses(nurse_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS weekend_assignments (
    weekend_start DATE PRIMARY KEY,
    fsf_nurse_id  INTEGER,
    sfs_nurse_id  INTEGER,
    FOREIGN KEY (fsf_nurse_id) REFERENCES nurses(nurse_id) ON DELETE SET NULL,
    FOREIGN KEY (sfs_nurse_id) REFERENCES nurses(nurse_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS weekend_rotation_history (
    nurse_id              INTEGER PRIMARY KEY,
    last_pattern          TEXT CHECK (last_pattern IN ('FSF', 'SFS')),
    expected_next_pattern TEXT CHECK (expected_next_pattern IN ('FSF', 'SFS')),
    FOREIGN KEY (nurse_id) REFERENCES nurses(nurse_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assignment_counts (
    nurse_id     INTEGER PRIMARY KEY,
    main_count   INTEGER DEFAULT 0,
    backup_count INTEGER DEFAULT 0,
    total_count  INTEGER DEFAULT 0,
    last_updated DATE,
    FOREIGN KEY (nurse_id) REFERENCES nurses(nurse_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rotation_violation_stats (
    nurse_id            INTEGER PRIMARY KEY,
    violation_count     INTEGER NOT NULL DEFAULT 0,
    last_violation_date TEXT,
    consec_violations   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (nurse_id) REFERENCES nurses(nurse_id)
);

CREATE TABLE IF NOT EXISTS rotation_violation_dates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    nurse_id         INTEGER NOT NULL,
    violation_date   TEXT NOT NULL,
    pattern          TEXT NOT NULL,
    previous_pattern TEXT NOT NULL,
    FOREIGN KEY (nurse_id) REFERENCES nurses(nurse_id),
    UNIQUE (nurse_id, violation_date)
);

CREATE INDEX IF NOT EXISTS idx_unavailable_dates_nurse_id
    ON unavailable_dates(nurse_id);
CREATE INDEX IF NOT EXISTS idx_pre_scheduled_main_nurse
    ON pre_scheduled_assignments(main_nurse_id);
CREATE INDEX IF NOT EXISTS idx_pre_scheduled_backup_nurse
    ON pre_scheduled_assignments(backup_nurse_id);
CREATE INDEX IF NOT EXISTS idx_schedule_history_main
    ON schedule_history(main_nurse_id);
CREATE INDEX IF NOT EXISTS idx_schedule_history_backup
    ON schedule_history(backup_nurse_id);
CREATE INDEX IF NOT EXISTS idx_weekend_assignments_fsf
    ON weekend_assignments(fsf_nurse_id);
CREATE INDEX IF NOT EXISTS idx_weekend_assignments_sfs
    ON weekend_assignments(sfs_nurse_id);
