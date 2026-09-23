"""CLI presenter and helpers extracted from ``scheduler.legacy_core``.

These classes implement the text-mode menu-driven UI for the nurse
scheduler. They were previously bundled into ``scheduler.legacy_core``,
which forced every importer of the algorithm package to load the CLI
plumbing. Moving them here keeps ``scheduler/`` algorithm-only and lets the
GUI compose backend services via :func:`scheduler.build_scheduler_service`
instead of constructing a CLI presenter as its "backend".
"""

from __future__ import annotations

import calendar
import logging
import os
import platform
import shutil
import subprocess
import sys
import traceback
from datetime import date, timedelta
from typing import Any

import pandas as pd

from scheduler import (
    ASSIGNMENT_DEBUG_LOGGER,
    DEFAULT_MAX_WEEKEND_VARIANTS,
    MAX_WEEKEND_VARIANTS_RANGE,
    AssignmentHistory,
    DateUtils,
    NurseManager,
    NurseScheduler,
    PreScheduler,
    SharedSettings,
    WeekendHistory,
    WeekendPattern,
    apply_schedule,
    build_scheduler_from_settings,
    default_worker_count,
)
from scheduler.engine import recorded_weekends_in_range, search_capped_note

logger = logging.getLogger(__name__)


class CLIHelper:
    """Helper class for CLI operations."""

    @staticmethod
    def is_interactive():
        """Check if we're running in an interactive environment."""
        return sys.stdin.isatty()

    @staticmethod
    def clear_screen() -> None:
        try:
            os.system("cls" if os.name == "nt" else "clear")
        except Exception:
            try:
                print("\033c", end="")  # ANSI escape code to clear screen
            except Exception:
                print("\n" * 50)  # Fallback: print multiple newlines

    @staticmethod
    def print_header(title: str) -> None:
        header = f"=== {title} ==="
        print("\n" + "=" * len(header))
        print(header)
        print("=" * len(header) + "\n")

    @staticmethod
    def pause(message: str = "Press Enter to continue...") -> None:
        if CLIHelper.is_interactive():
            try:
                input(message)
            except EOFError:
                print("\nNon-interactive mode detected. Continuing...")
                pass
        else:
            print(message)

    @staticmethod
    def display_menu(title: str, options: dict) -> str:
        CLIHelper.print_header(title)
        for key, option in options.items():
            print(f"{key}. {option}")
        print()  # Add a newline for better spacing

        if not CLIHelper.is_interactive():
            print("\nError: This program requires an interactive terminal.")
            print("Please run this program in a proper terminal that supports user input.")
            print("\nPossible solutions:")
            print("1. Use a different terminal emulator")
            print("2. Run Python in interactive mode")
            print("3. Use SSH to connect to your device and run from there")
            print("4. Use a Python IDE with proper terminal support")
            sys.exit(1)

        try:
            return input("Enter your choice: ").strip()
        except EOFError:
            print("\nError: Unable to read input. EOF detected.")
            print("This program requires an interactive terminal.")
            sys.exit(1)
        except KeyboardInterrupt:
            print("\nOperation cancelled by user.")
            sys.exit(0)

    @staticmethod
    def display_table(data: list, headers: list, title: str = "") -> None:
        if not data:
            print("No data to display.")
            return
        col_widths = [len(h) for h in headers]
        for row in data:
            for i, item in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(item)))
        header_row = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
        separator = "-+-".join("-" * w for w in col_widths)
        if title:
            print(f"\n{title}")
        print(f"\n{header_row}")
        print(f"{separator}")
        for row in data:
            formatted_row = " | ".join(str(item).ljust(col_widths[i]) for i, item in enumerate(row))
            print(formatted_row)


class VisualCalendarUI:
    """
    Visual calendar interface for editing nurse unavailable dates.
    Provides a text-based calendar UI with navigation and date selection.
    """

    def __init__(self, nurse_manager: Any) -> None:
        """
        Initialize the Visual Calendar UI.
        :param nurse_manager: The nurse manager instance that holds nurse data.
        """
        self.nurse_manager = nurse_manager
        today = date.today()
        self.current_year = today.year
        self.current_month = today.month
        self.selected_dates: set[str] = set()  # Working set of selected date strings

    def _clear_screen(self) -> None:
        """Cross-platform screen clear."""
        cmd = "cls" if platform.system() == "Windows" else "clear"

        if shutil.which(cmd):
            if cmd == "cls":
                subprocess.call(cmd, shell=True)
            else:
                subprocess.call([cmd])
            return

        # Fallbacks
        try:
            print("\033c", end="", flush=True)  # ANSI "full reset"
        except Exception:
            print("\n" * 40)  # Last resort

    def _print_calendar_header(self, nurse_name: str | None = None) -> None:
        """Print the calendar header along with context information."""
        print(f"=== {calendar.month_name[self.current_month]} {self.current_year} ===")
        if nurse_name:
            print(f"Editing unavailable dates for: {nurse_name}")
        print("  Mon  Tue  Wed  Thu  Fri  Sat  Sun")

    def _print_calendar_days(self) -> None:
        """Print the calendar days with selected dates marked."""
        month_calendar = calendar.monthcalendar(self.current_year, self.current_month)
        for week in month_calendar:
            week_str = ""
            for day in week:
                if day == 0:
                    week_str += "     "
                else:
                    day_date = date(self.current_year, self.current_month, day)
                    date_str = day_date.isoformat()
                    if date_str in self.selected_dates:
                        week_str += " [X] "
                    else:
                        week_str += f"  {day:2}  "
            print(week_str)

    def _print_controls(self) -> None:
        """Display the controls available to the user."""
        print("\nControls:")
        print(" • Enter day number(s) (comma separated) to toggle selection")
        print(" • [N] Next month | [P] Previous month")
        print(" • [D] Done and Save | [C] Cancel (revert changes)")

    def display_calendar(self, nurse_name: str | None = None) -> set[str]:
        """
        Display the calendar and allow the user to toggle dates, navigate months, or finish editing.
        :param nurse_name: Optional nurse name to display for context.
        :return: A set of selected date strings.
        """
        initial_selected = self.selected_dates.copy()
        while True:
            self._clear_screen()
            self._print_calendar_header(nurse_name)
            self._print_calendar_days()
            self._print_controls()
            choice = input("Enter choice: ").strip()
            command = choice.upper()

            if command == "N":
                self._next_month()
            elif command == "P":
                self._previous_month()
            elif command == "D":
                return self.selected_dates
            elif command == "C":
                return initial_selected
            else:
                self._process_day_entries(choice)

    def _next_month(self) -> None:
        """Advance to the next month."""
        if self.current_month == 12:
            self.current_month = 1
            self.current_year += 1
        else:
            self.current_month += 1

    def _previous_month(self) -> None:
        """Go back to the previous month."""
        if self.current_month == 1:
            self.current_month = 12
            self.current_year -= 1
        else:
            self.current_month -= 1

    def _process_day_entries(self, entries_str: str) -> None:
        """
        Process comma-separated day entries to toggle date selection.
        :param entries_str: Comma-separated string of day numbers.
        """
        entries = [entry.strip() for entry in entries_str.split(",") if entry.strip()]
        _, last_day = calendar.monthrange(self.current_year, self.current_month)
        error_messages = []

        for entry in entries:
            try:
                day_int = int(entry)
                if 1 <= day_int <= last_day:
                    date_str = date(self.current_year, self.current_month, day_int).isoformat()
                    if date_str in self.selected_dates:
                        self.selected_dates.remove(date_str)
                    else:
                        self.selected_dates.add(date_str)
                else:
                    error_messages.append(
                        f"Day {entry} is not valid for {calendar.month_name[self.current_month]}."
                    )
            except ValueError:
                error_messages.append(f"Invalid input: {entry}")

        if error_messages:
            print("\n".join(error_messages))
        input("Press Enter to continue...")

    def edit_unavailable_dates(self, nurse_name: str) -> set[str]:
        """
        Edit unavailable dates for a given nurse.
        :param nurse_name: The name of the nurse.
        :return: The updated set of unavailable date strings.
        """
        current_dates = self.nurse_manager.get_unavailable_dates(nurse_name)
        self.selected_dates = {
            date.date().isoformat() for date in current_dates if date is not None
        }
        return self.display_calendar(nurse_name)


