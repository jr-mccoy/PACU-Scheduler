import re
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

THEME_PATH = Path(__file__).resolve().parents[1] / "ui" / "theme.py"
_SPEC = spec_from_file_location("ui_theme_for_tests", THEME_PATH)
_MODULE = module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)
shade_color = _MODULE.shade_color


def test_shade_color_normalizes_to_uppercase_hex():
    value = shade_color("#ff88aa", 1.0)
    assert re.fullmatch(r"#[0-9A-F]{6}", value)
    assert value == "#FF88AA"


def test_shade_color_applies_darkening_factor():
    assert shade_color("#808080", 0.5) == "#404040"


def test_shade_color_returns_input_for_invalid_hex():
    assert shade_color("not-a-color", 0.8) == "not-a-color"
    assert shade_color("#GG0000", 0.8) == "#GG0000"
