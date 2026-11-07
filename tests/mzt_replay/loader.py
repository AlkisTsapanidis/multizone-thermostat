"""Load production modules by file path, without importing the Home Assistant package __init__."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "multizone_thermostat"


def load_prod_module(name: str) -> ModuleType:
    """Load `<package>/<name>.py` as an isolated module. Only valid for modules with no relative imports."""
    key = f"_mzt_prod_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, PACKAGE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def load_reference_module(name: str) -> ModuleType:
    """Load a frozen pre-fix copy from tests/mzt_replay/reference/<name>.py (never edited)."""
    key = f"_mzt_ref_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, Path(__file__).resolve().parent / "reference" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module