class InputValidator:
    """Utility class for validating user inputs."""

    @staticmethod
    def validate_date(date_str: str) -> date | None:
        """
        Validate a date string in YYYY-MM-DD format.
        Args:
            date_str: String containing the date
        Returns:
            date object if valid, None otherwise
        """
        try:
            return date.fromisoformat(date_str)
        except ValueError:
            print(f"Error: Invalid date format '{date_str}'. Please use YYYY-MM-DD format.")
            return None

    @staticmethod
    def validate_nurse_name(name: str) -> bool:
        """
        Validate that a nurse name is not empty.
        Args:
            name: Nurse name to validate
        Returns:
            True if valid, False otherwise
        """
        if not name.strip():
            print("Error: Nurse name cannot be empty.")
            return False
        return True

    @staticmethod
    def get_integer_input(prompt: str, valid_range: range | None = None) -> int:
        """
        Prompt the user for an integer input and validate that it is within the valid_range if provided.
        """
        while True:
            try:
                value = int(input(prompt).strip())
                if valid_range is not None and value not in valid_range:
                    print(
                        f"Error: Please enter a number in the range {valid_range.start} to {valid_range.stop - 1}."
                    )
                    continue
                return value
            except ValueError:
                print("Error: Please enter a valid integer.")

    @staticmethod
    def confirm_action(prompt: str = "Continue?", default: str = "n") -> bool:
        """
        Ask user to confirm an action.
        Args:
            prompt: Question to ask the user
            default: Default response if user just presses Enter
        Returns:
            True if user confirms, False otherwise
        """
        valid_yes = ["y", "yes"]
        valid_no = ["n", "no"]
        default = default.lower()
        if default in valid_yes:
            options = "[Y/n]"
        elif default in valid_no:
            options = "[y/N]"
        else:
            options = "[y/n]"
        while True:
            response = input(f"{prompt} {options}: ").strip().lower()
            if not response:
                response = default
            if response in valid_yes:
                return True
            if response in valid_no:
                return False
            else:
                print("Please respond with 'yes' (y) or 'no' (n).")


