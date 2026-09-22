"""The weekend-variant cap is a persisted setting editable from the GUI and CLI."""

from __future__ import annotations

import json

import pytest

import cli.nurse_scheduler_ui as cli_ui
from scheduler import (
    DEFAULT_MAX_WEEKEND_VARIANTS,
    SharedSettings,
    build_scheduler_config_from_settings,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the settings file at a throwaway home directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _settings_file(home):
    return home / ".nurse_scheduler" / "settings.json"


def test_default_applies_without_a_settings_file(home):
    settings = SharedSettings()
    assert settings.get("max_weekend_variants") == DEFAULT_MAX_WEEKEND_VARIANTS
    config = build_scheduler_config_from_settings(settings)
    assert config.max_weekend_variants == DEFAULT_MAX_WEEKEND_VARIANTS


def test_update_persists_across_sessions(home):
    SharedSettings().update({"max_weekend_variants": 2500})

    later = SharedSettings()
    assert later.get("max_weekend_variants") == 2500
    assert build_scheduler_config_from_settings(later).max_weekend_variants == 2500


def test_update_keeps_other_keys_and_does_not_pin_defaults(home):
    path = _settings_file(home)
    path.parent.mkdir()
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

    SharedSettings().update({"max_weekend_variants": 750})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "theme": "dark",
        "max_weekend_variants": 750,
    }


def test_update_does_not_clobber_changes_saved_since_load(home):
    settings = SharedSettings()
    path = _settings_file(home)
    path.parent.mkdir()
    path.write_text(json.dumps({"weekend_gap_days": 21}), encoding="utf-8")

    settings.update({"max_weekend_variants": 750})

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["weekend_gap_days"] == 21
    assert settings.get("weekend_gap_days") == 21


# --- CLI ---------------------------------------------------------------------


@pytest.fixture
def cli(home, monkeypatch):
    monkeypatch.setattr(cli_ui.CLIHelper, "pause", staticmethod(lambda *a, **k: None))
    ui = cli_ui.NurseSchedulerUI.__new__(cli_ui.NurseSchedulerUI)
    ui.settings = SharedSettings()
    return ui


def _answer(monkeypatch, *responses):
    replies = iter(responses)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))


def test_cli_saves_new_value(cli, monkeypatch):
    _answer(monkeypatch, "3,000")
    cli._handle_set_max_weekend_variants()
    assert SharedSettings().get("max_weekend_variants") == 3000


@pytest.mark.parametrize("reply", ["", "lots", "-5", "1000000"])
def test_cli_leaves_value_unchanged_on_blank_or_invalid_input(cli, monkeypatch, reply):
    _answer(monkeypatch, reply)
    cli._handle_set_max_weekend_variants()
    assert SharedSettings().get("max_weekend_variants") == DEFAULT_MAX_WEEKEND_VARIANTS


def test_cli_unlimited_requires_confirmation(cli, monkeypatch):
    _answer(monkeypatch, "0", "n")
    cli._handle_set_max_weekend_variants()
    assert SharedSettings().get("max_weekend_variants") == DEFAULT_MAX_WEEKEND_VARIANTS

    _answer(monkeypatch, "0", "y")
    cli._handle_set_max_weekend_variants()
    assert SharedSettings().get("max_weekend_variants") == 0


# --- GUI ---------------------------------------------------------------------


@pytest.fixture
def qapp():
    widgets = pytest.importorskip("PySide6.QtWidgets")
    return widgets.QApplication.instance() or widgets.QApplication([])


@pytest.mark.parametrize("dialog_name", ["SettingsDialog", "CompactSettingsDialog"])
def test_gui_dialogs_edit_and_save_the_cap(home, qapp, dialog_name):
    import ui.dialogs as dialogs
    from ui.settings import AppSettings

    settings = AppSettings()
    dialog = getattr(dialogs, dialog_name)(settings)
    assert dialog.variant_cap.value() == DEFAULT_MAX_WEEKEND_VARIANTS

    dialog.variant_cap.setValue(4000)
    settings.bulk_set(dialog.values())

    assert SharedSettings().get("max_weekend_variants") == 4000
    assert getattr(dialogs, dialog_name)(AppSettings()).variant_cap.value() == 4000


@pytest.mark.parametrize("dialog_name", ["SettingsDialog", "CompactSettingsDialog"])
def test_gui_dialogs_preserve_unlimited(home, qapp, dialog_name):
    import ui.dialogs as dialogs
    from ui.settings import AppSettings

    settings = AppSettings()
    settings.set("max_weekend_variants", 0)
    dialog = getattr(dialogs, dialog_name)(settings)

    assert dialog.variant_cap.text() == "Unlimited"
    assert dialog.values()["max_weekend_variants"] == 0
