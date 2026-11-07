"""Overshoot must occur while the current target is active, not because it was lowered."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.multizone_thermostat.const import make_zone_entity_id
from custom_components.multizone_thermostat.plant_diagnostics.const import (
    ANOMALY_NONE,
    ANOMALY_OVERSHOOT,
)
from custom_components.multizone_thermostat.plant_diagnostics.engine import PlantDiagnosticsEngine


@pytest.fixture
def plant(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("custom_components.multizone_thermostat.plant_diagnostics.anomalies.time.monotonic",
                        lambda: clock[0])
    states = {}
    hass = MagicMock()
    hass.states.get.side_effect = states.get
    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.zones = [{"name": "Living"}, {"name": "Bedroom"}]
    coordinator.zone_manager.get_zone.return_value = {}
    coordinator.zone_manager.is_passive_heat_allowed.return_value = False
    coordinator.get_zone_demand.return_value = 50.0
    coordinator.get_zone_mode.return_value = "primary"
    coordinator.get_master_state.return_value = True
    engine = PlantDiagnosticsEngine(coordinator)
    engine._current_boiler_start = clock[0]

    def evaluate(zone, temperature, target, *, heating=True):
        climate_id = make_zone_entity_id(zone)
        states[climate_id] = SimpleNamespace(attributes={
            "current_temperature": temperature, "temperature": target,
        })
        engine._current_boiler_start = clock[0] if heating else None
        clock[0] += 60
        return engine.evaluate_zone_anomalies(climate_id)[0]

    return evaluate, engine, states, coordinator, clock


@pytest.mark.parametrize("new_target", [18.0, 17.0, 16.0])
def test_setback_does_not_report_preexisting_heat_as_overshoot(plant, new_target):
    evaluate, _, _, _, _ = plant
    assert evaluate("Living", 20.0, 21.0) == ANOMALY_NONE
    for temp in (20.1, 20.1, 20.0, 19.9):
        assert evaluate("Living", temp, new_target, heating=False) == ANOMALY_NONE


def test_genuine_overshoot_after_setback_cools_and_rises(plant):
    evaluate, _, _, _, _ = plant
    assert evaluate("Living", 20.0, 21.0) == ANOMALY_NONE
    assert evaluate("Living", 20.0, 18.0, heating=False) == ANOMALY_NONE
    assert evaluate("Living", 19.5, 18.0, heating=False) == ANOMALY_NONE
    assert evaluate("Living", 19.6, 18.0, heating=False) == ANOMALY_OVERSHOOT
    assert evaluate("Living", 19.7, 18.0, heating=False) == ANOMALY_OVERSHOOT
    assert evaluate("Living", 19.5, 18.0, heating=False) == ANOMALY_NONE


def test_heating_under_unchanged_target_detects_threshold_crossing(plant):
    evaluate, _, _, _, _ = plant
    assert evaluate("Living", 20.0, 21.0) == ANOMALY_NONE
    assert evaluate("Living", 22.5, 21.0) == ANOMALY_NONE
    assert evaluate("Living", 22.6, 21.0, heating=False) == ANOMALY_OVERSHOOT


def test_initial_above_threshold_is_unknown_not_overshoot(plant):
    evaluate, _, _, _, _ = plant
    assert evaluate("Living", 22.0, 20.0) == ANOMALY_NONE
    assert evaluate("Living", 22.1, 20.0) == ANOMALY_NONE
    assert evaluate("Living", 21.5, 20.0) == ANOMALY_NONE
    assert evaluate("Living", 21.6, 20.0) == ANOMALY_OVERSHOOT


def test_recent_heating_requirement_remains(plant):
    evaluate, _, _, _, clock = plant
    assert evaluate("Living", 20.0, 20.0) == ANOMALY_NONE
    clock[0] += 7200
    assert evaluate("Living", 21.6, 20.0, heating=False) == ANOMALY_NONE


def test_target_tracking_is_per_zone(plant):
    evaluate, _, _, _, _ = plant
    assert evaluate("Living", 20.0, 21.0) == ANOMALY_NONE
    assert evaluate("Bedroom", 20.0, 21.0) == ANOMALY_NONE
    assert evaluate("Living", 20.0, 18.0, heating=False) == ANOMALY_NONE
    assert evaluate("Bedroom", 22.6, 21.0, heating=False) == ANOMALY_OVERSHOOT
    assert evaluate("Living", 20.1, 18.0, heating=False) == ANOMALY_NONE


@pytest.mark.parametrize("missing", ["target", "temperature", "state", "invalid_temperature"])
def test_missing_data_invalidates_previous_target_history(plant, missing):
    evaluate, engine, states, _, _ = plant
    climate_id = make_zone_entity_id("Living")
    assert evaluate("Living", 20.0, 20.0) == ANOMALY_NONE
    if missing == "state":
        del states[climate_id]
        engine.evaluate_zone_anomalies(climate_id)
    else:
        evaluate("Living", None if missing == "temperature" else
                 "unavailable" if missing == "invalid_temperature" else 20.0,
                 None if missing == "target" else 20.0)
    assert evaluate("Living", 21.6, 20.0) == ANOMALY_NONE


def test_bypass_invalidates_previous_target_history(plant):
    evaluate, _, _, coordinator, _ = plant
    assert evaluate("Living", 20.0, 20.0) == ANOMALY_NONE
    coordinator.get_zone_mode.return_value = "bypass"
    assert evaluate("Living", 20.0, 20.0) == ANOMALY_NONE
    coordinator.get_zone_mode.return_value = "primary"
    assert evaluate("Living", 21.6, 20.0) == ANOMALY_NONE


def test_target_change_is_tracked_even_if_sensor_fault_takes_priority(plant):
    evaluate, _, _, _, _ = plant
    assert evaluate("Living", 20.0, 21.0) == ANOMALY_NONE
    assert evaluate("Living", 65.0, 18.0) == "sensor_fault"
    assert evaluate("Living", 20.0, 18.0) == ANOMALY_NONE