class NurseSchedulerUI:
    """
    User Interface for the Nurse Scheduler System.
    Manages menus and interactions with the underlying nurse scheduling logic.
    """

    def __init__(self, db_name: str = "nurse_schedule.db"):
        logger.info(f"Initializing NurseSchedulerUI with database: {db_name}")
        try:
            # Load the same settings the GUI uses
            self.settings = SharedSettings()

            self.nurse_manager = NurseManager(db_name)
            self.pre_scheduler = PreScheduler(db_name)
            self.weekend_history = WeekendHistory(db_name)
            self.weekend_history_service = self.weekend_history.weekend_service
            self.violation_history_service = self.weekend_history.violation_service
            self.assignment_history = AssignmentHistory(db_name)
            self.calendar_ui = VisualCalendarUI(self.nurse_manager)
        except Exception as e:
            logger.error(f"Error initializing NurseSchedulerUI: {e}")
            print(f"Error initializing system: {e}")
            raise

    # ============================================================================
    # UTILITY AND HELPER METHODS
    # ============================================================================

    @staticmethod
    def _long_term_score(nurse_counts: dict[str, dict[str, int]], overage: dict[str, int]) -> int:
        """
        Penalty that prefers variants which *reduce* prior over-use.
        We compute, for every nurse:
            extra = max(0, overage[n] + this_variant_total - min_total)
        and sum those extras.  Lower = better.  Zero means that every
        previously over-worked nurse lands at or below the minimum
        assignment count in this variant.
        Works even if `overage` is all zeros.
        """
        if not nurse_counts:
            return 0
        min_total = min(c["total"] for c in nurse_counts.values())
        penalty = 0
        for n, c in nurse_counts.items():
            prior = overage.get(n, 0)
            penalty += max(0, prior + c["total"] - min_total)
        return penalty

    def _normalize_date(self, date_input) -> pd.Timestamp | None:
        """Normalize input date to pandas Timestamp at midnight. Return None if invalid."""
        normalized_date = DateUtils.safe_normalize_date(date_input)
        if normalized_date is None or pd.isna(normalized_date):
            return None
        return normalized_date

    def _get_nurse_name(self, prompt: str = "Enter nurse name: ") -> str | None:
        """Get and validate a nurse name from user input."""
        name = input(prompt).strip()
        if InputValidator.validate_nurse_name(name):
            return name
        return None

    def _get_date_input(self, prompt: str) -> pd.Timestamp | None:
        """Get and validate a date from user input."""
        date_str = input(prompt).strip()
        if not date_str:
            return None
        return self._normalize_date(date_str) if InputValidator.validate_date(date_str) else None

    def _handle_error_with_pause(self, operation: str, error: Exception) -> None:
        """Standard error handling with logging and user pause."""
        logger.error(f"Error in {operation}: {error}")
        print(f"Error: Could not {operation}. {error}")
        CLIHelper.pause()

    def _safe_execute(self, operation_name: str, operation_func, success_message: str = None):
        """Execute an operation with standard error handling."""
        try:
            result = operation_func()
            if success_message:
                print(f"Success: {success_message}")
            return result
        except Exception as e:
            self._handle_error_with_pause(operation_name, e)
            return None

    def _display_menu_and_get_choice(self, title: str, options: dict) -> str:
        """Standard menu display pattern."""
        CLIHelper.clear_screen()
        return CLIHelper.display_menu(title, options)

    def _select_nurse_from_list(self, prompt: str, allow_none: bool = True) -> str | None:
        """Display nurse list and get user selection."""
        nurses = self.nurse_manager.get_nurses()
        if not nurses:
            print("No nurses in the system.")
            return None

        print(f"\n{prompt}")
        for i, nurse in enumerate(nurses, 1):
            print(f"{i}. {nurse}")

        choice = input(
            f"\nEnter number or name{' (leave blank for none)' if allow_none else ''}: "
        ).strip()

        if not choice and allow_none:
            return ""

        if choice.isdigit() and 1 <= int(choice) <= len(nurses):
            return nurses[int(choice) - 1]
        elif choice in nurses:
            return choice
        elif not allow_none:
            print(f"Warning: Nurse '{choice}' is not in the system.")
            return choice if InputValidator.confirm_action("Continue anyway?") else None

        return choice

    # ============================================================================
    # WEEKEND HISTORY MANAGEMENT
    # ============================================================================

    def _handle_view_violation_dates(self) -> None:
        """View violation dates for all nurses or a specific nurse."""
        nurse_input = input("Enter nurse name (or press Enter for all nurses): ").strip()
        nurse = nurse_input if nurse_input else None

        def get_violations():
            violations = self.weekend_history.get_violation_dates(nurse)
            if not violations:
                print("No rotation violations found.")
                return None

            data = []
            for nurse_name, violation_date, pattern, prev_pattern in violations:
                date_obj = pd.to_datetime(violation_date).date()
                data.append((nurse_name, date_obj.isoformat(), f"{prev_pattern} → {pattern}"))

            headers = ["Nurse", "Violation Date", "Pattern Repeat"]
            title = "Rotation Violations" + (f" for {nurse}" if nurse else "")
            CLIHelper.display_table(data, headers, title)
            return True

        self._safe_execute("view violation dates", get_violations)
        CLIHelper.pause()

    def _handle_rebuild_violation_history(self) -> None:
        """Rebuild violation history from existing weekend assignments."""
        if not InputValidator.confirm_action(
            "This will rebuild all violation records from weekend history. Continue?", "n"
        ):
            return

        def rebuild_history():
            print("Rebuilding violation history...")
            self.violation_history_service.rebuild()
            print("✅ Violation history rebuilt successfully.")

            # Show summary
            counts = self.weekend_history.get_violation_counts()
            total_violations = sum(counts.values())
            nurses_with_violations = sum(1 for count in counts.values() if count > 0)

            print("\nSummary:")
            print(f"Total violations found: {total_violations}")
            print(f"Nurses with violations: {nurses_with_violations}")
            return True

        self._safe_execute("rebuild violation history", rebuild_history)
        CLIHelper.pause()

    def _handle_add_weekend_assignment(self) -> None:
        """Add a new weekend assignment with normalized dates (no double execution)."""
        weekend_start_input = input("Enter weekend start date (YYYY-MM-DD): ").strip()
        fsf_nurse = self._get_nurse_name("Enter FSF nurse name: ")
        sfs_nurse = self._get_nurse_name("Enter SFS nurse name: ")

        if not (weekend_start_input and fsf_nurse and sfs_nurse):
            print("Invalid input. Operation cancelled.")
            CLIHelper.pause()
            return

        weekend_start = self._normalize_date(weekend_start_input)
        if weekend_start is None:
            print("Invalid date. Operation cancelled.")
            CLIHelper.pause()
            return

        def add_assignment():
            self.weekend_history_service.add_assignment(weekend_start, fsf_nurse, sfs_nurse)
            logger.info(
                f"Added weekend assignment for {weekend_start.date()}: FSF={fsf_nurse}, SFS={sfs_nurse}"
            )

        msg = f"Weekend assignment added for {weekend_start.date()}."
        self._safe_execute("add weekend assignment", add_assignment, msg)
        CLIHelper.pause()

    def _handle_remove_weekend_assignment(self) -> None:
        """Remove weekend assignment with normalized dates."""
        weekend_start_input = input("Enter weekend start date to remove (YYYY-MM-DD): ").strip()
        if not weekend_start_input:
            print("Invalid input. Operation cancelled.")
            CLIHelper.pause()
            return

        def remove_assignment():
            weekend_start = self._normalize_date(weekend_start_input)
            self.weekend_history_service.remove_assignment(weekend_start)
            logger.info(f"Removed weekend assignment for {weekend_start.date()}")
            return f"Weekend assignment removed for {weekend_start.date()}."

        self._safe_execute("remove weekend assignment", remove_assignment)
        CLIHelper.pause()

    def _handle_modify_weekend_assignment(self) -> None:
        """Change one or both nurses for an existing weekend."""
        wk_list = self.weekend_history.get_assignments()
        if not wk_list:
            print("No weekends in history.")
            CLIHelper.pause()
            return

        # Display weekends for selection
        for i, (fri, fsf, sfs) in enumerate(wk_list, 1):
            print(f"{i}. {fri.date()}  FSF={fsf or '–'}  SFS={sfs or '–'}")

        choice = input("Select by number or enter Friday date: ").strip()

        # Determine selected weekend
        if choice.isdigit() and 1 <= int(choice) <= len(wk_list):
            friday = wk_list[int(choice) - 1][0]
        else:
            friday = self._normalize_date(choice)
            if friday is None:
                CLIHelper.pause()
                return

        # Get current assignment
        current = dict((d[0], d[1:]) for d in wk_list).get(friday)
        if current is None:
            print("Weekend not found.")
            CLIHelper.pause()
            return

        # Get new nurses
        new_fsf = self._get_nurse_name(f"FSF nurse [{current[0] or 'unchanged'}]: ") or current[0]
        new_sfs = self._get_nurse_name(f"SFS nurse [{current[1] or 'unchanged'}]: ") or current[1]

        # Validate changes
        if new_fsf == new_sfs:
            print("⚠  FSF and SFS must differ.")
            CLIHelper.pause()
            return

        if new_fsf == current[0] and new_sfs == current[1]:
            print("Nothing changed.")
            CLIHelper.pause()
            return

        def modify_assignment():
            self.weekend_history_service.modify_assignment(friday, new_fsf, new_sfs)
            self._sync_assignment_history_for_weekend(friday, new_fsf, new_sfs)
            return True

        result = self._safe_execute("modify weekend assignment", modify_assignment)
        if result:
            print("✅ Weekend modified.")
        CLIHelper.pause()

    def _handle_delete_weekend_assignment(self) -> None:
        """Remove a weekend from history and delete its daily rows."""
        wk_list = self.weekend_history.get_assignments()
        if not wk_list:
            print("No weekends in history.")
            CLIHelper.pause()
            return

        # Display weekends for selection
        for i, (fri, fsf, sfs) in enumerate(wk_list, 1):
            print(f"{i}. {fri.date()}  FSF={fsf or '–'}  SFS={sfs or '–'}")

        choice = input("Delete by number or Friday date: ").strip()

        # Determine selected weekend
        if choice.isdigit() and 1 <= int(choice) <= len(wk_list):
            friday = wk_list[int(choice) - 1][0]
        else:
            friday = self._normalize_date(choice)
            if friday is None:
                CLIHelper.pause()
                return

        if not InputValidator.confirm_action(f"Delete weekend {friday.date()} ?", "n"):
            CLIHelper.pause()
            return

        def delete_assignment():
            self.weekend_history_service.remove_assignment(friday)
            self._sync_assignment_history_for_weekend(friday, None, None)
            return True

        result = self._safe_execute("delete weekend assignment", delete_assignment)
        if result:
            print("✅ Weekend deleted.")
        CLIHelper.pause()

    # ============================================================================
    # NURSE MANAGEMENT
    # ============================================================================

    def _handle_edit_unavailable_dates(self) -> None:
        """Handle editing a nurse's unavailable dates."""
        name = self._get_nurse_name()
        if not name:
            CLIHelper.pause()
            return

        def update_dates():
            # Get dates from calendar UI and explicitly normalize them
            dates = self.calendar_ui.edit_unavailable_dates(name)
            normalized_dates = {self._normalize_date(date) for date in dates}
            self.nurse_manager.update_unavailable_dates(name, normalized_dates)
            logger.info(f"Updated unavailable dates for nurse: {name}")
            return f"Unavailable dates updated for nurse '{name}'."

        self._safe_execute("update unavailable dates", update_dates)
        CLIHelper.pause()

    def _handle_view_all_unavailable_dates(self) -> None:
        """Display every nurse with all their currently recorded unavailable dates."""
        from collections import defaultdict

        def prepare_table_data():
            table_data = []
            for name, info in sorted(self.nurse_manager.nurses.items()):
                dates = sorted(info["unavailable_dates"])
                if dates:
                    by_month = defaultdict(list)
                    for dt in dates:
                        month_key = dt.strftime("%b %Y")
                        by_month[month_key].append(dt.strftime("%d"))
                    parts = [f"{month}: {', '.join(days)}" for month, days in by_month.items()]
                    dates_str = "; ".join(parts)
                else:
                    dates_str = "None"
                table_data.append((name, dates_str))
            return table_data

        table_data = prepare_table_data()
        CLIHelper.clear_screen()
        CLIHelper.print_header("All Nurses' Unavailable Dates")
        CLIHelper.display_table(table_data, ["Nurse", "Unavailable Dates"])
        CLIHelper.pause()

    def _handle_add_nurse(self) -> None:
        """Handle adding a new nurse to the system."""
        name = self._get_nurse_name()
        if not name:
            CLIHelper.pause()
            return

        def add_nurse():
            self.nurse_manager.add_nurse(name)
            logger.info(f"Added nurse: {name}")
            return f"Nurse '{name}' has been added to the system."

        self._safe_execute("add nurse", add_nurse)
        CLIHelper.pause()

    def _handle_remove_nurse(self) -> None:
        """Handle removing a nurse from the system."""
        nurses = self.nurse_manager.get_nurses()
        if not nurses:
            print("No nurses in the system to remove.")
            CLIHelper.pause()
            return

        # Display nurses for selection
        print("\nCurrent Nurses:")
        for i, nurse in enumerate(nurses, 1):
            print(f"{i}. {nurse}")

        choice = input("\nEnter nurse number to remove or name: ").strip()

        # Determine selected nurse
        if choice.isdigit() and 1 <= int(choice) <= len(nurses):
            name = nurses[int(choice) - 1]
        else:
            name = choice

        if not InputValidator.validate_nurse_name(name):
            CLIHelper.pause()
            return

        if not InputValidator.confirm_action(f"Are you sure you want to remove nurse '{name}'?"):
            print("Operation cancelled.")
            CLIHelper.pause()
            return

        def remove_nurse():
            self.nurse_manager.remove_nurse(name)
            logger.info(f"Removed nurse: {name}")
            return f"Nurse '{name}' has been removed from the system."

        self._safe_execute("remove nurse", remove_nurse)
        CLIHelper.pause()

    def _handle_set_nurse_status(self, status_type: str) -> None:
        """Generic handler for setting nurse status (PRN or Late Shift)."""
        name = self._get_nurse_name()
        if not name:
            CLIHelper.pause()
            return

        def update_status():
            # Get current status
            if status_type == "PRN":
                current_status = self.nurse_manager.get_prn_status(name)
            elif status_type == "Late Shift":
                current_status = self.nurse_manager.get_late_shift_status(name)
            else:
                raise ValueError(f"Unknown status type: {status_type}")

            print(f"Current {status_type} status for {name}: {'Yes' if current_status else 'No'}")
            status = InputValidator.confirm_action(
                f"Set {name} as {status_type}?", "y" if current_status else "n"
            )

            # Update status
            if status_type == "PRN":
                self.nurse_manager.set_prn_status(name, status)
            elif status_type == "Late Shift":
                self.nurse_manager.set_late_shift_status(name, status)

            logger.info(f"Updated {status_type} status for nurse {name} to {status}")
            return f"{status_type} status updated for nurse '{name}'."

        self._safe_execute(f"set {status_type} status", update_status)
        CLIHelper.pause()

    def _handle_set_prn_status(self) -> None:
        """Handle setting a nurse's PRN status."""
        self._handle_set_nurse_status("PRN")

    def _handle_set_late_shift_status(self) -> None:
        """Handle setting a nurse's Late Shift status."""
        self._handle_set_nurse_status("Late Shift")

    # ============================================================================
    # ASSIGNMENT HISTORY MANAGEMENT
    # ============================================================================

    def _sync_assignment_history_for_weekend(
        self, friday: pd.Timestamp, fsf_nurse: str | None, sfs_nurse: str | None
    ) -> None:
        """Safely update the three schedule_history rows for weekend pattern."""
        friday = DateUtils.normalize_date(friday)
        saturday = friday + timedelta(days=1)
        sunday = friday + timedelta(days=2)

        # Capture existing data before modifications
        existing_records = {}
        for day in (friday, saturday, sunday):
            record = self.assignment_history.get_record(day)
            if record:
                existing_records[day] = record

        # Prepare new pattern
        new_pattern = []
        if fsf_nurse or sfs_nurse:
            new_pattern = [
                (friday, fsf_nurse, sfs_nurse),
                (saturday, sfs_nurse, fsf_nurse),
                (sunday, fsf_nurse, sfs_nurse),
            ]

        try:
            # Delete existing records
            for day in (friday, saturday, sunday):
                self.assignment_history.delete_record(day)

            # Insert new records
            for day, main, backup in new_pattern:
                if main or backup:
                    self.assignment_history.update_history(day, main, backup)

            logger.info(f"Successfully synced assignment history for weekend {friday.date()}")

        except Exception as e:
            logger.error(f"Failed to sync assignment history for weekend {friday.date()}: {e}")

            # Restore original data
            try:
                for day in (friday, saturday, sunday):
                    self.assignment_history.delete_record(day)

                for day, (orig_main, orig_backup) in existing_records.items():
                    self.assignment_history.update_history(day, orig_main, orig_backup)

                logger.info(f"Restored original assignment history for weekend {friday.date()}")
            except Exception as restore_error:
                logger.critical(
                    f"Failed to restore assignment history for weekend {friday.date()}: {restore_error}. "
                    f"Original data: {existing_records}"
                )
                raise RuntimeError(
                    f"Failed to sync weekend {friday.date()} and could not restore original data. "
                    f"Manual intervention may be required. Original error: {e}"
                ) from e

            raise

    def _handle_sync_assignment_history_with_weekend(self) -> None:
        """Syncs assignment history with weekend history."""
        weekends = self.weekend_history.get_assignments()
        if not weekends:
            print("No weekends in weekend history to sync.")
            CLIHelper.pause()
            return

        changes, conflicts = self._analyze_sync_requirements(weekends)

        # Show preview
        self._display_sync_preview(changes, conflicts)

        if not changes and not conflicts:
            print("Assignment history is already in sync with weekend history.")
            CLIHelper.pause()
            return

        # Get user confirmation and apply changes
        self._apply_sync_changes(changes, conflicts)
        CLIHelper.pause()

    def _analyze_sync_requirements(self, weekends):
        """Analyze what changes are needed for sync."""
        changes = []
        conflicts = []

        for friday, fsf, sfs in weekends:
            friday = DateUtils.normalize_date(friday)
            saturday = friday + timedelta(days=1)
            sunday = friday + timedelta(days=2)

            expected = [
                (friday, fsf, sfs),
                (saturday, sfs, fsf),
                (sunday, fsf, sfs),
            ]

            for day, main, backup in expected:
                current = self.assignment_history.get_record(day)
                if not current or (not current[0] and not current[1]):
                    changes.append((day, main, backup, "add"))
                elif (current[0] != main) or (current[1] != backup):
                    conflicts.append((day, main, backup, current))

        return changes, conflicts

    def _display_sync_preview(self, changes, conflicts):
        """Display sync preview to user."""
        print("\n=== Assignment History Sync Preview ===")
        print(f"Assignments to add: {len(changes)}")
        print(f"Conflicts to resolve: {len(conflicts)}\n")

        if changes:
            print("Assignments to be added:")
            for day, main, backup, _ in changes:
                print(f"  {day.date()}: Main={main or '-'}, Backup={backup or '-'}")
            print()

        if conflicts:
            print("Conflicting assignments found:")
            for day, main, backup, current in conflicts:
                print(f"  {day.date()}:")
                print(f"    Weekend History: Main={main or '-'}, Backup={backup or '-'}")
                print(f"    Assignment Hist: Main={current[0] or '-'}, Backup={current[1] or '-'}")
            print()

    def _apply_sync_changes(self, changes, conflicts):
        """Apply the sync changes based on user confirmation."""
        # Confirm additions
        if changes and not InputValidator.confirm_action(
            "Add missing assignments to assignment history?", "y"
        ):
            print("No changes made.")
            return

        # Confirm overwrites
        overwrite = False
        if conflicts:
            overwrite = InputValidator.confirm_action(
                "Overwrite conflicting assignments in assignment history with weekend history values?",
                "n",
            )
            if not overwrite:
                print("Conflicting assignments were not changed.")

        # Apply changes
        for day, main, backup, _ in changes:
            self.assignment_history.update_history(day, main, backup)
            logger.info(f"Added assignment for {day.date()}: Main={main}, Backup={backup}")

        if overwrite:
            for day, main, backup, current in conflicts:
                self.assignment_history.update_history(day, main, backup)
                logger.info(
                    f"Overwrote assignment for {day.date()}: Main={main}, Backup={backup} (was Main={current[0]}, Backup={current[1]})"
                )

        print("Sync complete.")

    # ============================================================================
    # MENU SYSTEMS
    # ============================================================================

    def main_menu(self) -> None:
        """Display the main menu and handle user input until exit."""
        menu_options = {
            "1": ("Manage Nurses", self.nurse_management_menu),
            "2": ("Manual Scheduling", self.manual_scheduling_menu),
            "3": ("Create Schedule", self.create_schedule_menu),
            "4": ("View Current Nurses", lambda: (self.view_nurses(), CLIHelper.pause())),
            "5": ("View Weekend History", lambda: (self.view_weekend_history(), CLIHelper.pause())),
            "6": ("Manage Weekend History", self.manage_weekend_history_menu),
            "7": ("Manage Assignment History", self.manage_assignment_history_menu),
            "8": ("Advanced Weekend Stats", self.advanced_weekend_stats_menu),
            "9": (
                "Sync Assignment History / Weekend History",
                self._handle_sync_assignment_history_with_weekend,
            ),
            "10": ("Settings", self.settings_menu),
            "11": ("Exit         (⁠ಠ⁠_⁠ಠ⁠)⁠>⁠⌐⁠■⁠-⁠■         (⁠⌐⁠■⁠-⁠■⁠)", None),
        }

        self._run_menu_loop(
            "Nurse Scheduler System",
            menu_options,
            exit_option="11",
            exit_message="Thank you for using the Nurse Scheduler System. Goodbye!",
        )

    def nurse_management_menu(self) -> None:
        """Display the nurse management menu and handle related operations."""
        menu_options = {
            "1": ("Add Nurse", self._handle_add_nurse),
            "2": ("Remove Nurse", self._handle_remove_nurse),
            "3": ("Edit Unavailable Dates", self._handle_edit_unavailable_dates),
            "4": ("View All Unavailable Dates", self._handle_view_all_unavailable_dates),
            "5": ("Set PRN Status", self._handle_set_prn_status),
            "6": ("Set Late Shift Status", self._handle_set_late_shift_status),
            "7": ("Return to Main Menu", None),
        }

        self._run_menu_loop("Nurse Management", menu_options, exit_option="7")

    def manual_scheduling_menu(self) -> None:
        """Display the manual scheduling menu for pre-scheduled assignments."""
        menu_options = {
            "1": ("Add Pre-scheduled Assignment", self._handle_add_pre_scheduled_assignment),
            "2": ("Remove Pre-scheduled Assignment", self._handle_remove_pre_scheduled_assignment),
            "3": ("View Pre-scheduled Assignments", self._handle_view_pre_scheduled_assignments),
            "4": ("Return to Main Menu", None),
        }

        self._run_menu_loop("Manual Scheduling", menu_options, exit_option="4")

    def manage_weekend_history_menu(self) -> None:
        """Menu for managing weekend history entries."""
        menu_options = {
            "1": ("Add Weekend Assignment", self._handle_add_weekend_assignment),
            "2": ("Modify Weekend Assignment", self._handle_modify_weekend_assignment),
            "3": ("Delete Weekend Assignment", self._handle_delete_weekend_assignment),
            "4": ("Return to Main Menu", None),
        }

        self._run_menu_loop("Weekend History Management", menu_options, exit_option="4")

    def manage_assignment_history_menu(self) -> None:
        """Menu for managing assignment history entries."""
        menu_options = {
            "1": ("View Assignment History", self._handle_view_assignment_history),
            "2": ("Add Assignment to History", self._handle_add_assignment_history),
            "3": ("Modify Assignment in History", self._handle_modify_assignment_history),
            "4": ("Delete Assignment from History", self._handle_delete_assignment_history),
            "5": ("Return to Main Menu", None),
        }

        self._run_menu_loop("Assignment History Management", menu_options, exit_option="5")

    def advanced_weekend_stats_menu(self) -> None:
        """Menu for advanced weekend statistics."""
        menu_options = {
            "1": ("View Rotation Violation Counts", self._handle_view_violation_counts),
            "2": ("View Violation Dates", self._handle_view_violation_dates),
            "3": ("View Last Weekend Pattern", self._handle_view_last_patterns),
            "4": ("Set Violation Count (Manual Override)", self._handle_set_violation_count),
            "5": ("Set Last Pattern", self._handle_set_last_pattern),
            "6": (
                "Rebuild Violation History (Recompute from Weekend History)",
                self._handle_rebuild_violation_history,
            ),
            "7": ("Return to Main Menu", None),
        }

        self._run_menu_loop("Advanced Weekend Stats", menu_options, exit_option="7")

    def settings_menu(self) -> None:
        """Menu for settings saved to the settings file the GUI shares."""
        menu_options = {
            "1": ("Weekend Variants to Evaluate", self._handle_set_max_weekend_variants),
            "2": ("Return to Main Menu", None),
        }

        self._run_menu_loop("Settings", menu_options, exit_option="2")

    def _run_menu_loop(self, title: str, options: dict, exit_option: str, exit_message: str = None):
        """Generic menu loop handler."""
        while True:
            # Convert options to display format
            display_options = {k: v[0] for k, v in options.items()}
            choice = self._display_menu_and_get_choice(title, display_options)

            try:
                if choice == exit_option:
                    if exit_message:
                        print(f"\n{exit_message}")
                    break
                elif choice in options and options[choice][1]:
                    options[choice][1]()
                else:
                    print("Invalid choice. Please select a number from the menu.")
                    CLIHelper.pause()
            except Exception as e:
                logger.error(f"Error in {title} menu handling option {choice}: {e}")
                print(f"An error occurred: {e}")
                CLIHelper.pause()

    # ============================================================================
    # SETTINGS HANDLERS
    # ============================================================================

    def _handle_set_max_weekend_variants(self) -> None:
        """Change how many weekend variants survive pruning, and save it."""
        lo, hi = MAX_WEEKEND_VARIANTS_RANGE
        current = int(self.settings.get("max_weekend_variants"))
        print(
            f"Weekend variants to evaluate: {current:,}" + (" (unlimited)" if current == 0 else "")
        )
        print(
            f"Default: {DEFAULT_MAX_WEEKEND_VARIANTS:,}. "
            f"This machine evaluates {default_worker_count()} variants at a time."
        )
        print(
            "Each weekend combination kept gets a full weekday evaluation, so run time\n"
            "grows roughly in proportion to this number. Higher values explore more\n"
            "candidate schedules. 0 means unlimited.\n"
        )
        raw = input(f"New value ({lo}-{hi:,}, blank to keep): ").strip().replace(",", "")
        if not raw:
            print("Unchanged.")
            CLIHelper.pause()
            return
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            print(f"Invalid input. Please enter a whole number from {lo} to {hi:,}.")
            CLIHelper.pause()
            return

        value = int(raw)
        if value == 0 and not InputValidator.confirm_action(
            "Unlimited can run for hours on long horizons. Continue?", "n"
        ):
            print("Unchanged.")
            CLIHelper.pause()
            return

        self._safe_execute(
            "save settings",
            lambda: self.settings.update({"max_weekend_variants": value}),
            f"Weekend variants set to {value:,}; saved for future sessions.",
        )
        CLIHelper.pause()

    # ============================================================================
    # PRE-SCHEDULED ASSIGNMENT HANDLERS
    # ============================================================================

    def _handle_add_pre_scheduled_assignment(self) -> None:
        """Handle adding a pre-scheduled assignment."""
        date_str = input("Enter date (YYYY-MM-DD): ").strip()
        valid_date = InputValidator.validate_date(date_str)
        if not valid_date:
            CLIHelper.pause()
            return

        # Get main and backup nurses
        main = self._select_nurse_from_list("Available Nurses for Main nurse:", allow_none=True)
        if main is None:
            CLIHelper.pause()
            return

        backup = self._select_nurse_from_list("Available Nurses for Backup nurse:", allow_none=True)
        if backup is None:
            CLIHelper.pause()
            return

        note = input("\nNote (optional): ").strip()

        def add_assignment():
            self.pre_scheduler.add_assignment(date_str, main, backup, note)
            logger.info(
                f"Added pre-scheduled assignment for {date_str}: Main={main}, Backup={backup}"
            )
            return f"Assignment added for {date_str}."

        self._safe_execute("add pre-scheduled assignment", add_assignment)
        CLIHelper.pause()

    def _handle_view_pre_scheduled_assignments(self) -> None:
        """Handle viewing pre-scheduled assignments."""

        def view_assignments():
            assignments = self.pre_scheduler.get_assignments()
            if not assignments:
                print("No pre-scheduled assignments found.")
                return None

            headers = ["Date", "Main Nurse", "Backup Nurse", "Note"]
            CLIHelper.display_table(assignments, headers, "Pre-scheduled Assignments")
            return True

        self._safe_execute("view pre-scheduled assignments", view_assignments)
        CLIHelper.pause()

    def _handle_remove_pre_scheduled_assignment(self) -> None:
        """Handle removing a pre-scheduled assignment."""
        date_str = input("Enter date of assignment to remove (YYYY-MM-DD): ").strip()
        valid_date = InputValidator.validate_date(date_str)
        if not valid_date:
            CLIHelper.pause()
            return

        def remove_assignment():
            self.pre_scheduler.remove_assignment(date_str)
            logger.info(f"Removed pre-scheduled assignment for {date_str}")
            return f"Assignment removed for {date_str}."

        self._safe_execute("remove pre-scheduled assignment", remove_assignment)
        CLIHelper.pause()

    # ============================================================================
    # ASSIGNMENT HISTORY DETAILED HANDLERS
    # ============================================================================

    def _handle_view_assignment_history(self) -> None:
        """Handle viewing assignment history (robust date validation)."""
        print("\nView Assignment History\n")

        start_date_str = input("Enter start date (YYYY-MM-DD) or press Enter for all: ").strip()
        end_date_str = input("Enter end date (YYYY-MM-DD) or press Enter for all: ").strip()

        start_date = InputValidator.validate_date(start_date_str) if start_date_str else None
        if start_date_str and start_date is None:
            print("Error: Invalid start date.")
            CLIHelper.pause()
            return

        end_date = InputValidator.validate_date(end_date_str) if end_date_str else None
        if end_date_str and end_date is None:
            print("Error: Invalid end date.")
            CLIHelper.pause()
            return

        if start_date and end_date and end_date < start_date:
            print("Error: End date must be after start date.")
            CLIHelper.pause()
            return

        def view_history():
            assignments = self.assignment_history.get_history(start_date, end_date)
            if not assignments:
                print("No assignments found for the specified period.")
                return None

            data = []
            for assign_date, main, backup in assignments:
                date_obj = (
                    date.fromisoformat(assign_date) if isinstance(assign_date, str) else assign_date
                )
                weekday = calendar.day_name[date_obj.weekday()]
                data.append((assign_date, weekday, main, backup))

            headers = ["Date", "Day", "Main Nurse", "Backup Nurse"]
            CLIHelper.display_table(data, headers, "Assignment History")
            return True

        self._safe_execute("view assignment history", view_history)
        CLIHelper.pause()

    def _handle_add_assignment_history(self) -> None:
        """Handle adding an assignment to history."""
        print("\nAdd Assignment to History\n")

        # Get assignment date
        date_str = input("Enter date (YYYY-MM-DD): ").strip()
        assign_date = InputValidator.validate_date(date_str)
        if not assign_date:
            CLIHelper.pause()
            return

        # Get nurses
        main = self._select_nurse_from_list("Available Nurses for Main nurse:", allow_none=True)
        backup = self._select_nurse_from_list("Available Nurses for Backup nurse:", allow_none=True)

        if not main and not backup:
            print("Error: At least one nurse must be specified.")
            CLIHelper.pause()
            return

        def add_to_history():
            self.assignment_history.update_history(assign_date.isoformat(), main, backup)
            logger.info(
                f"Added assignment to history for {assign_date}: Main={main}, Backup={backup}"
            )
            return f"Assignment added to history for {date_str}."

        self._safe_execute("add assignment to history", add_to_history)
        CLIHelper.pause()

    def _handle_modify_assignment_history(self) -> None:
        """Handle modifying an assignment in history."""
        print("\nModify Assignment in History\n")

        # Get assignment date
        date_str = input("Enter date to modify (YYYY-MM-DD): ").strip()
        assign_date = InputValidator.validate_date(date_str)
        if not assign_date:
            CLIHelper.pause()
            return

        def modify_history():
            # Check if assignment exists
            assignments = self.assignment_history.get_history(assign_date, assign_date)
            if not assignments:
                print(f"No assignment found for {date_str}.")
                return None

            current = assignments[0]
            print(f"\nCurrent Assignment: Main={current[1]}, Backup={current[2]}")

            # Get new nurses
            print(f"\nCurrent Main: {current[1]}")
            main = self._select_nurse_from_list(
                "Select new Main nurse (or press Enter to keep current):", allow_none=True
            )
            if not main:
                main = current[1]

            print(f"\nCurrent Backup: {current[2]}")
            backup = self._select_nurse_from_list(
                "Select new Backup nurse (or press Enter to keep current):", allow_none=True
            )
            if not backup:
                backup = current[2]

            if main == current[1] and backup == current[2]:
                print("No changes were made.")
                return None

            self.assignment_history.update_history(assign_date.isoformat(), main, backup)
            logger.info(
                f"Modified assignment in history for {assign_date}: Main={main}, Backup={backup}"
            )
            return f"Assignment modified in history for {date_str}."

        self._safe_execute("modify assignment in history", modify_history)
        CLIHelper.pause()

    def _handle_delete_assignment_history(self) -> None:
        """Handle deleting an assignment from history."""
        date_str = input("Enter date to delete (YYYY-MM-DD): ").strip()
        assign_date = self._normalize_date(date_str)
        if assign_date is None:
            print("Invalid date entered. Please enter a valid date in YYYY-MM-DD format.")
            CLIHelper.pause()
            return

        def delete_from_history():
            assignments = self.assignment_history.get_history(assign_date, assign_date)
            if not assignments:
                print(f"No assignment found for {assign_date.date()}.")
                return None

            if not InputValidator.confirm_action(
                f"Are you sure you want to delete the assignment for {assign_date.date()}?"
            ):
                print("Operation cancelled.")
                return None

            self.assignment_history.delete_record(assign_date)
            logger.info(f"Deleted assignment from history for {assign_date.date()}")
            return f"Assignment for {assign_date.date()} has been deleted from history."

        self._safe_execute("delete assignment from history", delete_from_history)
        CLIHelper.pause()

    # ============================================================================
    # ADVANCED WEEKEND STATS HANDLERS
    # ============================================================================

    def _handle_set_last_pattern(self) -> None:
        """Handle setting a nurse's last weekend pattern."""
        nurse = self._get_nurse_name()
        if not nurse:
            CLIHelper.pause()
            return

        def set_pattern():
            current = self.weekend_history.get_last_pattern(nurse)
            print(f"Current last pattern for {nurse}: {current.value if current else 'None'}")
            pattern = input("Enter new pattern (FSF or SFS): ").strip().upper()
            if pattern not in ("FSF", "SFS"):
                print("Invalid pattern. Must be FSF or SFS.")
                return None

            self.weekend_history.set_last_pattern(nurse, WeekendPattern(pattern))
            return f"Last pattern updated for {nurse}."

        self._safe_execute("set last pattern", set_pattern)
        CLIHelper.pause()

    def _handle_view_last_patterns(self) -> None:
        """Handle viewing last weekend patterns for all nurses."""
        nurses = self.nurse_manager.get_nurses()
        data = []
        for nurse in nurses:
            pat = self.weekend_history.get_last_pattern(nurse)
            data.append((nurse, pat.value if pat else "None"))
        CLIHelper.display_table(data, ["Nurse", "Last Pattern"], "Last Weekend Pattern")
        CLIHelper.pause()

    def _handle_view_violation_counts(self) -> None:
        """Handle viewing rotation violation counts."""
        counts = self.weekend_history.get_violation_counts()
        data = [(n, c) for n, c in sorted(counts.items())]
        CLIHelper.display_table(data, ["Nurse", "Violation Count"], "Rotation Violation Counts")
        CLIHelper.pause()

    def _handle_set_violation_count(self) -> None:
        """Allow manually setting the rotation violation count for a nurse."""
        nurse = self._get_nurse_name("Enter nurse name to set violation count: ")
        if not nurse:
            CLIHelper.pause()
            return

        def set_count():
            current_count = self.weekend_history.get_violation_counts().get(nurse, 0)
            print(f"Current violation count for {nurse}: {current_count}")
            print("Manual edit mode: this value is a temporary override.")
            print("It stays as entered until you explicitly run a recompute/rebuild action.")
            new_count_str = input(f"Enter new violation count for {nurse}: ").strip()
            if not new_count_str.isdigit():
                print("Invalid input. Please enter a non-negative integer.")
                return None

            new_count = int(new_count_str)
            if new_count < 0:
                print("Violation count cannot be negative.")
                return None

            self.weekend_history.set_violation_count(nurse, new_count)
            logger.info(f"Set violation count for {nurse} to {new_count}")
            return (
                f"Violation count override for {nurse} set to {new_count}. "
                "Run 'Rebuild Violation History' to recompute from canonical weekend history."
            )

        self._safe_execute("set violation count", set_count)
        CLIHelper.pause()

    # ============================================================================
    # DISPLAY AND VIEW METHODS
    # ============================================================================

    def view_nurses(self) -> None:
        """Display the list of current nurses with their status."""

        def get_nurse_data():
            nurses = self.nurse_manager.get_nurses()
            if not nurses:
                print("No nurses in the system.")
                return None

            data = []
            for nurse in nurses:
                prn_status = "Yes" if self.nurse_manager.get_prn_status(nurse) else "No"
                late_shift = "Yes" if self.nurse_manager.get_late_shift_status(nurse) else "No"
                data.append((nurse, prn_status, late_shift))

            headers = ["Name", "PRN", "Late Shift"]
            CLIHelper.display_table(data, headers, "Current Nurses")
            return True

        self._safe_execute("view nurses", get_nurse_data)

    def view_weekend_history(self) -> None:
        """Display weekend assignment history."""

        def get_weekend_data():
            assignments = self.weekend_history.get_assignments()
            if not assignments:
                print("No weekend assignments in history.")
                return None

            data = []
            for weekend_start, fsf, sfs in assignments:
                weekend_date = (
                    weekend_start
                    if isinstance(weekend_start, date)
                    else date.fromisoformat(weekend_start)
                )
                weekend_end = weekend_date + timedelta(days=2)
                data.append((weekend_date.isoformat(), weekend_end.isoformat(), fsf, sfs))

            headers = ["Start Date", "End Date", "Friday Nurse", "Saturday Nurse"]
            CLIHelper.display_table(data, headers, "Weekend Assignment History")
            return True

        self._safe_execute("view weekend history", get_weekend_data)

    def _display_schedule(self, schedule) -> None:
        """Display detailed schedule information in a tabular format."""
        print("\n=== Schedule Details ===")
        if schedule.empty:
            print("No schedule data to display.")
            return

        data = []
        for sched_date, row in schedule.iterrows():
            date_str = pd.to_datetime(sched_date).date().isoformat()
            weekday = calendar.day_name[pd.to_datetime(sched_date).weekday()]
            main_nurse = row["main"] if not NurseScheduler.is_empty(row["main"]) else "NOT ASSIGNED"
            backup_nurse = (
                row["backup"] if not NurseScheduler.is_empty(row["backup"]) else "NOT ASSIGNED"
            )
            data.append((date_str, weekday, main_nurse, backup_nurse))

        headers = ["Date", "Day", "Main Nurse", "Backup Nurse"]
        CLIHelper.display_table(data, headers)

    # ============================================================================
    # SCHEDULE CREATION (Complex method preserved with minimal changes)
    # ============================================================================

    def create_schedule_menu(self) -> None:
        """Create a new schedule for a specified date range with rotation-violation control."""
        CLIHelper.clear_screen()
        print("=== Create New Schedule ===\n")

        if ASSIGNMENT_DEBUG_LOGGER.enabled:
            json_name = (
                ASSIGNMENT_DEBUG_LOGGER.json_path.name
                if ASSIGNMENT_DEBUG_LOGGER.json_path
                else "assignment_debug.jsonl"
            )
            csv_name = (
                ASSIGNMENT_DEBUG_LOGGER.csv_path.name
                if ASSIGNMENT_DEBUG_LOGGER.csv_path
                else "assignment_debug.csv"
            )
            print(f"Assignment diagnostics are being recorded to {json_name} and {csv_name}.\n")
        else:
            print(
                "Tip: set DEBUG_SCHED=1 before launching to write per-slot diagnostics to assignment_debug_<timestamp>.jsonl/.csv.\n"
            )

        try:
            # Basic checks & date range input
            if not self._validate_nurses_exist():
                return

            start_date, end_date = self._get_schedule_date_range()
            if not start_date or not end_date:
                return

            # Rotation-violation settings with summary
            nurses_allowed = self._handle_rotation_violation_settings()

            # Generate and display schedule options
            scheduler = self._create_scheduler(start_date, end_date)
            issues = scheduler.validate_pre_schedule()
            if issues:
                print("\nSome pinned (pre-scheduled) cells in this range look wrong:")
                for issue in issues:
                    print(f"  • {issue.message}")
                if not InputValidator.confirm_action("Generate anyway?", "n"):
                    CLIHelper.pause()
                    return
            if (scheduler.start_date, scheduler.end_date) != (
                scheduler.requested_start_date,
                scheduler.requested_end_date,
            ):
                print(
                    f"Scheduling {scheduler.start_date:%a %b %d} – "
                    f"{scheduler.end_date:%a %b %d, %Y} so no weekend is split."
                )
            replaced = recorded_weekends_in_range(
                self.weekend_history, scheduler.start_date, scheduler.end_date
            )
            if replaced:
                print(
                    f"{len(replaced)} weekend(s) already recorded in this range will be "
                    "ignored while generating and replaced if you save the new schedule."
                )
            top_schedules = self._generate_schedule_with_violations(scheduler, nurses_allowed)

            weekend_result = getattr(scheduler, "last_weekend_generation", None)
            if weekend_result is not None and weekend_result.pruned:
                print(search_capped_note(scheduler.config.max_weekend_variants))
            if not top_schedules:
                print("No valid schedules could be generated with the current constraints.")
                CLIHelper.pause()
                return

            pdfs = scheduler.export_top_variants_as_pdfs(top_schedules, len(top_schedules))
            print(f"Wrote {len(pdfs)} PDF file(s): {', '.join(pdfs)}")

            # Display candidates and get user selection
            selected_schedule = self._display_and_select_schedule(top_schedules)

            # Save if confirmed
            if InputValidator.confirm_action("Save this schedule and update weekend history?", "n"):
                self._save_selected_schedule(selected_schedule)
                print("Success: schedule saved and weekend history updated.")
            else:
                print("Not saved; history is unchanged.")

        except Exception as e:
            logger.error(f"Error generating schedule: {e}")
            print(f"Error generating schedule: {e}")
            traceback.print_exc()

        CLIHelper.pause()

    def _validate_nurses_exist(self) -> bool:
        """Check if nurses exist in the system."""
        nurses = self.nurse_manager.get_nurses()
        if not nurses:
            print("No nurses in the system! Please add nurses first.")
            CLIHelper.pause()
            return False
        return True

    def _get_schedule_date_range(self) -> tuple:
        """Get and validate schedule date range from user."""
        print("Enter schedule date range (format: YYYY-MM-DD)")
        start_date_str = input("Start date: ").strip()
        start_date = InputValidator.validate_date(start_date_str)
        if not start_date:
            CLIHelper.pause()
            return None, None

        end_date_str = input("End date: ").strip()
        end_date = InputValidator.validate_date(end_date_str)
        if not end_date:
            CLIHelper.pause()
            return None, None

        if end_date < start_date:
            print("Error: End date must be after start date.")
            CLIHelper.pause()
            return None, None

        return start_date, end_date

    def _handle_rotation_violation_settings(self) -> list:
        """Handle rotation violation settings and return allowed nurses."""
        print("\n=== Rotation Violation Summary ===")
        summary = self.weekend_history.get_violation_summary()
        summary = summary.sort_values(
            by=["total_viol", "consec_viol", "clean_run_weeks", "days_since_last"],
            ascending=[True, True, False, False],
        ).reset_index(drop=True)

        # Display table
        CLIHelper.display_table(
            summary[
                [
                    "nurse",
                    "total_viol",
                    "last_violation_date",
                    "consec_viol",
                    "clean_run_weeks",
                    "days_since_last",
                ]
            ].values.tolist(),
            ["Nurse", "#Viol", "Last Viol", "Viol-Streak", "Clean Runs", "Days Since Last"],
            "Rotation Violation Summary",
        )

        print(
            "\nWe recommend allowing violations for nurses at the top of this list (least violated, longest clean run)."
        )
        print("Enter comma-separated numbers to allow, or leave blank for none, or 'all' for all.")

        for i, row in summary.iterrows():
            print(
                f"{i + 1}. {row['nurse']} (Viol: {row['total_viol']}, Streak: {row['consec_viol']}, Clean: {row['clean_run_weeks']}, Days: {row['days_since_last']})"
            )

        allow = InputValidator.confirm_action(
            "Allow any rotation violations for this schedule run?", "n"
        )

        if allow:
            sel = input("Enter numbers (comma-separated), or 'all' for all: ").strip()
            if sel.lower() == "all":
                return summary["nurse"].tolist()
            elif sel:
                idxs = [
                    int(x) - 1
                    for x in sel.split(",")
                    if x.strip().isdigit() and 0 < int(x) <= len(summary)
                ]
                return [summary.iloc[i]["nurse"] for i in idxs]

        return []

    def _create_scheduler(self, start_date, end_date):
        """Create and configure the nurse scheduler from the shared GUI settings."""
        return build_scheduler_from_settings(
            start_date,
            end_date,
            self.nurse_manager,
            self.weekend_history,
            self.pre_scheduler,
            self.settings,
        )

    def _generate_schedule_with_violations(self, scheduler, nurses_allowed):
        """Generate schedule with violation settings."""
        allow_rotation_violations = bool(nurses_allowed)
        mode = (
            scheduler.WeekendVariantMode.RELAXED_ALLOWED
            if allow_rotation_violations
            else scheduler.WeekendVariantMode.STRICT_ONLY
        )

        scheduler.set_allow_rotation_violations(allow_rotation_violations)
        scheduler.set_nurses_allowed_rotation_violation(nurses_allowed)
        try:
            return scheduler.generate_schedule(top_n=5, weekend_variant_mode=mode)
        finally:
            scheduler.set_allow_rotation_violations(False)
            scheduler.set_nurses_allowed_rotation_violation([])

    def _display_and_select_schedule(self, top_schedules):
        """Display schedule candidates and get user selection (1-based and clear)."""
        print(
            "\nTop candidate schedules (ranked by fewest rotation repeats, "
            "then fewest unfilled slots, then score):"
        )
        for rank, candidate in enumerate(top_schedules, start=1):
            idx, stats, nurse_counts, sched = candidate
            print(
                f"\nCandidate {rank}: Rotation repeats={stats['rotation_rep']}, "
                f"Unfilled slots={stats['gaps']} ({stats.get('unfillable', 0)} impossible), "
                f"Score={stats.get('weighted_score', 0.0):.3f}, "
                f"Balance Main={stats['balance_main']}, "
                f"Balance Backup={stats['balance_backup']}"
            )

            print("\nNurse Assignment Counts:")
            print(f"{'Nurse':<20}{'Main':<10}{'Backup':<10}{'Total':<10}")
            print("-" * 50)
            for nurse, cnt in nurse_counts.items():
                print(f"{nurse:<20}{cnt['main']:<10}{cnt['backup']:<10}{cnt['total']:<10}")
            self._display_schedule(sched)
            print("-" * 50)

        # Expect 1..N
        sel = InputValidator.get_integer_input(
            f"Enter the candidate number to select (1-{len(top_schedules)}): ",
            valid_range=range(1, len(top_schedules) + 1),
        )
        sel_idx = sel - 1
        final_sched = top_schedules[sel_idx][3]

        print("\nFinal Selected Schedule:")
        self._display_schedule(final_sched)

        return final_sched

    def _save_selected_schedule(self, schedule) -> None:
        """Record the chosen schedule in assignment and weekend history.

        One transaction through :func:`scheduler.apply_schedule`, the same path
        the GUI uses, so a failure leaves history unchanged.
        """
        report = apply_schedule(self.nurse_manager.db_name, schedule)
        self.weekend_history.reload()
        self.assignment_history.reload()
        print(
            f"Recorded {report.days_recorded} days and "
            f"{report.weekends_recorded} weekend rotations."
        )
        if report.days_cleared or report.weekends_removed:
            print(
                f"Removed {report.days_cleared} earlier day records and "
                f"{report.weekends_removed} earlier weekend rotations this schedule replaces."
            )


