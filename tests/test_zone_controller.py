"""Unit tests for ZoneController orchestrator."""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio

from custom_components.multizone_thermostat.zone_controller import ZoneController
from custom_components.multizone_thermostat.const import (
    CONF_ZONE_NAME,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    ZONE_MODE_PRIMARY,
    ZONE_MODE_BYPASS,
    KEY_WEATHER_CURVE,
    make_zone_entity_id,
)


class DummyState:
    def __init__(self, state, current_temp=None, target_temp=None, domain="sensor"):
        self.state = state
        self.attributes = {}
        if current_temp is not None:
            self.attributes["current_temperature"] = current_temp
        if target_temp is not None:
            self.attributes["temperature"] = target_temp
        self.domain = domain


def create_mock_coordinator(zones=None):
    if zones is None:
        zones = [
            {CONF_ZONE_NAME: "Living Room"},
            {CONF_ZONE_NAME: "Bedroom"},
        ]
    coordinator = MagicMock()
    coordinator.zones = zones
    coordinator.weather_sensor_id = None
    coordinator.is_dhw_active = False
    zone_demands = {}
    def get_demand(cid):
        return zone_demands.get(cid, 0.0)
    def set_demand(cid, val):
        zone_demands[cid] = val

    coordinator.zone_manager = MagicMock()
    coordinator.zone_manager.get_zone_mode = MagicMock(return_value=ZONE_MODE_PRIMARY)
    coordinator.get_zone_mode = coordinator.zone_manager.get_zone_mode
    
    coordinator.demand_aggregator = MagicMock()
    coordinator.demand_aggregator.get_zone_demand = MagicMock(side_effect=get_demand)
    coordinator.demand_aggregator.set_zone_demand = MagicMock(side_effect=set_demand)
    coordinator.get_zone_demand = coordinator.demand_aggregator.get_zone_demand
    coordinator.set_zone_demand = coordinator.demand_aggregator.set_zone_demand

    coordinator.get_persistent_data = MagicMock(return_value=0.0)
    coordinator.debounced_save_thermal_states = MagicMock()
    coordinator.storage = MagicMock()
    coordinator.storage.async_save_autotuner_states = AsyncMock()
    coordinator.storage.async_save_thermal_states = AsyncMock()
    coordinator.storage.async_debounced_save_thermal_states = MagicMock()
    coordinator.plant_diagnostics = MagicMock()
    coordinator.plant_diagnostics.has_critical_sensor_fault = MagicMock(return_value=False)
    return coordinator, zone_demands


def test_zone_controller_init():
    hass = MagicMock()
    coordinator, _ = create_mock_coordinator()
    zc = ZoneController(hass, coordinator)

    assert len(zc.pids) == 2
    assert len(zc.autotuners) == 2
    assert len(zc.thermal_models) == 2

    cid1 = make_zone_entity_id("Living Room")
    cid2 = make_zone_entity_id("Bedroom")

    assert zc.get_pid(cid1) is not None
    assert zc.get_autotuner(cid1) is not None
    assert zc.get_thermal_model(cid1) is not None
    assert zc.get_pid("non_existent") is None
    print("test_zone_controller_init PASSED")


def test_zone_controller_selects_rate_estimator_from_coordinator():
    from custom_components.multizone_thermostat import rate_estimators
    from custom_components.multizone_thermostat.rate_estimators import LegacyEmaEstimator

    class Marker(LegacyEmaEstimator):
        pass

    rate_estimators.register_estimator("marker", Marker)
    try:
        cid = make_zone_entity_id("Living Room")

        coordinator, _ = create_mock_coordinator()
        coordinator.rate_estimator = "marker"
        zc = ZoneController(MagicMock(), coordinator)
        assert type(zc.get_thermal_model(cid).estimator) is Marker
        assert zc.get_thermal_model(cid).estimator_name == "marker"

        # Unset (MagicMock attribute, not a string) -> default estimator
        coordinator, _ = create_mock_coordinator()
        zc = ZoneController(MagicMock(), coordinator)
        assert type(zc.get_thermal_model(cid).estimator) is LegacyEmaEstimator
    finally:
        rate_estimators._REGISTRY.pop("marker", None)


def test_zone_controller_load_state():
    hass = MagicMock()
    coordinator, _ = create_mock_coordinator()
    zc = ZoneController(hass, coordinator)
    cid = make_zone_entity_id("Living Room")

    autotune_data = {
        cid: {
            "completed_cycles": [
                {"Kp": 42.0, "Ki": 0.05, "Kd": 12.0},
                {"Kp": 42.0, "Ki": 0.05, "Kd": 12.0},
                {"Kp": 42.0, "Ki": 0.05, "Kd": 12.0},
            ]
        }
    }
    thermal_data = {
        cid: {
            "heating_rate": 1.5,
            "cooling_rate": 0.3,
            "thermal_inertia": 0.4,
        }
    }

    zc.load_state(autotune_data, thermal_data)

    tuner = zc.get_autotuner(cid)
    assert tuner.state == tuner.STATE_COMPLETED
    pid = zc.get_pid(cid)
    print("DEBUG tuner.ki:", tuner.ki, "pid._pid._Ki:", pid._pid._Ki)
    assert pid._pid._Kp == 42.0
    assert abs(pid._pid._Ki - 0.05) < 1e-6
    assert pid._pid._Kd == 12.0

    thermal = zc.get_thermal_model(cid)
    assert thermal.heating_rate == 1.5
    assert thermal.cooling_rate == 0.3
    assert thermal.thermal_inertia == 0.4
    print("test_zone_controller_load_state PASSED")


