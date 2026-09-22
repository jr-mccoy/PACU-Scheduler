"""Behaviour tests for the UI/UX fixes: keyboard handling, layout, validation."""

from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QDate, QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QDialogButtonBox, QLineEdit, QVBoxLayout  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _key(widget, key):
    widget.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))


def _tool_dialog():
    from ui.dialogs.tool_dialog import ToolDialog

    dlg = ToolDialog(None, "Test")
    layout = QVBoxLayout()
    layout.addWidget(QLineEdit())
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    dlg.setLayout(layout)
    events = []
    dlg.accepted.connect(lambda: events.append("accepted"))
    dlg.rejected.connect(lambda: events.append("rejected"))
    return dlg, events


# ─────────────────────────── dialogs ───────────────────────────
def test_escape_rejects_tool_dialog(qapp):
    dlg, events = _tool_dialog()
    dlg.open()
    _key(dlg, Qt.Key_Escape)
    assert events == ["rejected"]
    assert not dlg.isVisible()


def test_enter_presses_the_accept_button(qapp):
    dlg, events = _tool_dialog()
    dlg.open()
    _key(dlg, Qt.Key_Return)
    assert events == ["accepted"]


def test_closing_after_accept_does_not_also_reject(qapp):
    dlg, events = _tool_dialog()
    dlg.open()
    dlg.accept()
    dlg.close()
    assert events == ["accepted"]


def test_window_close_counts_as_cancel(qapp):
    dlg, events = _tool_dialog()
    dlg.open()
    dlg.close()
    assert events == ["rejected"]


def test_destructive_confirm_defaults_to_cancel(qapp):
    from ui.messages import confirm

    calls = []
    dlg = confirm(
        None,
        "Remove",
        "Remove it?",
        yes_cb=lambda: calls.append("yes"),
        yes_text="Remove",
        destructive=True,
    )
    assert dlg.default_button().text() == "Cancel"
    _key(dlg, Qt.Key_Return)
    assert calls == []


def test_message_dialog_grows_with_long_text(qapp):
    from ui.messages import show_info

    short = show_info(None, "Short", "Done.")
    long = show_info(None, "Long", "A much longer message. " * 30)
    assert long.height() > short.height()
    short.close()
    long.close()


def test_error_details_are_hidden_until_asked_for(qapp):
    from PySide6.QtWidgets import QPlainTextEdit

    from ui.messages import show_error

    dlg = show_error(None, "Failed", "It failed.", details="Traceback: boom")
    details = dlg.findChild(QPlainTextEdit)
    assert details is not None and not details.isVisible()
    dlg.close()


# ─────────────────────────── widgets ───────────────────────────
def test_date_pickers_start_the_week_on_sunday(qapp):
    from ui.widgets.date_pickers import MultiDatePicker, SingleDatePicker

    single = SingleDatePicker()
    multi = MultiDatePicker()
    assert single.cal.firstDayOfWeek() == Qt.Sunday
    assert multi.cal.firstDayOfWeek() == Qt.Sunday
    assert single.dow_labels[0].text() == "Sun"