def main():
    """Main entry point for the Nurse Scheduler application."""
    # Check if we're in an interactive environment first
    if not sys.stdin.isatty():
        print("=" * 60)
        print("ERROR: Non-interactive environment detected!")
        print("=" * 60)
        print("\nThis program requires an interactive terminal to function.")
        print("\nYou appear to be running this on Android. Try one of these solutions:")
        print("1. Use Termux with Python installed")
        print("2. Use Pydroid 3's terminal (not the run button)")
        print("3. Use a Jupyter notebook with input() support")
        print("4. Connect via SSH and run from a proper terminal")
        print("\nThe program cannot continue without interactive input support.")
        print("=" * 60)
        sys.exit(1)

    try:
        print("Initializing Nurse Scheduler System...")
        scheduler_ui = NurseSchedulerUI()
        scheduler_ui.main_menu()
    except EOFError:
        print("\n\nEOF Error: The program cannot read input from the terminal.")
        print("Please ensure you're running this in a proper interactive terminal.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nProgram interrupted by user. Goodbye!")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Critical error in main: {e}")
        print(f"A critical error occurred: {e}")
        print("Please check the log file for details.")
        traceback.print_exc()

        # Try to pause, but handle EOFError
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            print("\nExiting due to non-interactive environment...")
        except KeyboardInterrupt:
            print("\nExiting...")


if __name__ == "__main__":
    main()
