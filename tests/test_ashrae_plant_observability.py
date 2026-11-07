"""ASHRAE Guideline 36-2018 addendum y inspired tests, not conformance tests.

The domestic plant's control contract differs from the central plant sequences in the guideline;
timings and thresholds here are MZT configuration values, not ASHRAE requirements.
"""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.components.climate import HVACMode
from custom_components.multizone_thermostat import telemetry
from custom_components.multizone_thermostat.boiler.opentherm_backend import OpenThermBoilerBackend
from custom_components.multizone_thermostat.boiler.plant_orchestrator import PlantOrchestrator
from custom_components.multizone_thermostat.boiler.relay_backend import RelayBoilerBackend
from custom_components.multizone_thermostat.demand_aggregator import DemandAggregator
from custom_components.multizone_thermostat.const import VALVE_SCALE_PERCENT, VALVE_SCALE_VOLT, make_zone_entity_id
from custom_components.multizone_thermostat.sync_manager import ZoneSyncManager
from tests.mzt_replay.drivers import (
    replay_proportional_valves_from_records, replay_stateful_proportional_valves_from_records,
    replay_relay_delay_checks_from_records, replay_relay_delay_cycles_from_records,
    replay_relay_switch_paths_from_records,
)


@pytest.fixture
def recorder(tmp_path):
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    yield recorder
    telemetry.set_recorder(None)


def records(recorder, kind):
    recorder.flush()
    return [r for r in telemetry.read_records([str(p) for p in sorted(
        Path(recorder.directory).glob("*.jsonl")
    )]) if r["kind"] == kind]


def make_plant():
    agg = DemandAggregator([{"name": "Primary"}, {"name": "Secondary"}])
    primary = make_zone_entity_id("Primary")
    secondary = make_zone_entity_id("Secondary")
    agg.set_zone_demand(primary, 40)
    agg.set_zone_demand(secondary, 80)
    agg.set_zone_mode(secondary, "secondary")
    backend = SimpleNamespace(async_update=AsyncMock(), async_emergency_off=AsyncMock())
    coordinator = SimpleNamespace(
        get_master_state=lambda: True, is_dhw_active=False, zones=[],
        _climate_entities={}, _climate_callbacks={},
    )
    return PlantOrchestrator(MagicMock(), agg, backend, coordinator), primary, secondary


@pytest.mark.asyncio
async def test_real_aggregation_and_override_decisions_are_observable(recorder):
    """§5.21.2: adapted plant enable inputs and overrides, not its fixed timing."""
    plant, primary, secondary = make_plant()
    await plant._async_update_boiler()
    plant.coordinator.is_dhw_active = True
    await plant._async_update_boiler()
    plant.coordinator.is_dhw_active = False
    plant.coordinator.frost_manager = SimpleNamespace(async_handle_frost_emergencies=AsyncMock(return_value=True))
    await plant._async_update_boiler()
    rows = [r["data"] for r in records(recorder, telemetry.KIND_PLANT)]
    assert [r["demand"] for r in rows] == [40, 0, 100]
    assert rows[0]["raw_demands"] == {primary: 40, secondary: 80}
    assert rows[0]["primary_demands"] == {"Primary": 40}
    assert rows[0]["secondary_demands"] == {"Secondary": 80}
    assert rows[1]["dhw_active"] and rows[1]["result_dhw_active"]
    assert rows[2]["frost_emergency"] and rows[2]["result_frost_emergency"]
    assert [call.args[0] for call in plant.boiler_backend.async_update.await_args_list] == [40, 0, 100]


@pytest.mark.asyncio
async def test_fault_suppression_and_master_off_are_not_confused_with_zero_demand(recorder):
    """§5.21.2, §5.21.11: record operating context and fault-related suppression."""
    plant, primary, _ = make_plant()
    plant.coordinator.zones = [{"name": "Primary"}]
    plant.coordinator.plant_diagnostics = SimpleNamespace(
        evaluate_zone_anomalies=lambda _: None, has_critical_sensor_fault=lambda _: True,
    )
    await plant._async_update_boiler()
    plant.coordinator.get_master_state = lambda: False
    await plant._async_update_boiler()
    rows = [r["data"] for r in records(recorder, telemetry.KIND_PLANT)]
    assert rows[0]["raw_demands"][primary] == 40
    assert rows[0]["suppressed"] == {primary: "critical_sensor_fault"}
    assert rows[0]["demand"] == 0
    assert rows[1] == {
        "action": "master_off", "frost_emergency": False, "master_on": False, "aggregation_evaluated": False,
    }
    plant.boiler_backend.async_emergency_off.assert_awaited_once()