def test_nav_arrows_render_without_image_files(qapp, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no arrowL.png / arrowR.png here
    from ui.widgets.date_pickers import SingleDatePicker

    picker = SingleDatePicker()
    for btn in (picker.prev_btn, picker.next_btn):
        assert btn.text() or not btn.icon().isNull()
        assert btn.accessibleName()


def test_single_picker_reports_keyboard_changes(qapp):
    from ui.widgets.date_pickers import SingleDatePicker

    picker = SingleDatePicker(initial=QDate(2026, 9, 22))
    seen = []
    picker.dateChanged.connect(seen.append)
    picker.cal.setSelectedDate(QDate(2026, 9, 25))  # what arrow keys do
    assert picker.iso() == "2026-09-25"
    assert seen == [QDate(2026, 9, 25)]


def test_calendar_grid_setting_toggles_grid(qapp):
    from ui.widgets.date_pickers import SingleDatePicker

    picker = SingleDatePicker()
    picker.set_theme("dark", "#5C8DBC", grid=False)
    assert picker.cal.isGridVisible() is False
    picker.set_theme("dark", "#5C8DBC", grid=True)
    assert picker.cal.isGridVisible() is True


def test_empty_state_filter_tracks_rows(qapp):
    from PySide6.QtWidgets import QListWidget

    from ui.widgets.common import install_empty_state

    lw = QListWidget()
    hint = install_empty_state(lw, "Nothing here")
    assert hint._is_empty()
    lw.addItem("x")
    assert not hint._is_empty()
    lw.item(0).setHidden(True)
    assert hint._is_empty()


# ─────────────────────────── validation & helpers ───────────────────────────
def test_assignment_validation():
    from ui.dialogs.assignment_dialog import validate_assignment

    assert validate_assignment("", "") is not None
    assert validate_assignment("Avery", "Avery") is not None
    assert validate_assignment("Avery", "") is None
    assert validate_assignment("Avery", "Blair") is None


def test_nurse_combo_keeps_a_departed_nurse(qapp):
    from ui.dialogs.assignment_dialog import nurse_combo

    cb = nurse_combo(["Avery", "Blair"], "Former Nurse")
    assert cb.currentData() == "Former Nurse"


def test_describe_range():
    from ui.screens.schedule_generation import describe_range

    text = describe_range(date(2026, 9, 21), date(2026, 10, 18))
    assert "28 days" in text
    assert "4 weekends" in text
    assert describe_range(date(2026, 9, 21), date(2026, 9, 20)) == ""


def test_describe_range_explains_a_widened_range():
    from ui.screens.schedule_generation import describe_range

    text = describe_range(
        date(2026, 10, 3), date(2026, 10, 30), (date(2026, 10, 2), date(2026, 11, 1))
    )
    assert "28 days" in text
    assert "5 weekends" in text
    assert "Scheduling Fri Oct 02 – Sun Nov 01, 2026 so no weekend is split." in text
    assert "already recorded" not in text


def test_describe_range_warns_about_replaced_weekends():
    from ui.screens.schedule_generation import describe_range

    text = describe_range(date(2026, 11, 2), date(2026, 11, 29), replaces=2)
    assert "2 weekends are already recorded in this range" in text


def test_stats_items_sort_numerically(qapp):
    from PySide6.QtWidgets import QTableWidget

    from ui.screens.advanced_weekend_stats import _item

    table = QTableWidget(3, 1)
    for row, value in enumerate([10, 9, 100]):
        table.setItem(row, 0, _item(str(value), value))
    table.sortItems(0, Qt.AscendingOrder)
    assert [table.item(r, 0).text() for r in range(3)] == ["9", "10", "100"]


# ─────────────────────────── settings dialogs ───────────────────────────
@pytest.mark.parametrize("dialog_name", ["SettingsDialog", "CompactSettingsDialog"])
def test_settings_dialogs_include_scoring_weights(home, qapp, dialog_name):
    import ui.dialogs as dialogs
    from ui.settings import AppSettings

    dialog = getattr(dialogs, dialog_name)(AppSettings())
    weights = dialog.values()["scoring_weights"]
    assert set(weights) == set(AppSettings.DEFAULTS["scoring_weights"])


@pytest.mark.parametrize("dialog_name", ["SettingsDialog", "CompactSettingsDialog"])
def test_restore_defaults_resets_the_form(home, qapp, dialog_name):
    import ui.dialogs as dialogs
    from ui.dialogs.settings_support import load_values
    from ui.settings import AppSettings

    dialog = getattr(dialogs, dialog_name)(AppSettings())
    dialog.variant_cap.setValue(4321)
    dialog.one_day_gap.setChecked(True)
    load_values(dialog, AppSettings.DEFAULTS)
    values = dialog.values()
    assert values["max_weekend_variants"] == AppSettings.DEFAULTS["max_weekend_variants"]
    assert values["allow_one_day_weekday_gap"] is False


def test_invalid_accent_blocks_save(home, qapp):
    from ui.dialogs import SettingsDialog
    from ui.dialogs.settings_support import validate_and_accept
    from ui.settings import AppSettings

    dialog = SettingsDialog(AppSettings())
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    dialog.accent_edit.setText("blue")
    validate_and_accept(dialog)
    assert accepted == []
    dialog.accent_edit.setText("#123abc")
    validate_and_accept(dialog)
    assert accepted == [True]
    assert dialog.values()["accent_color"] == "#123ABC"


# ─────────────────────────── main window ───────────────────────────
@pytest.fixture
def app_window(home, qapp, tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)  # fresh database, no GIF.gif
    from ui.app_shell import App

    window = App()
    window.resize(1200, 800)
    yield window
    window.close()


def test_main_menu_hides_missing_gif(app_window):
    assert app_window.main.gif_lbl.isHidden()


def test_main_menu_promotes_generate(app_window):
    buttons = app_window.main.buttons
    assert buttons["generate"].property("role") == "special"
    assert buttons["quit"].property("role") in (None, "")


def test_generate_disabled_when_end_before_start(app_window):
    screen = app_window.generate
    assert screen.gen_btn.isEnabled()
    screen._end_cal.set_date(screen._start_cal.qdate().addDays(-1))
    assert not screen.gen_btn.isEnabled()
    assert "before the start date" in screen.summary.text()


def test_screens_refresh_on_entry(app_window):
    manage = app_window.manage
    other = type(manage.nm)(manage.nm.db_name)
    other.add_nurse("Quinn Example")
    app_window.switch_frame("manage")
    names = [manage.list.item(i).data(Qt.UserRole) for i in range(manage.list.count())]
    assert "Quinn Example" in names


def test_nurse_actions_need_a_selection(app_window):
    manage = app_window.manage
    manage.nm.add_nurse("Avery Brooks")
    manage.refresh()
    assert not manage._btn_remove.isEnabled()
    manage.list.setCurrentRow(0)
    assert manage._btn_remove.isEnabled()


def test_every_screen_has_a_title(app_window):
    from PySide6.QtWidgets import QLabel

    from ui.style import UiStyle

    for name, _cls in app_window._pages:
        if name == "main":
            continue
        page = getattr(app_window, name)
        titles = [
            lbl
            for lbl in page.findChildren(QLabel)
            if lbl.font().pointSize() == UiStyle.TITLE_FONT.pointSize() and lbl.font().bold()
        ]
        assert titles, f"{name} has no screen title"


def test_assignment_table_keeps_selection_after_refresh(app_window):
    screen = app_window.prescheduled
    screen.ps.add_assignment("2026-09-22", None, None, "a")
    screen.ps.add_assignment("2026-09-25", None, None, "b")
    screen.refresh(select="2026-09-25")
    assert screen.selected_iso() == "2026-09-25"
    screen.refresh()
    assert screen.selected_iso() == "2026-09-25"


def test_back_button_is_neutral(app_window):
    from PySide6.QtWidgets import QPushButton

    for name, _cls in app_window._pages:
        page = getattr(app_window, name)
        for btn in page.findChildren(QPushButton):
            if btn.text() == "Back":
                assert btn.property("role") in (None, ""), name
