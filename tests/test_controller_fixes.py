"""Regression tests for the 2026-11-04 boiler/valve control failures."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.components.climate import HVACMode
from custom_components.multizone_thermostat.autotune import MAX_KD_TO_KP, PassiveAutotuneObserver
from custom_components.multizone_thermostat.boiler.relay_backend import RelayBoilerBackend
from custom_components.multizone_thermostat.pid import PID
from custom_components.multizone_thermostat.pid_wrapper import MultizonePID
from custom_components.multizone_thermostat.sync_manager import ZoneSyncManager


def _state(value, attrs=None):
    return MagicMock(state=value, attributes=attrs or {})


def _sync_manager(hass, demand=100.0, switches=("switch.valve",), valves=None):
    coordinator = MagicMock()
    coordinator.get_zone_demand.return_value = demand
    coordinator.get_zone_mode.return_value = "primary"
    return ZoneSyncManager(
        hass=hass,
        coordinator=coordinator,
        zone_name="Zone",
        entity_id="climate.zone",
        climates=[],
        switches=list(switches),
        calibrations={},
        valves=valves,
    )


def _calls(hass):
    return [c.args[1] for c in hass.services.async_call.call_args_list]


# ---------------------------------------------------------------- valve switches

@pytest.mark.asyncio
async def test_unknown_valve_switch_is_commanded():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(return_value=_state("unknown"))
    sm = _sync_manager(hass)

    await sm.async_pwm_tick(HVACMode.HEAT)

    assert _calls(hass) == ["turn_on"]


@pytest.mark.asyncio
async def test_unavailable_valve_switch_is_skipped_then_resynced_on_next_tick():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    sm = _sync_manager(hass)

    hass.states.get = MagicMock(return_value=_state("unavailable"))
    await sm.async_pwm_tick(HVACMode.HEAT)
    assert _calls(hass) == []

    # PWM state is unchanged on the next tick, but the switch is back: must resync
    hass.states.get = MagicMock(return_value=_state("off"))
    await sm.async_pwm_tick(HVACMode.HEAT)
    assert _calls(hass) == ["turn_on"]


@pytest.mark.asyncio
async def test_master_off_closes_unknown_valve_switch():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(return_value=_state("unknown"))
    sm = _sync_manager(hass)

    await sm.async_pwm_tick(HVACMode.OFF)

    assert _calls(hass) == ["turn_off"]


@pytest.mark.asyncio
async def test_unknown_proportional_valve_is_commanded():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(return_value=_state("unknown"))
    sm = _sync_manager(hass, demand=50.0, switches=(), valves=["number.valve"])

    await sm.async_sync_proportional_valves(HVACMode.HEAT)

    assert _calls(hass) == ["set_value"]


# ------------------------------------------------------------------ boiler relay

def _backend(zones, states, demand=80.0, master=True):
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(side_effect=lambda eid: states.get(eid))
    coordinator = MagicMock()
    coordinator.zones = zones
    coordinator.get_master_state.return_value = master
    coordinator.is_dhw_active = False
    coordinator.get_zone_mode.return_value = "primary"
    coordinator.get_zone_demand.return_value = demand
    backend = RelayBoilerBackend(hass, coordinator, "switch.boiler", 0.0, 0.0, 0.0)
    return hass, backend


SWITCH_ZONE = {"name": "Back Bedroom", "switch_entities": ["switch.valve"]}


@pytest.mark.asyncio
async def test_boiler_off_sent_when_state_unknown():
    hass, backend = _backend([], {"switch.boiler": _state("unknown")})
    await backend.async_emergency_off()
    assert _calls(hass) == ["turn_off"]


@pytest.mark.asyncio
async def test_boiler_off_sent_when_state_unavailable():
    hass, backend = _backend([], {"switch.boiler": _state("unavailable")})
    await backend._force_boiler_off()
    assert _calls(hass) == ["turn_off"]


@pytest.mark.asyncio
async def test_boiler_on_sent_when_state_unknown():
    hass, backend = _backend([], {"switch.boiler": _state("unknown")})
    await backend._force_boiler_on()
    assert _calls(hass) == ["turn_on"]


@pytest.mark.asyncio
async def test_boiler_holds_while_demanding_switch_valves_closed():
    states = {"switch.boiler": _state("off"), "switch.valve": _state("off")}
    hass, backend = _backend([SWITCH_ZONE], states)

    await backend.async_update(80.0, "Back Bedroom", False)

    assert "turn_on" not in _calls(hass)
    assert backend._status_reason.startswith("HOLD_VALVES_CLOSED")


@pytest.mark.asyncio
async def test_boiler_holds_with_unknown_valve_switch():
    states = {"switch.boiler": _state("off"), "switch.valve": _state("unknown")}
    hass, backend = _backend([SWITCH_ZONE], states)

    await backend.async_update(80.0, "Back Bedroom", False)

    assert "turn_on" not in _calls(hass)


@pytest.mark.asyncio
async def test_boiler_fires_when_switch_valve_open():
    states = {"switch.boiler": _state("off"), "switch.valve": _state("on")}
    hass, backend = _backend([SWITCH_ZONE], states)

    await backend.async_update(80.0, "Back Bedroom", False)

    assert _calls(hass) == ["turn_on"]


@pytest.mark.asyncio
async def test_boiler_valve_hold_never_blocks_frost_emergency():
    states = {"switch.boiler": _state("off"), "switch.valve": _state("off")}
    hass, backend = _backend([SWITCH_ZONE], states, demand=100.0)

    await backend.async_update(100.0, "Frost Emergency", True)

    assert _calls(hass) == ["turn_on"]


def test_valve_delay_recheck_fails_when_valves_closed():
    states = {"switch.boiler": _state("off"), "switch.valve": _state("off")}
    _, backend = _backend([SWITCH_ZONE], states)
    backend._last_demand = 80.0
    backend._last_frost_emergency = False
    assert backend._conditions_still_valid() is False

    states["switch.valve"] = _state("on")
    assert backend._conditions_still_valid() is True


def test_relay_only_zone_without_switches_is_not_blocked():
    _, backend = _backend([{"name": "Relay Zone"}], {"switch.boiler": _state("off")})
    assert backend._switch_valve_path_open() is None


# --------------------------------------------------------------------------- PID

def test_pid_derivative_spike_does_not_override_large_error():
    """Kd=60000, error=-3C, one -0.1C sample after 8 s must not demand heat."""
    pid = PID(100.0, 0.0, 60000.0, out_min=0.0, out_max=100.0, sampling_period=0)
    pid.calc(18.3, 15.0, input_time=1000.0, last_input_time=None)
    out, _ = pid.calc(18.2, 15.0, input_time=1008.0, last_input_time=1000.0)
    assert out == 0.0
    assert abs(pid.derivative) <= PID.MAX_DERIVATIVE


def test_pid_derivative_is_zero_without_enough_history():
    """Two samples seconds apart must never produce a derivative (no 2-point slopes)."""
    pid = PID(100.0, 0.0, 60000.0, out_min=0.0, out_max=100.0, sampling_period=0)
    pid.calc(18.3, 18.3, input_time=1000.0, last_input_time=None)
    pid.calc(18.2, 18.3, input_time=1010.0, last_input_time=1000.0)
    assert pid.derivative == 0.0
    pid.calc(18.2, 18.3, input_time=1010.2, last_input_time=1010.0)
    assert pid.derivative == 0.0


def test_pid_wrapper_flicker_with_huge_kd_stays_at_zero():
    pid = MultizonePID(100.0, 0.0, 100000.0)
    temps = [18.3, 18.2, 18.3, 18.2, 18.3]
    for t in temps:
        assert pid.calc(t, 15.0) == 0.0


def test_autotune_kd_is_capped():
    obs = PassiveAutotuneObserver("Zone", required_cycles=1)
    obs.update(18.0, True, now=0.0)
    for i in range(1, 31):  # 30 min of dead time
        obs.update(18.0, True, now=i * 60.0)
    for i in range(1, 11):  # then 0.03 C/min
        obs.update(18.0 + 0.03 * i, True, now=1800.0 + i * 60.0)
    obs.update(18.3, False, now=2400.0)

    assert obs.state == obs.STATE_COMPLETED
    assert obs.kd <= obs.kp * MAX_KD_TO_KP + 1e-6
    assert obs.kp * 0.5 * 1800 > obs.kp * MAX_KD_TO_KP  # the uncapped value would have been larger


# ------------------------------------------------------------------- hvac_action

def _zone_entity(switch_state, climates=(), demand=60.0):
    from homeassistant.components.climate import HVACAction
    from custom_components.multizone_thermostat.climate import MultizoneVirtualThermostat

    ent = object.__new__(MultizoneVirtualThermostat)
    ent._hvac_mode = HVACMode.HEAT
    ent._switches = ["switch.valve"]
    ent._climates = list(climates)
    ent._coordinator = MagicMock()
    ent._coordinator.get_zone_demand.return_value = demand
    ent.hass = MagicMock()
    ent.hass.states.get = MagicMock(return_value=_state(switch_state))
    ent.entity_id = "climate.zone"
    return ent, HVACAction


def test_hvac_action_idle_while_switch_valve_closed():
    ent, HVACAction = _zone_entity("off")
    assert ent.hvac_action == HVACAction.IDLE


def test_hvac_action_heating_when_switch_valve_open():
    ent, HVACAction = _zone_entity("on")
    assert ent.hvac_action == HVACAction.HEATING


def test_hvac_action_heating_for_trv_zone_with_demand():
    ent, HVACAction = _zone_entity("off", climates=["climate.trv"])
    assert ent.hvac_action == HVACAction.HEATING


# ------------------------------------------------------------ periodic zone refresh

@pytest.mark.asyncio
async def test_periodic_refresh_recomputes_every_available_zone():
    from custom_components.multizone_thermostat.coordinator import MultizoneCoordinator
    from custom_components.multizone_thermostat.const import make_zone_entity_id

    coord = object.__new__(MultizoneCoordinator)
    coord.zones = [{"name": "Kitchen"}, {"name": "Hall"}, {"name": "Gone"}]
    coord.hass = MagicMock()
    states = {
        make_zone_entity_id("Kitchen"): _state("heat"),
        make_zone_entity_id("Hall"): _state("unavailable"),
    }
    coord.hass.states.get = MagicMock(side_effect=lambda eid: states.get(eid))
    coord.zone_controller = MagicMock()
    coord.zone_controller.async_update_zone = AsyncMock(return_value=0.0)
    coord.demand_aggregator = MagicMock()
    coord.plant_orchestrator = MagicMock()
    coord.plant_orchestrator._async_update_boiler = AsyncMock()

    await coord._async_periodic_zone_refresh(None)

    coord.zone_controller.async_update_zone.assert_awaited_once()
    assert coord.zone_controller.async_update_zone.call_args.args[0] == make_zone_entity_id("Kitchen")
    coord.demand_aggregator.set_zone_demand.assert_called_once_with(make_zone_entity_id("Kitchen"), 0.0)


@pytest.mark.asyncio
async def test_periodic_refresh_survives_zone_failure():
    from custom_components.multizone_thermostat.coordinator import MultizoneCoordinator
    from custom_components.multizone_thermostat.const import make_zone_entity_id

    coord = object.__new__(MultizoneCoordinator)
    coord.zones = [{"name": "Kitchen"}, {"name": "Hall"}]
    coord.hass = MagicMock()
    coord.hass.states.get = MagicMock(return_value=_state("heat"))
    coord.zone_controller = MagicMock()
    coord.zone_controller.async_update_zone = AsyncMock(side_effect=[RuntimeError("boom"), 0.0])
    coord.demand_aggregator = MagicMock()
    coord.plant_orchestrator = MagicMock()
    coord.plant_orchestrator._async_update_boiler = AsyncMock()

    await coord._async_periodic_zone_refresh(None)

    assert coord.zone_controller.async_update_zone.await_count == 2


@pytest.mark.asyncio
async def test_periodic_refresh_is_demand_only():
    from custom_components.multizone_thermostat.coordinator import MultizoneCoordinator

    coord = object.__new__(MultizoneCoordinator)
    coord.zones = [{"name": "Kitchen"}]
    coord.hass = MagicMock()
    coord.hass.states.get = MagicMock(return_value=_state("heat"))
    coord.zone_controller = MagicMock()
    coord.zone_controller.async_update_zone = AsyncMock(return_value=0.0)
    coord.demand_aggregator = MagicMock()
    coord.plant_orchestrator = MagicMock()
    coord.plant_orchestrator._async_update_boiler = AsyncMock()

    await coord._async_periodic_zone_refresh(None)

    assert coord.zone_controller.async_update_zone.call_args.kwargs == {"learn": False}


@pytest.mark.asyncio
async def test_timer_refresh_does_not_feed_learning_observers():
    from custom_components.multizone_thermostat.zone_controller import ZoneController
    from custom_components.multizone_thermostat.const import make_zone_entity_id

    coordinator = MagicMock()
    coordinator.zones = [{"name": "Living Room"}]
    coordinator.weather_sensor_id = None
    coordinator.is_dhw_active = False
    coordinator.zone_manager.get_zone_mode = MagicMock(return_value="primary")
    coordinator.get_persistent_data = MagicMock(return_value=0.0)
    coordinator.plant_diagnostics.has_critical_sensor_fault = MagicMock(return_value=False)
    coordinator.demand_aggregator.get_zone_demand = MagicMock(return_value=0.0)
    zc = ZoneController(MagicMock(), coordinator)
    eid = make_zone_entity_id("Living Room")
    zc.thermal_models[eid].update = MagicMock()
    zc.autotuners[eid].update = MagicMock()
    zc.autotuners[eid].state = zc.autotuners[eid].STATE_IDLE
    state = MagicMock(state="heat", attributes={"current_temperature": 18.0, "temperature": 21.0})

    await zc.async_update_zone(eid, state, learn=False)
    zc.thermal_models[eid].update.assert_not_called()
    zc.autotuners[eid].update.assert_not_called()
    coordinator.demand_aggregator.set_zone_demand.assert_called()

    await zc.async_update_zone(eid, state)
    zc.thermal_models[eid].update.assert_called_once()
    zc.autotuners[eid].update.assert_called_once()