@pytest.mark.asyncio
async def test_simultaneous_dhw_and_frost_records_existing_priority(recorder):
    """§5.21.2 and §5.21.11: characterize domestic overrides, not a guideline priority."""
    plant, _, _ = make_plant()
    plant.coordinator.get_master_state = lambda: False
    plant.coordinator.is_dhw_active = True
    plant.coordinator.frost_manager = SimpleNamespace(async_handle_frost_emergencies=AsyncMock(return_value=True))
    await plant._async_update_boiler()
    row = records(recorder, telemetry.KIND_PLANT)[0]["data"]
    assert row["dhw_active"] and row["frost_emergency"]
    assert row["result_dhw_active"] and not row["result_frost_emergency"]
    assert row["demand"] == 0
    plant.boiler_backend.async_update.assert_awaited_once_with(0, None, True)


@pytest.mark.asyncio
async def test_missing_temperature_suppression_is_observable(recorder):
    """§5.21.11: adapted missing-sensor operating context, not a guideline alarm."""
    plant, primary, _ = make_plant()
    plant.coordinator._climate_entities = {primary: SimpleNamespace(current_temperature=None)}
    await plant._async_update_boiler()
    row = records(recorder, telemetry.KIND_PLANT)[0]["data"]
    assert row["raw_demands"][primary] == 40
    assert row["suppressed"][primary] == "missing_temperature"
    assert row["demand"] == 0


def make_opentherm():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get.return_value = SimpleNamespace(state="heat", attributes={"temperature": 55})
    coordinator = SimpleNamespace(plant_diagnostics=None, weather_sensor_id=None)
    return OpenThermBoilerBackend(hass, coordinator, "climate.boiler", 35, 75), hass


@pytest.mark.asyncio
@pytest.mark.parametrize("demand,target", [(0, 35), (25, 45), (50, 55), (100, 75), (120, 75)])
async def test_opentherm_mapping_records_inputs_and_output(recorder, demand, target):
    """§5.21.4: adapted bounded target mapping, not trim-and-respond conformance."""
    backend, _ = make_opentherm()
    await backend.async_update(demand, "Primary", False)
    row = records(recorder, telemetry.KIND_OPENTHERM)[0]["data"]
    assert row["demand"] == demand and row["target_temp"] == target
    assert row["min_temp"] == 35 and row["max_temp"] == 75
    assert row["observed_state"] == "heat"
    assert row["command_error"] is None


@pytest.mark.asyncio
async def test_opentherm_unavailable_and_command_failure_are_recorded(recorder):
    """§5.21.10, §5.21.11: distinguish unavailable inputs and failed command attempts."""
    backend, hass = make_opentherm()
    hass.states.get.return_value = SimpleNamespace(state="unavailable", attributes={})
    await backend.async_update(50, "Primary", False)
    hass.states.get.return_value = SimpleNamespace(state="off", attributes={"temperature": 30})
    hass.services.async_call.side_effect = RuntimeError("service failed")
    await backend.async_update(50, "Primary", False)
    rows = [r["data"] for r in records(recorder, telemetry.KIND_OPENTHERM)]
    assert rows[0]["action"] == "unavailable" and rows[0]["observed_state"] == "unavailable"
    assert rows[1]["target_temp"] == 55 and rows[1]["command_error"] == "service failed"
    assert not rows[1]["update_completed"]