async def test_zone_controller_update_missing_temps():
    hass = MagicMock()
    coordinator, demands = create_mock_coordinator()
    zc = ZoneController(hass, coordinator)
    cid = make_zone_entity_id("Living Room")

    state = DummyState("heat", current_temp=None, target_temp=20.0)
    res = await zc.async_update_zone(cid, state)
    assert res == 0.0
    assert demands[cid] == 0.0
    print("test_zone_controller_update_missing_temps PASSED")


async def test_zone_controller_update_off_or_bypass():
    hass = MagicMock()
    coordinator, demands = create_mock_coordinator()
    zc = ZoneController(hass, coordinator)
    cid = make_zone_entity_id("Living Room")

    # HVAC OFF
    state_off = DummyState("off", current_temp=18.0, target_temp=20.0)
    res = await zc.async_update_zone(cid, state_off)
    assert res == 0.0
    assert demands[cid] == 0.0

    # Zone mode BYPASS
    coordinator.zone_manager.get_zone_mode.return_value = ZONE_MODE_BYPASS
    state_heat = DummyState("heat", current_temp=18.0, target_temp=20.0)
    res2 = await zc.async_update_zone(cid, state_heat)
    assert res2 == 0.0
    assert demands[cid] == 0.0
    print("test_zone_controller_update_off_or_bypass PASSED")


async def test_zone_controller_update_fault_or_dhw():
    hass = MagicMock()
    coordinator, demands = create_mock_coordinator()
    zc = ZoneController(hass, coordinator)
    cid = make_zone_entity_id("Living Room")

    # Critical fault
    coordinator.plant_diagnostics.has_critical_sensor_fault.return_value = True
    state = DummyState("heat", current_temp=18.0, target_temp=20.0)
    res = await zc.async_update_zone(cid, state)
    assert res == 0.0
    assert demands[cid] == 0.0

    # DHW Active (freezes demand)
    coordinator.plant_diagnostics.has_critical_sensor_fault.return_value = False
    coordinator.is_dhw_active = True
    demands[cid] = 50.0  # current demand
    res = await zc.async_update_zone(cid, state)
    assert res == 50.0
    print("test_zone_controller_update_fault_or_dhw PASSED")


async def test_zone_controller_hysteresis_learning():
    hass = MagicMock()
    coordinator, demands = create_mock_coordinator()
    zc = ZoneController(hass, coordinator)
    cid = make_zone_entity_id("Living Room")

    # Temp below target - 0.3 -> demand 100%
    state_cold = DummyState("heat", current_temp=19.5, target_temp=20.0)
    res1 = await zc.async_update_zone(cid, state_cold)
    assert res1 == 100.0
    assert demands[cid] == 100.0

    # Temp above target + 0.3 -> demand 0%
    state_warm = DummyState("heat", current_temp=20.5, target_temp=20.0)
    res2 = await zc.async_update_zone(cid, state_warm)
    assert res2 == 0.0
    assert demands[cid] == 0.0
    print("test_zone_controller_hysteresis_learning PASSED")


async def test_zone_controller_smart_pid_and_weather_comp():
    hass = MagicMock()
    coordinator, demands = create_mock_coordinator()
    cid = make_zone_entity_id("Living Room")

    coordinator.weather_sensor_id = "sensor.outdoor_temp"
    outdoor_state = DummyState("5.0", domain="sensor")
    hass.states.get = MagicMock(return_value=outdoor_state)
    coordinator.get_persistent_data = MagicMock(return_value=1.5)

    zc = ZoneController(hass, coordinator)
    tuner = zc.get_autotuner(cid)
    tuner.state = tuner.STATE_COMPLETED
    tuner.kp = 10.0
    tuner.ki = 0.0
    tuner.kd = 0.0
    zc.get_pid(cid).set_pid_param(kp=10.0, ki=0.0, kd=0.0)

    state = DummyState("heat", current_temp=19.0, target_temp=20.0)
    res = await zc.async_update_zone(cid, state)

    # Effective target = 20.0 (inertia 0)
    # PID with error = 1.0, Kp=10 -> base demand = 10.0%
    # Weather: (20 - 5) * 1.5 = 22.5%
    # Total demand = 10.0 + 22.5 = 32.5%
    assert res > 25.0
    assert demands[cid] == res
    coordinator.debounced_save_thermal_states.assert_called_once()
    print("test_zone_controller_smart_pid_and_weather_comp PASSED")


async def test_zone_controller_feeds_outdoor_temp_to_estimator():
    hass = MagicMock()
    coordinator, _ = create_mock_coordinator()
    cid = make_zone_entity_id("Living Room")
    coordinator.weather_sensor_id = "weather.home"
    hass.states.get = MagicMock(
        return_value=DummyState("sunny", target_temp=7.5, domain="weather")
    )

    zc = ZoneController(hass, coordinator)
    seen = []
    zc.get_thermal_model(cid).estimator.set_outdoor = seen.append
    await zc.async_update_zone(cid, DummyState("heat", current_temp=19.0, target_temp=20.0))
    assert seen == [7.5]

    hass.states.get = MagicMock(return_value=DummyState("unavailable", domain="weather"))
    await zc.async_update_zone(cid, DummyState("heat", current_temp=19.0, target_temp=20.0))
    assert seen == [7.5, None]


async def main():
    test_zone_controller_init()
    test_zone_controller_load_state()
    await test_zone_controller_update_missing_temps()
    await test_zone_controller_update_off_or_bypass()
    await test_zone_controller_update_fault_or_dhw()
    await test_zone_controller_hysteresis_learning()
    await test_zone_controller_smart_pid_and_weather_comp()
    print("\nAll ZoneController tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
