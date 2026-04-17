"""Import contracts for deprecated compatibility module names."""

from importlib import import_module, reload
import warnings

import scheduler


def _reload_with_deprecation_capture(module_name: str):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        module = import_module(module_name)
        module = reload(module)
    return module, [w for w in caught if issubclass(w.category, DeprecationWarning)]


def test_backend_legacy_modules_emit_deprecation_and_match_scheduler_contract():
    for module_name in ("NCSSQL55", "NCSSQL57"):
        module, deprecations = _reload_with_deprecation_capture(module_name)

        assert module.__all__ == scheduler.__all__
        assert deprecations, f"{module_name} did not emit a DeprecationWarning"
        assert any("Import from scheduler instead." in str(w.message) for w in deprecations)

        public_names = [name for name in module.__dict__ if not name.startswith("_")]
        assert set(public_names) == set(module.__all__)


def test_gui_legacy_module_has_limited_api_contract_and_deprecation_warning():
    module, deprecations = _reload_with_deprecation_capture("NCSSQLGUIFINALIST57")

    assert module.__all__ == ["App", "UiStyle"]
    assert deprecations, "NCSSQLGUIFINALIST57 did not emit a DeprecationWarning"
    assert any("Import from ui instead." in str(w.message) for w in deprecations)

    public_names = [name for name in module.__dict__ if not name.startswith("_")]
    assert set(public_names) == set(module.__all__)