@pytest.mark.asyncio
async def test_opentherm_weather_offset_is_observable(recorder):
    """§5.21.4: adapted weather compensation, not trim-and-respond."""
    backend, hass = make_opentherm()
    backend.coordinator.weather_sensor_id = "sensor.outside"
    backend.coordinator.get_persistent_data = lambda *_: 1.0
    hass.states.get.side_effect = lambda entity: (
        SimpleNamespace(state="5", attributes={}) if entity == "sensor.outside"
        else SimpleNamespace(state="heat", attributes={"temperature": 55})
    )
    await backend.async_update(50, "Primary", False)
    row = records(recorder, telemetry.KIND_OPENTHERM)[0]["data"]
    assert row["outdoor_temp"] == 5 and row["weather_curve"] == 1
    assert row["retention_factor"] == 1 and row["weather_flow_offset"] == 5
    assert row["target_temp"] == 60 and row["update_completed"]


@pytest.mark.asyncio
async def test_anti_seize_bypasses_aggregation(recorder):
    """§5.21.11: adapted operating-state observation, not its plant fault sequences."""
    plant, _, _ = make_plant()
    plant.coordinator.anti_seize_manager = SimpleNamespace(is_running=True)
    await plant.async_tick(None)
    row = records(recorder, telemetry.KIND_PLANT)[0]["data"]
    assert row == {"action": "anti_seize", "aggregation_evaluated": False}
    plant.boiler_backend.async_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_plant_snapshots_survive_later_demand_changes(recorder):
    """§5.21.2: adapted observation of demand at the time of each decision."""
    plant, primary, _ = make_plant()
    await plant._async_update_boiler()
    plant.demand_aggregator.set_zone_demand(primary, 5)
    await plant._async_update_boiler()
    rows = [r["data"] for r in records(recorder, telemetry.KIND_PLANT)]
    assert [row["raw_demands"][primary] for row in rows] == [40, 5]
    assert [row["primary_demands"]["Primary"] for row in rows] == [40, 5]


@pytest.mark.asyncio
async def test_failed_telemetry_snapshot_does_not_stop_plant(recorder, caplog):
    """§5.21.2: an unavailable observational snapshot must not suppress the plant decision."""
    plant, _, _ = make_plant()
    plant.demand_aggregator.get_all_demands = MagicMock(side_effect=RuntimeError("snapshot failed"))
    await plant._async_update_boiler()
    plant.boiler_backend.async_update.assert_awaited_once_with(40, "Primary", False)
    assert records(recorder, telemetry.KIND_PLANT)[0]["data"]["raw_demands"] is None
    assert "Unable to capture pre-suppression plant demands" in caplog.text
    telemetry.set_recorder(None)
    plant.demand_aggregator.get_all_demands.reset_mock()
    await plant._async_update_boiler()
    plant.demand_aggregator.get_all_demands.assert_not_called()
    assert plant.boiler_backend.async_update.await_count == 2


