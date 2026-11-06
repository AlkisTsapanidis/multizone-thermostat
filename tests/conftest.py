"""Pytest configuration for multizone_thermostat tests."""
import sys
import os

dummy_ha_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "dummy_ha"))
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

if dummy_ha_path not in sys.path:
    sys.path.insert(0, dummy_ha_path)
if repo_root not in sys.path:
    sys.path.insert(1, repo_root)

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--show-red", action="store_true", default=False,
        help="Run @pytest.mark.known_defect tests as ordinary tests so they fail visibly.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "known_defect(id, reason): test asserts correct behaviour that production code is known NOT to "
        "have yet. Reported as strict xfail by default (fixing the code makes it XPASS=fail, forcing "
        "removal of the marker); run with --show-red to see the real failures.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--show-red"):
        return
    for item in items:
        marker = item.get_closest_marker("known_defect")
        if marker is None:
            continue
        defect_id = marker.args[0] if marker.args else "?"
        reason = marker.args[1] if len(marker.args) > 1 else marker.kwargs.get("reason", "")
        item.add_marker(pytest.mark.xfail(strict=True, reason=f"[{defect_id}] {reason}"))