@pytest.mark.asyncio
async def test_opentherm_cancelled_update_is_not_complete(recorder):
    """§5.21.10 and §5.21.11: adapted interrupted command observation."""
    backend, hass = make_opentherm()
    hass.services.async_call.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await backend.async_update(25, "Primary", False)
    row = records(recorder, telemetry.KIND_OPENTHERM)[0]["data"]
    assert not row["update_completed"] and row["command_error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "closed", "unknown", "master_off", "dhw", "no_demand"])
async def test_delayed_relay_rechecks_switch_path_and_request(change):
    """§4.11, §5.21.2.4: adapted delayed path check; an HA switch is not flow proof."""
    state = SimpleNamespace(state="on")
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    hass.states.get.return_value = state
    coordinator = SimpleNamespace(
        zones=[{"name": "Primary", "switch_entities": ["switch.valve"]}],
        get_zone_mode=lambda _: "primary", get_zone_demand=lambda _: 40,
        get_master_state=lambda: True, is_dhw_active=False,
    )
    backend = RelayBoilerBackend(hass, coordinator, "switch.boiler", 0, 0, 1)
    backend._last_demand = 40
    backend._force_boiler_on = AsyncMock()
    backend._schedule_boiler_check(0, True)
    task = backend._pending_boiler_task
    if change == "closed":
        state.state = "off"
    elif change == "unknown":
        state.state = "unknown"
    elif change == "master_off":
        coordinator.get_master_state = lambda: False
    elif change == "dhw":
        coordinator.is_dhw_active = True
    elif change == "no_demand":
        backend._last_demand = 0
    await task
    assert backend._force_boiler_on.await_count == (1 if change == "none" else 0)
    assert backend._pending_boiler_task is None


@pytest.mark.asyncio
async def test_delayed_relay_task_cancellation_does_not_command():
    """§5.21.2.4: adapted cancellation during a valve-opening delay."""
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    backend = RelayBoilerBackend(hass, SimpleNamespace(), "switch.boiler", 0, 0, 1)
    backend._force_boiler_on = AsyncMock()
    backend._schedule_boiler_check(60, True)
    task = backend._pending_boiler_task
    await asyncio.sleep(0)
    task.cancel()
    await task
    backend._force_boiler_on.assert_not_awaited()
    assert backend._pending_boiler_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("frost,master,dhw,path,demand,expected", [
    (True, False, True, "off", 0, True),
    (False, False, False, "on", 50, False),
    (False, True, True, "on", 50, False),
    (False, True, False, "off", 50, False),
    (False, True, False, "on", 50, True),
    (False, True, False, "on", 0, False),
])
async def test_delayed_relay_recheck_record_replay(recorder, frost, master, dhw, path, demand, expected):
    """§4.11 and §5.21.2.4: adapted delayed request rechecks, not flow proof."""
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    hass.states.get.return_value = SimpleNamespace(state=path)
    coord = SimpleNamespace(
        get_master_state=lambda: master, is_dhw_active=dhw,
        zones=[{"name": "Room", "switch_entities": ["switch.valve"]}],
        get_zone_mode=lambda _: "primary", get_zone_demand=lambda _: 50,
    )
    backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0)
    backend._last_frost_emergency = frost
    backend._last_demand = demand
    backend._force_boiler_on = AsyncMock()
    backend._schedule_boiler_check(0, True)
    await backend._pending_boiler_task
    rows = records(recorder, telemetry.KIND_EVENT)
    delay_phases = [row["data"]["phase"] for row in rows if row["data"]["event"] == "relay_delay"]
    assert delay_phases == (["scheduled", "waiting", "expired", "rechecked", "on_request_started", "on_request_completed", "finished"]
                            if expected else ["scheduled", "waiting", "expired", "rechecked", "finished"])
    assert backend._force_boiler_on.await_count == int(expected)
    telemetry.set_recorder(None)
    replayed = replay_relay_delay_checks_from_records(rows)
    assert replayed[0][1] is expected
    cycles = await replay_relay_delay_cycles_from_records(rows)
    assert cycles[0]["actions"] == (["on"] if expected else [])
    assert cycles[0]["reason"] == backend.status_reason
    with pytest.raises(ValueError, match="Incomplete relay delay"):
        await replay_relay_delay_cycles_from_records(rows[:-1])
    bad = deepcopy(rows)
    check = next(row["data"] for row in bad if row["data"]["event"] == "relay_delay_check")
    check["result"] = not expected
    with pytest.raises(ValueError, match="diverged"):
        replay_relay_delay_checks_from_records(bad)
    missing = deepcopy(rows)
    check = next(row["data"] for row in missing if row["data"]["event"] == "relay_delay_check")
    del check["frost"]
    with pytest.raises(ValueError, match="lacks inputs"):
        replay_relay_delay_checks_from_records(missing)


@pytest.mark.asyncio
async def test_delayed_relay_lifecycle_records_cancellation(recorder):
    """§5.21.2.4: adapted cancellation during delayed plant enable."""
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    backend = RelayBoilerBackend(hass, SimpleNamespace(), "switch.boiler", 0, 0, 60)
    backend._force_boiler_on = AsyncMock()
    backend._schedule_boiler_check(60, True)
    task = backend._pending_boiler_task
    await asyncio.sleep(0)
    task.cancel()
    await task
    rows = records(recorder, telemetry.KIND_EVENT)
    delay = [row["data"] for row in rows if row["data"]["event"] == "relay_delay"]
    assert [row["phase"] for row in delay] == ["scheduled", "waiting", "cancelled", "finished"]
    assert len({(row["epoch"], row["step"]) for row in delay}) == 1
    backend._force_boiler_on.assert_not_awaited()
    telemetry.set_recorder(None)
    assert (await replay_relay_delay_cycles_from_records(rows))[0]["actions"] == []


@pytest.mark.asyncio
async def test_delayed_relay_lifecycle_records_service_failure(recorder):
    """§5.21.11: adapted delayed operation failure visibility."""
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    backend = RelayBoilerBackend(hass, SimpleNamespace(), "switch.boiler", 0, 0, 0)
    backend._last_frost_emergency = True
    backend._force_boiler_on = AsyncMock(side_effect=RuntimeError("relay failed"))
    backend._schedule_boiler_check(0, True)
    task = backend._pending_boiler_task
    with pytest.raises(RuntimeError, match="relay failed"):
        await task
    rows = records(recorder, telemetry.KIND_EVENT)
    delay = [row["data"] for row in rows
             if row["data"]["event"] == "relay_delay"]
    assert [row["phase"] for row in delay] == [
        "scheduled", "waiting", "expired", "rechecked", "on_request_started", "error", "finished",
    ]
    assert delay[-2]["error"] == "relay failed"
    telemetry.set_recorder(None)
    assert (await replay_relay_delay_cycles_from_records(rows))[0]["actions"] == ["on"]


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_on", [True, False])
async def test_delayed_relay_replays_cancellation_during_request(recorder, turn_on):
    """§5.21.2.4: adapted cancellation during delayed command, not physical actuation."""
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    backend = RelayBoilerBackend(hass, SimpleNamespace(), "switch.boiler", 0, 0, 0)
    backend._last_frost_emergency = True
    backend._last_demand = 38
    backend._force_boiler_on = AsyncMock(side_effect=asyncio.CancelledError)
    backend._force_boiler_off = AsyncMock(side_effect=asyncio.CancelledError)
    backend._schedule_boiler_check(0, turn_on)
    await backend._pending_boiler_task
    rows = records(recorder, telemetry.KIND_EVENT)
    telemetry.set_recorder(None)
    replayed = (await replay_relay_delay_cycles_from_records(rows))[0]
    assert replayed["actions"] == (["on"] if turn_on else ["off"])
    assert replayed["reason"] == backend.status_reason


@pytest.mark.parametrize("states,expected", [
    ({"switch.first": "off", "switch.second": "off"}, False),
    ({"switch.first": "unavailable", "switch.second": "on"}, True),
    ({"switch.first": "on", "switch.second": "off"}, True),
    ({}, False),
])
def test_relay_switch_path_replays_observed_inputs(recorder, states, expected):
    """§4.11 and §5.21.2.4: adapted switch-path inputs, not evidence of physical flow."""
    hass = MagicMock()
    hass.states.get.side_effect = lambda entity: (
        SimpleNamespace(state=states[entity]) if entity in states else None
    )
    zones = [
        {"name": "Secondary", "switch_entities": ["switch.ignored"]},
        {"name": "Idle", "switch_entities": ["switch.ignored"]},
        {"name": "Room", "switch_entities": ["switch.first", "switch.second"]},
    ]
    coord = SimpleNamespace(
        zones=zones,
        get_zone_mode=lambda cid: "secondary" if cid.endswith("_secondary") else "primary",
        get_zone_demand=lambda cid: 0 if cid.endswith("_idle") else 70,
    )
    backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0)
    assert backend._switch_valve_path_open() is expected
    rows = records(recorder, telemetry.KIND_EVENT)
    telemetry.set_recorder(None)
    assert replay_relay_switch_paths_from_records(rows)[0][1] is expected
    changed = deepcopy(rows)
    changed[0]["data"]["result"] = not expected
    with pytest.raises(ValueError, match="diverged"):
        replay_relay_switch_paths_from_records(changed)
    missing = deepcopy(rows)
    del missing[0]["data"]["zones"][-1]["demand"]
    with pytest.raises(ValueError, match="lacks demand"):
        replay_relay_switch_paths_from_records(missing)


def test_relay_switch_path_replays_trv_only_and_empty_configuration(recorder):
    """§4.11 and §5.21.2.4: adapted absence of a demanding switch-only path."""
    coord = SimpleNamespace(
        zones=[{"name": "Room", "climate_entities": ["climate.trv"], "switch_entities": ["switch.valve"]}],
        get_zone_mode=lambda _: "primary", get_zone_demand=lambda _: 25,
    )
    backend = RelayBoilerBackend(MagicMock(), coord, "switch.boiler", 0, 0, 0)
    assert backend._switch_valve_path_open() is None
    coord.zones = []
    assert backend._switch_valve_path_open() is None
    rows = records(recorder, telemetry.KIND_EVENT)
    telemetry.set_recorder(None)
    assert [result for _, result in replay_relay_switch_paths_from_records(rows)] == [None, None]


def make_valve(entity="number.test_valve"):
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get.return_value = SimpleNamespace(state="30")
    coordinator = SimpleNamespace(
        get_zone_mode=lambda _: "primary", get_zone_demand=lambda _: 65.0,
    )
    manager = ZoneSyncManager(
        hass=hass, coordinator=coordinator, zone_name="Test", entity_id="climate.test",
        climates=[], switches=[], calibrations={}, valves=[entity], valve_scale=VALVE_SCALE_PERCENT,
    )
    return manager, coordinator, hass


@pytest.mark.asyncio
async def test_valve_command_deadband_and_unavailable(recorder):
    """§4.11 and §5.21.10.6-.7: adapted command/observation, not proof of flow."""
    manager, _, hass = make_valve()
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    hass.states.get.return_value = SimpleNamespace(state="unavailable")
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    rows = [r["data"] for r in records(recorder, telemetry.KIND_VALVE)]
    assert [r["action"] for r in rows] == ["command_attempt", "deadband", "skipped_unavailable"]
    assert rows[0]["target_value"] == 65 and rows[0]["observed"] == "30"
    assert rows[0]["command_completed"] and rows[0]["command_error"] is None
    assert rows[1]["previous_command"] == 65
    assert rows[2]["observed"] == "unavailable"
    assert hass.services.async_call.await_count == 1


@pytest.mark.asyncio
async def test_valve_failure_unsupported_and_overrides(recorder):
    """§5.21.10.6-.7: adapted command failure and override observability."""
    manager, coordinator, hass = make_valve()
    hass.services.async_call.side_effect = RuntimeError("valve failed")
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    hass.services.async_call.side_effect = None
    await manager.async_sync_proportional_valves(HVACMode.OFF)
    coordinator.get_zone_mode = lambda _: "bypass"
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    unsupported, _, unsupported_hass = make_valve("sensor.test_valve")
    await unsupported.async_sync_proportional_valves(HVACMode.HEAT)
    rows = [r["data"] for r in records(recorder, telemetry.KIND_VALVE)]
    assert rows[0]["command_error"] == "valve failed" and not rows[0]["command_completed"]
    assert rows[1]["target_pct"] == rows[2]["target_pct"] == 0
    assert rows[2]["zone_mode"] == "bypass"
    assert rows[3]["action"] == "unsupported_domain" and not rows[3]["command_completed"]
    unsupported_hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_valve_command_is_not_reported_complete(recorder):
    """§5.21.10.6-.7: adapted incomplete command observation, not feedback proof."""
    manager, _, hass = make_valve()
    hass.services.async_call.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await manager.async_sync_proportional_valves(HVACMode.HEAT)
    row = records(recorder, telemetry.KIND_VALVE)[0]["data"]
    assert not row["command_completed"] and row["command_error"] is None
    assert row["command_cancelled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entity,scale", [
    ("valve.test", VALVE_SCALE_PERCENT), ("number.test", VALVE_SCALE_VOLT),
    ("input_number.test", VALVE_SCALE_PERCENT), ("sensor.test", VALVE_SCALE_PERCENT),
])
async def test_proportional_valve_replay_decisions_and_failures(recorder, entity, scale):
    """§4.11 and §5.21.10.6-.7: adapted command replay, not actuator feedback."""
    manager, coordinator, hass = make_valve(entity)
    manager._valve_scale = scale
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    hass.states.get.return_value = None
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    hass.states.get.return_value = SimpleNamespace(state="unknown")
    await manager.async_sync_proportional_valves(HVACMode.OFF)
    coordinator.get_zone_mode = lambda _: "bypass"
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    coordinator.get_zone_mode = lambda _: "primary"
    hass.services.async_call.side_effect = RuntimeError("service error")
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    if entity != "sensor.test":
        hass.services.async_call.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await manager.async_sync_proportional_valves(HVACMode.HEAT)
    rows = records(recorder, telemetry.KIND_VALVE)
    telemetry.set_recorder(None)
    replayed = await replay_proportional_valves_from_records(rows)
    assert len(replayed) == len(rows)
    assert replayed[0]["last_command"] == (6.5 if scale == VALVE_SCALE_VOLT else 65)
    assert replayed[1]["commands"] == []
    assert replayed[2]["commands"] == []
    if entity != "sensor.test":
        assert replayed[-1]["cancelled"]
        assert replayed[-2]["record"]["data"]["command_error"] == "service error"
    missing = deepcopy(rows)
    del missing[0]["data"]["raw_demand"]
    with pytest.raises(ValueError, match="lacks replay inputs"):
        await replay_proportional_valves_from_records(missing)
    divergent = deepcopy(rows)
    divergent[0]["data"]["target_pct"] = 17
    with pytest.raises(ValueError, match="diverged"):
        await replay_proportional_valves_from_records(divergent)


@pytest.mark.asyncio
async def test_stateful_valve_replay_detects_dropped_and_divergent_decisions(recorder):
    """§4.11 and §5.21.10.6-.7: adapted stateful command-history reconstruction."""
    manager, coordinator, hass = make_valve()
    manager._valves.append("valve.second")
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    coordinator.get_zone_demand = lambda _: 0
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    rows = records(recorder, telemetry.KIND_VALVE)
    telemetry.set_recorder(None)
    replayed = await replay_stateful_proportional_valves_from_records(rows)
    assert [len(row["commands"]) for row in replayed] == [1, 1, 0, 0, 1, 1]
    assert [row["last_command"] for row in replayed] == [65, 65, 65, 65, 0, 0]
    with pytest.raises(ValueError, match="missing, duplicate or reordered"):
        await replay_stateful_proportional_valves_from_records(rows[:2] + rows[4:])
    with pytest.raises(ValueError, match="ordering"):
        await replay_stateful_proportional_valves_from_records(rows[:1] + rows[2:])
    with pytest.raises(ValueError, match="Incomplete valve call"):
        await replay_stateful_proportional_valves_from_records(rows[:-1])
    changed = deepcopy(rows)
    changed[2]["data"]["previous_command"] = 40
    with pytest.raises(ValueError, match="divergent previous command"):
        await replay_stateful_proportional_valves_from_records(changed)
    altered = deepcopy(rows)
    altered[1]["data"]["raw_demand"] = 30
    with pytest.raises(ValueError, match="divergent shared inputs"):
        await replay_stateful_proportional_valves_from_records(altered)


@pytest.mark.asyncio
async def test_stateful_valve_replay_handles_mid_call_cancellation(recorder):
    """§5.21.10.6-.7: adapted interruption and subsequent command state."""
    manager, _, hass = make_valve()
    manager._valves.append("valve.second")
    hass.services.async_call.side_effect = [None, asyncio.CancelledError(), None, None]
    with pytest.raises(asyncio.CancelledError):
        await manager.async_sync_proportional_valves(HVACMode.HEAT)
    await manager.async_sync_proportional_valves(HVACMode.HEAT)
    rows = records(recorder, telemetry.KIND_VALVE)
    telemetry.set_recorder(None)
    replayed = await replay_stateful_proportional_valves_from_records(rows)
    assert [row["cancelled"] for row in replayed] == [False, True, False, False]
    assert [row["last_command"] for row in replayed] == [65, None, 65, 65]
