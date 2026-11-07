"""Regression tests for telemetry inputs and deterministic component replay."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.multizone_thermostat import telemetry
from custom_components.multizone_thermostat import _async_update_listener
from custom_components.multizone_thermostat.climate import MultizoneVirtualThermostat
from custom_components.multizone_thermostat.boiler.opentherm_backend import OpenThermBoilerBackend
from custom_components.multizone_thermostat.boiler.plant_orchestrator import PlantOrchestrator
from custom_components.multizone_thermostat.boiler.relay_backend import RelayBoilerBackend
from custom_components.multizone_thermostat.coordinator import MultizoneCoordinator, snapshot_control_config
from custom_components.multizone_thermostat.demand_aggregator import DemandAggregator
from custom_components.multizone_thermostat.plant_diagnostics.anomalies import get_opentherm_diagnostics
from custom_components.multizone_thermostat.safety.anti_seize import AntiSeizeManager
from custom_components.multizone_thermostat.safety import anti_seize as anti_seize_module
from custom_components.multizone_thermostat.safety.frost import FrostManager
from custom_components.multizone_thermostat.schedule_manager import ScheduleProfileManager
from custom_components.multizone_thermostat.thermal_model import ThermalObserver
from custom_components.multizone_thermostat.zone_manager import ZoneManager
from tests.mzt_replay.drivers import (
    replay_aggregation_from_records, replay_estimator_from_records, replay_opentherm_targets_from_records,
    replay_opentherm_commands_from_records, replay_opentherm_diagnostics_from_records,
    replay_stateful_opentherm_from_records, replay_linked_opentherm_plant_from_records,
    replay_frost_checks_from_records, replay_frost_scans_from_records, replay_anti_seize_from_records,
    replay_plant_decisions_from_records,
    replay_linked_plant_pwm_from_records, replay_linked_relay_plant_from_records,
    replay_schedule_evaluations_from_records, replay_smart_return_plans_from_records,
    replay_linked_smart_return_evaluations_from_records, replay_stateful_schedule_evaluations_from_records,
    replay_manual_schedule_overrides_from_records,
    replay_relay_delayed_commands_from_records,
    replay_single_delayed_plant_from_records, replay_sequential_delayed_plant_from_records,
    replay_waiting_delayed_plant_from_records, replay_sequential_waiting_plant_from_records,
)
from tests.mzt_replay.history import (
    reconstruct_configured_inputs, reconstruct_persistent_settings, reconstruct_preset_history,
    reconstruct_runtime_control_settings,
)


@pytest.fixture
def recorder(tmp_path):
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    yield recorder
    telemetry.set_recorder(None)


def all_records(recorder):
    recorder.flush()
    return list(telemetry.read_records(str(p) for p in Path(recorder.directory).glob("*.jsonl")))


def test_aggregation_replay_effective_modes_weights_and_faults(recorder):
    """ASHRAE Addendum y §5.21.2: adapted demand decision, not a prescribed sequence."""
    modes = {"climate.a": "primary", "climate.b": "primary", "climate.c": "bypass"}
    agg = DemandAggregator(strategy="weighted_average", mode_getter=modes.get)
    for name, cid, weight, demand in [("A", "climate.a", 1, 20), ("B", "climate.b", 3, 80),
                                       ("C", "climate.c", 5, 90)]:
        agg.register_zone(name, cid, weight)
        agg.set_zone_demand(cid, demand)
    agg.coordinator = SimpleNamespace(plant_diagnostics=SimpleNamespace(
        has_critical_sensor_fault=lambda cid: cid == "climate.b",
    ))
    expected = [agg.calculate(), agg.calculate(is_dhw_active=True), agg.calculate(frost_emergency=True)]
    agg.coordinator.plant_diagnostics.has_critical_sensor_fault = lambda _: False
    expected.append(agg.calculate())
    rows = all_records(recorder)
    replayed = replay_aggregation_from_records(rows)
    assert [row[1] for row in replayed] == expected
    inputs = replayed[0][0]["data"]["zones"]
    assert inputs[1]["input_demand"] == 80 and inputs[1]["effective_demand"] == 0
    assert inputs[1]["fault_suppressed"]
    assert inputs[1]["configured_weight"] == 3 and inputs[2]["mode"] == "bypass"
    assert replayed[-1][1].demand == 65


def test_aggregation_replay_uses_actual_weight_reads(recorder):
    """ASHRAE Addendum y §5.21.2: adapted replay of the actual aggregation inputs."""
    agg = DemandAggregator([{"name": "A"}, {"name": "B"}], strategy="weighted_average")
    ids = list(agg.get_all_demands())
    agg.set_zone_demand(ids[0], 20)
    agg.set_zone_demand(ids[1], 80)
    values = iter([1, 3, 2, 4])
    agg.get_zone_weight = lambda _: next(values)
    expected = agg.calculate()
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    assert replay_aggregation_from_records(rows)[0][1] == expected
    assert [w["weight"] for w in rows[0]["data"]["weight_calls"]] == [1, 3, 2, 4]
    rows[0]["data"]["weight_calls"].pop()
    with pytest.raises(ValueError, match="weight"):
        replay_aggregation_from_records(rows)


@pytest.mark.asyncio
async def test_real_plant_decision_round_trip(recorder):
    """ASHRAE Addendum y §5.21.2 and §5.21.11: adapted decision replay, not compliance."""
    agg = DemandAggregator([{"name": "A"}, {"name": "B", "weight": 3}], strategy="weighted_average")
    cid_a, cid_b = list(agg.get_all_demands())
    agg.set_zone_demand(cid_a, 20)
    agg.set_zone_demand(cid_b, 80)
    backend = SimpleNamespace(async_update=AsyncMock(), async_emergency_off=AsyncMock())
    coord = SimpleNamespace(get_master_state=lambda: True, is_dhw_active=False, zones=[],
                            _climate_entities={}, _climate_callbacks={})
    plant = PlantOrchestrator(MagicMock(), agg, backend, coord)
    await plant._async_update_boiler()
    coord._climate_entities[cid_b] = SimpleNamespace(current_temperature=None)
    await plant._async_update_boiler()
    coord.is_dhw_active = True
    await plant._async_update_boiler()
    coord.get_master_state = lambda: False
    await plant._async_update_boiler()
    coord.anti_seize_manager = SimpleNamespace(is_running=True)
    await plant.async_tick(None)
    rows = all_records(recorder)
    replayed = await replay_plant_decisions_from_records(rows)
    assert [r[1] for r in replayed] == ["update", "update", "update", "emergency_off", "none"]
    assert [r[2] for r in replayed[:3]] == [call.args for call in backend.async_update.await_args_list]
    with pytest.raises(ValueError, match="lacks aggregation"):
        await replay_plant_decisions_from_records([r for r in rows if r["kind"] == "plant"])


@pytest.mark.asyncio
async def test_real_plant_decisions_link_to_stateful_boiler_pwm(recorder):
    """ASHRAE Addendum y §5.21.2: adapted recorded demand-to-boiler trace."""
    hass = MagicMock()
    state = {"value": "off"}
    hass.states.get.side_effect = lambda _: SimpleNamespace(state=state["value"])
    async def call(domain, service, data, **kwargs):
        state["value"] = "on" if service == "turn_on" else "off"
    hass.services.async_call = call
    agg = DemandAggregator([{"name": "A"}])
    cid = next(iter(agg.get_all_demands()))
    diag = SimpleNamespace(
        _current_boiler_start=None, has_critical_sensor_fault=lambda _: False,
    )
    diag.record_boiler_state = lambda on: setattr(diag, "_current_boiler_start", 1 if on else None)
    coord = SimpleNamespace(
        zones=[], _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: True, plant_diagnostics=diag,
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0)
    plant = PlantOrchestrator(hass, agg, backend, coord)
    for demand in (100, 50, 0):
        agg.set_zone_demand(cid, demand)
        await plant._async_update_boiler()
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    result = await replay_linked_plant_pwm_from_records(rows)
    assert [row["plant"]["data"]["demand"] for row in result] == [100, 50, 0]
    assert [row["pwm_state"] for row in result] == [True, True, False]
    relay_result = await replay_linked_relay_plant_from_records(rows)
    assert [[command[1] for command in row["commands"]] for row in relay_result] == [
        ["turn_on"], [], ["turn_off"],
    ]
    broken = deepcopy(rows)
    next(row for row in broken if row["kind"] == "boiler")["data"]["peak_zone"] = "wrong"
    with pytest.raises(ValueError, match="inputs diverge"):
        await replay_linked_plant_pwm_from_records(broken)
    broken = deepcopy(rows)
    next(row for row in broken if row["kind"] == "plant")["data"]["demand"] = 1
    with pytest.raises(ValueError, match="diverges from its recorded output"):
        await replay_linked_plant_pwm_from_records(broken)
    broken = deepcopy(rows)
    plant_records = [row for row in broken if row["kind"] == "plant"]
    plant_records[1]["seq"] = plant_records[0]["seq"]
    with pytest.raises(ValueError, match="duplicate sequence numbers"):
        await replay_linked_plant_pwm_from_records(broken)
    no_final = deepcopy(rows)
    last_boiler = next(i for i in range(len(no_final) - 1, -1, -1) if no_final[i]["kind"] == "boiler")
    del no_final[last_boiler]
    with pytest.raises(ValueError, match="incomplete boiler"):
        await replay_linked_plant_pwm_from_records(no_final)
    reordered = deepcopy(rows)
    first_boiler = next(i for i, row in enumerate(reordered) if row["kind"] == "boiler")
    second_plant = next(i for i in range(first_boiler + 1, len(reordered)) if reordered[i]["kind"] == "plant")
    reordered.insert(second_plant, reordered.pop(first_boiler))
    with pytest.raises(ValueError, match="reordered sequence numbers"):
        await replay_linked_plant_pwm_from_records(reordered)


@pytest.mark.asyncio
async def test_linked_relay_replays_switch_path_and_dhw_from_recorded_reads(recorder):
    """ASHRAE Addendum y §4.11, §5.21.2.4 and §5.21.11: adapted path and override replay."""
    hass = MagicMock()
    states = {"switch.boiler": "off", "switch.valve": "off"}
    hass.states.get.side_effect = lambda entity: SimpleNamespace(state=states[entity], attributes={})
    async def call(domain, service, data, **kwargs):
        states[data["entity_id"]] = "on" if service == "turn_on" else "off"
    hass.services.async_call = call
    agg = DemandAggregator([{"name": "A", "switch_entities": ["switch.valve"]}])
    cid = next(iter(agg.get_all_demands()))
    agg.set_zone_demand(cid, 100)
    diag = SimpleNamespace(_current_boiler_start=None, has_critical_sensor_fault=lambda _: False)
    diag.record_boiler_state = lambda on: setattr(diag, "_current_boiler_start", 1 if on else None)
    coord = SimpleNamespace(
        zones=[{"name": "A", "switch_entities": ["switch.valve"]}],
        _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: True, plant_diagnostics=diag,
        get_zone_mode=agg.get_zone_mode, get_zone_demand=agg.get_zone_demand,
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    plant = PlantOrchestrator(hass, agg, RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0), coord)
    await plant._async_update_boiler()
    states["switch.valve"] = "on"
    await plant._async_update_boiler()
    coord.is_dhw_active = True
    await plant._async_update_boiler()
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_linked_relay_plant_from_records(rows)
    assert [row["reason"] for row in replayed] == [
        "HOLD_VALVES_CLOSED - Waiting for a zone valve to open",
        "ON - Demand 100.0% from A",
        "OFF - Cycle ended (Peak Demand: 0.0%)",
    ]
    assert [[command[1] for command in row["commands"]] for row in replayed] == [
        [], ["turn_on"], ["turn_off"],
    ]
    broken = deepcopy(rows)
    trace = next(row["data"]["replay_trace"] for row in broken if row["kind"] == "boiler")
    trace[0]["entity"] = "switch.other"
    with pytest.raises(ValueError, match="unexpected state"):
        await replay_linked_relay_plant_from_records(broken)
    broken = deepcopy(rows)
    next(row for row in broken if row["kind"] == "boiler")["data"]["replay_before"]["valve_delay"] = 1
    with pytest.raises(ValueError, match="delayed tasks"):
        await replay_linked_relay_plant_from_records(broken)
    broken = deepcopy(rows)
    next(row for row in broken if row["kind"] == "boiler")["data"]["replay_after"]["last_change"] = -1
    with pytest.raises(ValueError, match="diverged during backend replay"):
        await replay_linked_relay_plant_from_records(broken)
    broken = deepcopy(rows)
    del next(row for row in broken if row["kind"] == "boiler")["data"]["replay_trace"]
    with pytest.raises(ValueError, match="lacks replay inputs"):
        await replay_linked_relay_plant_from_records(broken)
    broken = deepcopy(rows)
    boilers = [row for row in broken if row["kind"] == "boiler"]
    boilers[1]["data"]["replay_before"]["last_change"] = -1
    with pytest.raises(ValueError, match="divergent previous backend state"):
        await replay_linked_relay_plant_from_records(broken)


@pytest.mark.asyncio
async def test_linked_relay_replays_master_off_between_updates(recorder):
    """ASHRAE Addendum y §5.21.2: adapted master-off interruption and restart."""
    hass = MagicMock()
    state = {"value": "off", "master": True}
    hass.states.get.side_effect = lambda _: SimpleNamespace(state=state["value"], attributes={})
    async def call(domain, service, data, **kwargs):
        state["value"] = "on" if service == "turn_on" else "off"
    hass.services.async_call = call
    agg = DemandAggregator([{"name": "A"}])
    agg.set_zone_demand(next(iter(agg.get_all_demands())), 100)
    diag = SimpleNamespace(_current_boiler_start=None, has_critical_sensor_fault=lambda _: False)
    diag.record_boiler_state = lambda on: setattr(diag, "_current_boiler_start", 1 if on else None)
    coord = SimpleNamespace(
        zones=[], _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: state["master"], plant_diagnostics=diag,
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    plant = PlantOrchestrator(hass, agg, RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0), coord)
    await plant._async_update_boiler()
    state["master"] = False
    await plant._async_update_boiler()
    state["master"] = True
    await plant._async_update_boiler()
    records = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_linked_relay_plant_from_records(records)
    assert [[command[1] for command in row["commands"]] for row in replayed] == [
        ["turn_on"], ["turn_off"], ["turn_on"],
    ]
    assert replayed[1]["reason"] == "OFF - Emergency/Master OFF"
    assert replayed[1]["emergency"]["data"]["completed"] is True
    boundary = next(i for i, row in enumerate(records)
                    if row["kind"] == "plant" and row["data"]["action"] == "master_off")
    replayed_from_off = await replay_linked_relay_plant_from_records(records[boundary:])
    assert [[command[1] for command in row["commands"]] for row in replayed_from_off] == [
        ["turn_off"], ["turn_on"],
    ]
    broken = deepcopy(records)
    event = next(row for row in broken if row["data"].get("event") == "relay_emergency_off")
    event["data"]["after"]["pending_command"] = None
    with pytest.raises(ValueError, match="diverged during backend replay"):
        await replay_linked_relay_plant_from_records(broken)
    broken = [row for row in records if row["data"].get("event") != "relay_emergency_off"]
    with pytest.raises(ValueError, match="overlapping or incomplete"):
        await replay_linked_relay_plant_from_records(broken)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_linked_relay_replays_failed_master_off(recorder, cancel):
    """ASHRAE Addendum y §5.21.11: adapted emergency command failure visibility."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="on", attributes={})
    hass.services.async_call = AsyncMock(side_effect=asyncio.CancelledError() if cancel else RuntimeError("off failed"))
    agg = DemandAggregator([{"name": "A"}])
    diag = SimpleNamespace(_current_boiler_start=None, has_critical_sensor_fault=lambda _: False,
                           record_boiler_state=lambda _: None)
    state = {"master": True}
    coord = SimpleNamespace(
        zones=[], _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: state["master"], plant_diagnostics=diag,
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    plant = PlantOrchestrator(hass, agg, RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0), coord)
    # A completed no-demand update establishes a replay boundary without issuing a switch call.
    hass.states.get.return_value = SimpleNamespace(state="off", attributes={})
    await plant._async_update_boiler()
    hass.states.get.return_value = SimpleNamespace(state="on", attributes={})
    state["master"] = False
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await plant._async_update_boiler()
    records = all_records(recorder)
    data = next(row["data"] for row in records if row["data"].get("event") == "relay_emergency_off")
    assert data["completed"] is False and data["cancelled"] is cancel
    assert data["error"] == (None if cancel else "off failed")
    assert next(event for event in data["trace"] if event["type"] == "service")["outcome"] == (
        "cancelled" if cancel else "error"
    )
    telemetry.set_recorder(None)
    replayed = await replay_linked_relay_plant_from_records(records)
    assert replayed[-1]["outcome"] == ("cancelled" if cancel else "error")
    assert replayed[-1]["error"] == (None if cancel else "off failed")
    with pytest.raises(ValueError, match="did not complete"):
        await replay_linked_plant_pwm_from_records(records, allow_emergency_off=True)


@pytest.mark.asyncio
async def test_linked_relay_replays_cycle_locks_and_trv_grace(recorder):
    """ASHRAE Addendum y §4.11 and §5.21.2.4: adapted timer and path observations."""
    from custom_components.multizone_thermostat import pwm_engine
    from custom_components.multizone_thermostat.boiler import relay_backend
    from tests.mzt_replay.clock import ManualClock, patched_time

    clock = ManualClock(start=10)
    hass = MagicMock()
    states = {"switch.boiler": "off", "climate.trv": "heat"}
    hass.states.get.side_effect = lambda entity: SimpleNamespace(
        state=states[entity], attributes={"position": 0} if entity == "climate.trv" else {},
    )
    async def call(domain, service, data, **kwargs):
        states[data["entity_id"]] = "on" if service == "turn_on" else "off"
    hass.services.async_call = call
    zones = [{"name": "A", "climate_entities": ["climate.trv"]}]
    agg = DemandAggregator(zones)
    cid = next(iter(agg.get_all_demands()))
    agg.set_zone_demand(cid, 100)
    diag = SimpleNamespace(_current_boiler_start=None, has_critical_sensor_fault=lambda _: False)
    diag.record_boiler_state = lambda on: setattr(diag, "_current_boiler_start", 1 if on else None)
    coord = SimpleNamespace(
        zones=zones, _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: True, plant_diagnostics=diag,
        get_zone_mode=agg.get_zone_mode, get_zone_demand=agg.get_zone_demand,
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    with patched_time(pwm_engine, clock), patched_time(relay_backend, clock):
        backend = RelayBoilerBackend(hass, coord, "switch.boiler", 2, 1, 0)
        plant = PlantOrchestrator(hass, agg, backend, coord)
        for timestamp in (10, 70, 100, 191):
            clock.set(timestamp)
            await plant._async_update_boiler()
        clock.set(193)
        agg.set_zone_demand(cid, 0)
        await plant._async_update_boiler()
        clock.set(194)
        coord.is_dhw_active = True
        await plant._async_update_boiler()
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_linked_relay_plant_from_records(rows)
    assert [row["reason"].split(" - ")[0] for row in replayed] == [
        "HOLD_OFF", "HOLD_TRV_CLOSED", "HOLD_TRV_CLOSED", "ON", "HOLD_ON", "OFF",
    ]
    assert [[command[1] for command in row["commands"]] for row in replayed] == [
        [], [], [], ["turn_on"], [], ["turn_off"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_on", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "error", "cancelled"])
async def test_delayed_relay_replays_real_switch_commands(recorder, turn_on, outcome):
    """ASHRAE Addendum y §4.11 and §5.21.11: adapted delayed relay command observations."""
    hass = MagicMock()
    state = {"value": "off" if turn_on else "on"}
    hass.states.get.side_effect = lambda _: SimpleNamespace(state=state["value"], attributes={})
    async def call(domain, service, data, **kwargs):
        if outcome == "error":
            raise RuntimeError("delayed service failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        state["value"] = "on" if service == "turn_on" else "off"
    hass.services.async_call = call
    hass.async_create_task = asyncio.create_task
    coord = SimpleNamespace(
        get_master_state=lambda: True, is_dhw_active=False, zones=[],
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0)
    backend._last_frost_emergency = True
    backend._last_demand = 80
    backend._schedule_boiler_check(0, turn_on)
    task = backend._pending_boiler_task
    if outcome == "error":
        with pytest.raises(RuntimeError, match="delayed service failed"):
            await task
    else:
        await task
    records = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_relay_delayed_commands_from_records(records)
    assert len(replayed) == 1 and replayed[0]["outcome"] == outcome
    assert replayed[0]["commands"][0][1] == ("turn_on" if turn_on else "turn_off")
    assert replayed[0]["error"] == ("delayed service failed" if outcome == "error" else None)
    broken = [row for row in records if row["data"].get("event") != "relay_delay_command"]
    with pytest.raises(ValueError, match="lacks its command record"):
        await replay_relay_delayed_commands_from_records(broken)
    if outcome != "completed":
        broken = deepcopy(records)
        trace = next(row["data"]["trace"] for row in broken if row["data"].get("event") == "relay_delay_command")
        next(item for item in trace if item["type"] == "service")["outcome"] = "completed"
        with pytest.raises(ValueError, match="incomplete or divergent"):
            await replay_relay_delayed_commands_from_records(broken)


@pytest.mark.asyncio
async def test_delayed_relay_replays_rejected_recheck_without_command(recorder):
    """ASHRAE Addendum y §4.11 and §5.21.2.4: adapted delayed-start recheck."""
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    coord = SimpleNamespace(get_master_state=lambda: False, is_dhw_active=False, zones=[])
    backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0)
    backend._last_demand = 80
    backend._schedule_boiler_check(0, True)
    await backend._pending_boiler_task
    records = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_relay_delayed_commands_from_records(records)
    assert replayed[0]["command"] is None and replayed[0]["commands"] == []
    assert replayed[0]["cycle"]["reason"] == "OFF - Heat request ended during valve delay"


@pytest.mark.asyncio
async def test_delayed_relay_rejects_missing_interior_task(recorder):
    """ASHRAE Addendum y §5.21.11: adapted missing relay task visibility."""
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    coord = SimpleNamespace(get_master_state=lambda: False, is_dhw_active=False, zones=[])
    backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0)
    backend._last_demand = 80
    for _ in range(3):
        backend._schedule_boiler_check(0, True)
        await backend._pending_boiler_task
    records = all_records(recorder)
    telemetry.set_recorder(None)
    assert len(await replay_relay_delayed_commands_from_records(records)) == 3
    with pytest.raises(ValueError, match="missing or duplicate step"):
        await replay_relay_delayed_commands_from_records([
            row for row in records if row["data"].get("step") != 1
        ])


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "error", "cancelled", "recheck_rejected", "overlap", "sequential"])
async def test_single_delayed_plant_replays_actual_task(recorder, outcome):
    """ASHRAE Addendum y §4.11, §5.21.2.4 and §5.21.11: adapted delayed plant path."""
    from custom_components.multizone_thermostat.boiler import relay_backend as relay_module
    from custom_components.multizone_thermostat import pwm_engine
    from tests.mzt_replay.clock import ManualClock, patched_time

    clock = ManualClock(start=100)
    recorder._mono = clock.monotonic
    hass = MagicMock()
    states = {"switch.boiler": "off", "switch.valve": "on"}
    master = {"on": True}
    hass.states.get.side_effect = lambda entity: SimpleNamespace(state=states[entity], attributes={})
    async def call(domain, service, data, **kwargs):
        if outcome == "error":
            raise RuntimeError("service failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        states[data["entity_id"]] = "on" if service == "turn_on" else "off"
    hass.services.async_call = call
    hass.async_create_task = asyncio.create_task
    zones = [{"name": "A", "switch_entities": ["switch.valve"]}]
    agg = DemandAggregator(zones)
    agg.set_zone_demand(next(iter(agg.get_all_demands())), 100)
    diag = SimpleNamespace(_current_boiler_start=None, has_critical_sensor_fault=lambda _: False,
                           record_boiler_state=lambda _: None)
    coord = SimpleNamespace(
        zones=zones, _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: master["on"], plant_diagnostics=diag,
        get_zone_mode=agg.get_zone_mode, get_zone_demand=agg.get_zone_demand,
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    async def immediate_sleep(seconds):
        clock.advance(seconds)
    shim = SimpleNamespace(sleep=immediate_sleep, current_task=asyncio.current_task,
                           CancelledError=asyncio.CancelledError)
    with patch.object(relay_module, "asyncio", shim), patched_time(relay_module, clock), patched_time(pwm_engine, clock):
        backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 5)
        plant = PlantOrchestrator(hass, agg, backend, coord)
        await plant._async_update_boiler()
        task = backend._pending_boiler_task
        if outcome == "overlap":
            await plant._async_update_boiler()
        if outcome == "recheck_rejected":
            master["on"] = False
        if outcome == "error":
            with pytest.raises(RuntimeError, match="service failed"):
                await task
        else:
            await task
        if outcome == "sequential":
            states["switch.boiler"] = "off"
            await plant._async_update_boiler()
            await backend._pending_boiler_task
    records = all_records(recorder)
    telemetry.set_recorder(None)
    if outcome == "overlap":
        with pytest.raises(ValueError, match="requires one update"):
            await replay_single_delayed_plant_from_records(records)
        return
    if outcome == "sequential":
        replayed = await replay_sequential_delayed_plant_from_records(records)
        assert len(replayed) == 2
        assert [row["cycle"]["outcome"] for row in replayed] == ["completed", "completed"]
        broken = deepcopy(records)
        boilers = [row for row in broken if row["kind"] == "boiler"]
        boilers[1]["data"]["replay_before"]["last_change"] += 1
        with pytest.raises(ValueError, match="divergent previous backend state"):
            await replay_sequential_delayed_plant_from_records(broken)
        broken = deepcopy(records)
        for row in broken:
            if row["data"].get("step") == 1 and "epoch" in row["data"]:
                row["data"]["epoch"] = "different-backend"
        with pytest.raises(ValueError, match="crosses a backend or PWM epoch"):
            await replay_sequential_delayed_plant_from_records(broken)
        return
    replayed = await replay_single_delayed_plant_from_records(records)
    if outcome == "recheck_rejected":
        assert replayed["cycle"]["command"] is None
        assert replayed["cycle"]["cycle"]["reason"] == "OFF - Heat request ended during valve delay"
    else:
        assert replayed["cycle"]["outcome"] == outcome
        assert replayed["cycle"]["commands"][0][1] == "turn_on"
    broken = deepcopy(records)
    check = next(row["data"] for row in broken if row["data"].get("event") == "relay_delay_check")
    check["frost"] = not check["frost"]
    with pytest.raises(ValueError):
        await replay_single_delayed_plant_from_records(broken)
    broken = deepcopy(records)
    expired = next(row for row in broken if row["data"].get("phase") == "expired")
    expired["mono"] -= 0.01
    with pytest.raises(ValueError, match="premature delay"):
        await replay_single_delayed_plant_from_records(broken)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_before_updates", [False, True])
@pytest.mark.parametrize("outcome", [
    "completed", "error", "cancelled", "recheck_rejected", "cancel_during_wait", "cancel_by_update",
])
async def test_delayed_plant_replays_updates_while_waiting(recorder, wait_before_updates, outcome):
    """ASHRAE Addendum y §4.11 and §5.21.2.4: adapted pending-delay demand changes."""
    from custom_components.multizone_thermostat import pwm_engine
    from custom_components.multizone_thermostat.boiler import relay_backend as relay_module
    from tests.mzt_replay.clock import ManualClock, patched_time

    clock = ManualClock(start=100)
    recorder._mono = clock.monotonic
    hass = MagicMock()
    states = {"switch.boiler": "off", "switch.valve": "on"}
    master = {"on": True}
    hass.states.get.side_effect = lambda entity: SimpleNamespace(state=states[entity], attributes={})
    async def call(domain, service, data, **kwargs):
        if outcome == "error":
            raise RuntimeError("service failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        states[data["entity_id"]] = "on" if service == "turn_on" else "off"
    hass.services.async_call = call
    hass.async_create_task = asyncio.create_task
    zones = [{"name": "A", "switch_entities": ["switch.valve"]}]
    agg = DemandAggregator(zones)
    cid = next(iter(agg.get_all_demands()))
    agg.set_zone_demand(cid, 100)
    diag = SimpleNamespace(_current_boiler_start=None, has_critical_sensor_fault=lambda _: False,
                           record_boiler_state=lambda _: None)
    coord = SimpleNamespace(
        zones=zones, _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: master["on"], plant_diagnostics=diag,
        get_zone_mode=agg.get_zone_mode, get_zone_demand=agg.get_zone_demand,
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    waiters = []
    async def sleep(_):
        future = asyncio.get_running_loop().create_future()
        waiters.append(future)
        await future
    shim = SimpleNamespace(sleep=sleep, current_task=asyncio.current_task, CancelledError=asyncio.CancelledError)
    with patch.object(relay_module, "asyncio", shim), patched_time(relay_module, clock), patched_time(pwm_engine, clock):
        backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 30)
        plant = PlantOrchestrator(hass, agg, backend, coord)
        await plant._async_update_boiler()
        task = backend._pending_boiler_task
        if wait_before_updates or outcome == "cancel_by_update":
            await asyncio.sleep(0)
        for demand in (50, 0 if outcome == "cancel_by_update" else 75):
            clock.advance(10)
            agg.set_zone_demand(cid, demand)
            await plant._async_update_boiler()
        if not wait_before_updates and outcome != "cancel_by_update":
            await asyncio.sleep(0)
        if outcome in ("cancel_during_wait", "cancel_by_update"):
            if outcome == "cancel_during_wait":
                task.cancel()
            await task
        else:
            master["on"] = outcome != "recheck_rejected"
            clock.advance(10 if wait_before_updates else 30)
            waiters[0].set_result(None)
            if outcome == "error":
                with pytest.raises(RuntimeError, match="service failed"):
                    await task
            else:
                await task
    records = all_records(recorder)
    telemetry.set_recorder(None)
    if outcome == "cancel_during_wait":
        replayed = await replay_waiting_delayed_plant_from_records(records)
        assert replayed["cancellation_source"] == "unknown"
        assert [row["boiler"]["data"]["demand"] for row in replayed["updates"]] == [100, 50, 75]
        assert replayed["cycle"]["commands"] == []
        broken = deepcopy(records)
        next(row for row in broken if row["data"].get("phase") == "finished")["data"]["reason"] = "forged"
        with pytest.raises(ValueError, match="cancellation diverged"):
            await replay_waiting_delayed_plant_from_records(broken)
        broken = deepcopy(records)
        broken = [row for row in broken if row["data"].get("phase") != "cancelled"]
        with pytest.raises(ValueError):
            await replay_waiting_delayed_plant_from_records(broken)
        return
    if outcome == "cancel_by_update":
        replayed = await replay_waiting_delayed_plant_from_records(records)
        assert replayed["cancellation_source"] == "update"
        assert [row["boiler"]["data"]["demand"] for row in replayed["updates"]] == [100, 50, 0]
        assert replayed["cycle"]["commands"] == []
        assert replayed["cycle"]["cycle"]["reason"] == "OFF - Idle (Peak Demand: 0.0%)"
        broken = deepcopy(records)
        [row for row in broken if row["kind"] == "boiler"][-1]["data"]["replay_after"]["delay_pending"] = True
        with pytest.raises(ValueError):
            await replay_waiting_delayed_plant_from_records(broken)
        broken = deepcopy(records)
        next(row for row in broken if row["data"].get("phase") == "cancelled")["mono"] = 0
        with pytest.raises(ValueError, match="premature delay"):
            await replay_waiting_delayed_plant_from_records(broken)
        broken = deepcopy(records)
        next(row for row in broken if row["data"].get("phase") == "finished")["data"]["reason"] = "forged"
        with pytest.raises(ValueError, match="cancellation diverged"):
            await replay_waiting_delayed_plant_from_records(broken)
        broken = deepcopy(records)
        waiting = next(row for row in broken if row["data"].get("phase") == "waiting")
        last_plant = [row for row in broken if row["kind"] == "plant"][-1]
        insertion = last_plant["seq"] + 1
        for row in broken:
            if row["seq"] >= insertion:
                row["seq"] += 1
        waiting["seq"] = insertion
        broken.sort(key=lambda row: row["seq"])
        with pytest.raises(ValueError, match="overlapping updates or task execution"):
            await replay_waiting_delayed_plant_from_records(broken)
        return
    replayed = await replay_waiting_delayed_plant_from_records(records)
    assert [row["boiler"]["data"]["demand"] for row in replayed["updates"]] == [100, 50, 75]
    if outcome == "recheck_rejected":
        assert replayed["cycle"]["command"] is None
        assert replayed["cycle"]["cycle"]["reason"] == "OFF - Heat request ended during valve delay"
    else:
        assert replayed["cycle"]["outcome"] == outcome
        assert replayed["cycle"]["commands"][0][1] == "turn_on"
        assert replayed["cycle"]["cycle"]["reason"] == "ON - Demand 75.0% (after valve delay)"
    broken = deepcopy(records)
    boilers = [row for row in broken if row["kind"] == "boiler"]
    boilers[1]["data"]["replay_before"]["last_change"] = -1
    tasks_before = asyncio.all_tasks()
    original_modules = relay_module.time, relay_module.asyncio, pwm_engine.time
    with pytest.raises(ValueError, match="divergent previous backend state"):
        await replay_waiting_delayed_plant_from_records(broken)
    await asyncio.sleep(0)
    assert asyncio.all_tasks() == tasks_before
    assert (relay_module.time, relay_module.asyncio, pwm_engine.time) == original_modules
    broken = deepcopy(records)
    next(row for row in broken if row["data"].get("phase") == "expired")["mono"] -= 1
    with pytest.raises(ValueError, match="premature delay"):
        await replay_waiting_delayed_plant_from_records(broken)
    broken = deepcopy(records)
    next(row for row in broken if row["kind"] == "boiler")["data"]["replay_trace"] = None
    with pytest.raises(ValueError):
        await replay_waiting_delayed_plant_from_records(broken)
    broken = deepcopy(records)
    next(row for row in broken if row["data"].get("phase") == "waiting")["data"]["phase"] = "missing"
    with pytest.raises(ValueError):
        await replay_waiting_delayed_plant_from_records(broken)
    broken = deepcopy(records)
    [row for row in broken if row["kind"] == "boiler"][1]["data"]["pwm_epoch"] = "different"
    with pytest.raises(ValueError):
        await replay_waiting_delayed_plant_from_records(broken)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [
    "already_off", "completed", "error", "cancelled", "overlap", "overlap_error", "overlap_cancelled",
])
async def test_waiting_delay_master_off_replays_real_emergency_path(recorder, outcome):
    """ASHRAE Addendum y §5.21.2 and §5.21.11: adapted emergency-off lifecycle."""
    from custom_components.multizone_thermostat import pwm_engine
    from custom_components.multizone_thermostat.boiler import relay_backend as relay_module
    from tests.mzt_replay.clock import ManualClock, patched_time

    clock = ManualClock(start=100)
    recorder._mono = clock.monotonic
    states = {"switch.boiler": "off", "switch.valve": "on"}
    master = {"on": True}
    hass = MagicMock()
    hass.states.get.side_effect = lambda entity: SimpleNamespace(state=states[entity], attributes={})
    async def call(domain, service, data, **kwargs):
        if outcome.startswith("overlap"):
            await asyncio.sleep(0)
        if outcome in ("error", "overlap_error"):
            raise RuntimeError("emergency service failed")
        if outcome in ("cancelled", "overlap_cancelled"):
            raise asyncio.CancelledError
        states[data["entity_id"]] = "off"
    hass.services.async_call = call
    hass.async_create_task = asyncio.create_task
    zones = [{"name": "A", "switch_entities": ["switch.valve"]}]
    agg = DemandAggregator(zones)
    cid = next(iter(agg.get_all_demands()))
    agg.set_zone_demand(cid, 100)
    diag = SimpleNamespace(_current_boiler_start=None, has_critical_sensor_fault=lambda _: False,
                           record_boiler_state=lambda _: None)
    coord = SimpleNamespace(
        zones=zones, _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: master["on"], plant_diagnostics=diag,
        get_zone_mode=agg.get_zone_mode, get_zone_demand=agg.get_zone_demand,
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    async def sleep(_):
        await asyncio.get_running_loop().create_future()
    shim = SimpleNamespace(sleep=sleep, current_task=asyncio.current_task, CancelledError=asyncio.CancelledError)
    with patch.object(relay_module, "asyncio", shim), patched_time(relay_module, clock), patched_time(pwm_engine, clock):
        backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 30)
        plant = PlantOrchestrator(hass, agg, backend, coord)
        await plant._async_update_boiler()
        task = backend._pending_boiler_task
        await asyncio.sleep(0)
        clock.advance(10)
        agg.set_zone_demand(cid, 75)
        await plant._async_update_boiler()
        master["on"] = False
        if outcome != "already_off":
            states["switch.boiler"] = "on"
        if outcome in ("error", "overlap_error"):
            with pytest.raises(RuntimeError, match="emergency service failed"):
                await plant._async_update_boiler()
        elif outcome in ("cancelled", "overlap_cancelled"):
            with pytest.raises(asyncio.CancelledError):
                await plant._async_update_boiler()
        else:
            await plant._async_update_boiler()
        await task
    records = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_waiting_delayed_plant_from_records(records)
    assert replayed["cancellation_source"] == "emergency_off"
    assert replayed["final_state"]["reason"] == "OFF - Emergency/Master OFF"
    assert replayed["final_state"]["delay_pending"] is False
    assert "emergency" in replayed["updates"][-1]
    broken = deepcopy(records)
    next(row for row in broken if row["data"].get("event") == "relay_emergency_off")["data"]["before"]["last_change"] += 1
    with pytest.raises(ValueError, match="divergent previous backend state"):
        await replay_waiting_delayed_plant_from_records(broken)
    broken = deepcopy(records)
    next(row for row in broken if row["data"].get("event") == "relay_emergency_off")["data"]["trace"] = []
    with pytest.raises(ValueError):
        await replay_waiting_delayed_plant_from_records(broken)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_outcome", [
    "completed", "cancel_by_update", "external_cancel", "master_off", "master_off_overlap",
])
async def test_sequential_waiting_plant_cycles_preserve_backend_continuity(recorder, first_outcome):
    """ASHRAE Addendum y §4.11 and §5.21.2.4: adapted delayed relay lifecycle."""
    from custom_components.multizone_thermostat import pwm_engine
    from custom_components.multizone_thermostat.boiler import relay_backend as relay_module
    from tests.mzt_replay.clock import ManualClock, patched_time

    clock = ManualClock(start=100)
    recorder._mono = clock.monotonic
    states = {"switch.boiler": "off", "switch.valve": "on"}
    master = {"on": True}
    hass = MagicMock()
    hass.states.get.side_effect = lambda entity: SimpleNamespace(state=states[entity], attributes={})
    async def call(domain, service, data, **kwargs):
        if first_outcome == "master_off_overlap" and service == "turn_off":
            await asyncio.sleep(0)
        states[data["entity_id"]] = "on" if service == "turn_on" else "off"
    hass.services.async_call = call
    hass.async_create_task = asyncio.create_task
    zones = [{"name": "A", "switch_entities": ["switch.valve"]}]
    agg = DemandAggregator(zones)
    cid = next(iter(agg.get_all_demands()))
    diag = SimpleNamespace(_current_boiler_start=None, has_critical_sensor_fault=lambda _: False,
                           record_boiler_state=lambda _: None)
    coord = SimpleNamespace(
        zones=zones, _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: master["on"], plant_diagnostics=diag,
        get_zone_mode=agg.get_zone_mode, get_zone_demand=agg.get_zone_demand,
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    waiters = []
    async def sleep(_):
        future = asyncio.get_running_loop().create_future()
        waiters.append(future)
        await future
    shim = SimpleNamespace(sleep=sleep, current_task=asyncio.current_task, CancelledError=asyncio.CancelledError)
    with patch.object(relay_module, "asyncio", shim), patched_time(relay_module, clock), patched_time(pwm_engine, clock):
        backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 30)
        plant = PlantOrchestrator(hass, agg, backend, coord)
        for index in range(2):
            agg.set_zone_demand(cid, 100)
            await plant._async_update_boiler()
            task = backend._pending_boiler_task
            await asyncio.sleep(0)
            clock.advance(10)
            agg.set_zone_demand(cid, 0 if index == 0 and first_outcome == "cancel_by_update" else 75)
            await plant._async_update_boiler()
            if index == 0 and first_outcome == "external_cancel":
                task.cancel()
            elif index == 0 and first_outcome in ("master_off", "master_off_overlap"):
                master["on"] = False
                if first_outcome == "master_off_overlap":
                    states["switch.boiler"] = "on"
                await plant._async_update_boiler()
                master["on"] = True
            elif index != 0 or first_outcome == "completed":
                clock.advance(20)
                waiters[-1].set_result(None)
            await task
            states["switch.boiler"] = "off"
            clock.advance(1)
    records = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_sequential_waiting_plant_from_records(records)
    assert len(replayed) == 2
    assert [len(row["updates"]) for row in replayed] == [
        3 if first_outcome in ("master_off", "master_off_overlap") else 2, 2,
    ]
    assert replayed[1]["cycle"]["commands"][0][1] == "turn_on"
    if first_outcome != "completed":
        expected = {"cancel_by_update": "update", "external_cancel": "unknown",
                    "master_off": "emergency_off", "master_off_overlap": "emergency_off"}
        assert replayed[0]["cancellation_source"] == expected[first_outcome]
    broken = deepcopy(records)
    [row for row in broken if row["kind"] == "boiler"][2]["data"]["replay_before"]["last_change"] += 1
    with pytest.raises(ValueError, match="divergent previous backend state"):
        await replay_sequential_waiting_plant_from_records(broken)
    broken = deepcopy(records)
    for row in broken:
        if row["data"].get("event") in ("relay_delay", "relay_delay_check", "relay_delay_command") and row["data"].get("step") == 1:
            row["data"]["epoch"] = "different"
    with pytest.raises(ValueError, match="crosses a backend or PWM epoch"):
        await replay_sequential_waiting_plant_from_records(broken)
    broken = deepcopy(records)
    second_task = [row for row in broken if row["data"].get("event") == "relay_delay"
                   and row["data"].get("phase") == "scheduled"][1]
    broken.remove(second_task)
    with pytest.raises(ValueError):
        await replay_sequential_waiting_plant_from_records(broken)
    broken = deepcopy(records)
    last_boiler = [row for row in broken if row["kind"] == "boiler"][-1]
    broken.remove(last_boiler)
    with pytest.raises(ValueError, match="incomplete boiler update"):
        await replay_sequential_waiting_plant_from_records(broken)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_linked_plant_replay_rejects_failed_boiler_updates(recorder, cancel):
    """ASHRAE Addendum y §5.21.11: adapted command failure and cancellation visibility."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off")
    hass.services.async_call = AsyncMock(side_effect=asyncio.CancelledError() if cancel else RuntimeError("failed"))
    agg = DemandAggregator([{"name": "A"}])
    agg.set_zone_demand(next(iter(agg.get_all_demands())), 100)
    coord = SimpleNamespace(
        zones=[], _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: True, plant_diagnostics=SimpleNamespace(
            _current_boiler_start=None, has_critical_sensor_fault=lambda _: False,
        ),
    )
    plant = PlantOrchestrator(hass, agg, RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0), coord)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await plant._async_update_boiler()
    rows = all_records(recorder)
    data = next(row["data"] for row in rows if row["kind"] == "boiler")
    assert not data["update_completed"] and data["update_cancelled"] is cancel
    assert data["update_error"] == (None if cancel else "failed")
    telemetry.set_recorder(None)
    with pytest.raises(ValueError, match="did not complete"):
        await replay_linked_plant_pwm_from_records(rows)
    replayed = await replay_linked_relay_plant_from_records(rows)
    assert replayed[0]["outcome"] == ("cancelled" if cancel else "error")
    assert replayed[0]["error"] == (None if cancel else "failed")
    broken = deepcopy(rows)
    trace = next(row["data"]["replay_trace"] for row in broken if row["kind"] == "boiler")
    next(event for event in trace if event["type"] == "service")["outcome"] = "completed"
    with pytest.raises(ValueError, match="incomplete or divergent"):
        await replay_linked_relay_plant_from_records(broken)


@pytest.mark.asyncio
async def test_linked_relay_rejects_failure_without_replay_context(recorder):
    """ASHRAE Addendum y §5.21.11: adapted unexplained failures must not replay as success."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off")
    agg = DemandAggregator([{"name": "A"}])
    agg.set_zone_demand(next(iter(agg.get_all_demands())), 100)
    coord = SimpleNamespace(
        zones=[], _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        get_master_state=lambda: True, plant_diagnostics=SimpleNamespace(
            _current_boiler_start=None, has_critical_sensor_fault=lambda _: False,
        ),
    )
    backend = RelayBoilerBackend(hass, coord, "switch.boiler", 0, 0, 0)
    backend._force_boiler_on = AsyncMock(side_effect=RuntimeError("untraced failure"))
    with pytest.raises(RuntimeError, match="untraced failure"):
        await PlantOrchestrator(hass, agg, backend, coord)._async_update_boiler()
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    with pytest.raises(ValueError, match="incomplete or divergent"):
        await replay_linked_relay_plant_from_records(rows)


def test_estimator_round_trip_and_missing_steps(recorder):
    observer = ThermalObserver("climate.a", temp_delta_threshold=0.1)
    for ts, temp, heating in [(100, 20, False), (200, 19.8, False), (300, 20, True), (400, 20.2, True)]:
        observer.update(temp, heating, datetime.fromtimestamp(ts, timezone.utc), outdoor_temp=5)
    rows = all_records(recorder)
    replayed = replay_estimator_from_records(rows, "climate.a")
    assert len(replayed) == 4
    for rec, heat, cool in replayed:
        assert heat == pytest.approx(rec["data"]["heating_rate"])
        assert cool == pytest.approx(rec["data"]["cooling_rate"])
    with pytest.raises(ValueError, match="missing or duplicate"):
        replay_estimator_from_records([rows[0], *rows[2:]], "climate.a")
    with pytest.raises(ValueError, match="fresh boundary"):
        replay_estimator_from_records(rows[1:], "climate.a")


def test_estimator_restored_state_replays_and_midstream_restore_rejects(recorder):
    observer = ThermalObserver("climate.a")
    observer.load_state({"estimator": "legacy", "heating_rate": 3, "cooling_rate": 2})
    observer.update(20, False, datetime.fromtimestamp(100, timezone.utc))
    observer.update(19.8, False, datetime.fromtimestamp(200, timezone.utc))
    rows = all_records(recorder)
    replayed = replay_estimator_from_records(rows, "climate.a")
    assert replayed[-1][1:] == pytest.approx((observer.heating_rate, observer.cooling_rate))
    observer.load_state({"estimator": "legacy", "heating_rate": 4, "cooling_rate": 5})
    observer.update(19.7, False, datetime.fromtimestamp(300, timezone.utc))
    with pytest.raises(ValueError, match="fresh boundary"):
        replay_estimator_from_records(all_records(recorder), "climate.a")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "unavailable", "error", "cancelled"])
async def test_stateful_opentherm_master_off_replays_emergency_lifecycle(recorder, outcome):
    """ASHRAE Addendum y §5.21.2 and §5.21.10: adapted master-off command lifecycle."""
    hass = MagicMock()
    observed = {"state": "off"}
    hass.states.get.side_effect = lambda _: SimpleNamespace(
        state=observed["state"], attributes={"temperature": 30}, domain="climate",
    )
    hass.services.async_call = AsyncMock()
    master = {"on": True}
    coord = SimpleNamespace(
        plant_diagnostics=None, weather_sensor_id=None, zones=[], _climate_entities={},
        _climate_callbacks={}, is_dhw_active=False, get_master_state=lambda: master["on"],
    )
    agg = DemandAggregator([{"name": "A"}])
    agg.set_zone_demand(next(iter(agg.get_all_demands())), 100)
    backend = OpenThermBoilerBackend(hass, coord, "climate.boiler", 35, 75)
    plant = PlantOrchestrator(hass, agg, backend, coord)
    await plant._async_update_boiler()
    master["on"] = False
    observed["state"] = "unavailable" if outcome == "unavailable" else "heat"
    if outcome == "error":
        hass.services.async_call.side_effect = RuntimeError("off service failed")
    elif outcome == "cancelled":
        hass.services.async_call.side_effect = asyncio.CancelledError
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await plant._async_update_boiler()
    else:
        await plant._async_update_boiler()
    hass.services.async_call.side_effect = None
    observed["state"] = "off"
    master["on"] = True
    await plant._async_update_boiler()
    records = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_stateful_opentherm_from_records(records)
    assert len(replayed) == 3
    linked = await replay_linked_opentherm_plant_from_records(records)
    assert [row["action"] for row in linked] == ["update", "emergency_off", "update"]
    emergency = replayed[1]
    assert emergency["emergency_start"]["data"]["step"] == 1
    assert emergency["emergency_end"]["data"]["cancelled"] is (outcome == "cancelled")
    assert emergency["observation"]["data"]["reason_before"] == "OFF - Emergency/Master OFF"
    assert emergency["result"]["cancelled"] is (outcome == "cancelled")
    if outcome == "error":
        assert emergency["observation"]["data"]["command_error"] == "off service failed"
    broken = deepcopy(records)
    next(row for row in broken if row["data"].get("event") == "opentherm_emergency_start")["data"]["reason"] = "forged"
    with pytest.raises(ValueError, match="divergent previous backend state"):
        await replay_stateful_opentherm_from_records(broken)
    broken = deepcopy(records)
    next(row for row in broken if row["data"].get("event") == "opentherm_emergency_end")["data"]["reason"] = "forged"
    with pytest.raises(ValueError, match="emergency-off diverged"):
        await replay_stateful_opentherm_from_records(broken)
    broken = [row for row in records if row["data"].get("event") != "opentherm_emergency_end"]
    with pytest.raises(ValueError):
        await replay_stateful_opentherm_from_records(broken)
    broken = [row for row in records if not (row["kind"] == "plant" and row["data"]["action"] == "master_off")]
    with pytest.raises(ValueError, match="unmatched decisions or operations"):
        await replay_linked_opentherm_plant_from_records(broken)
    broken = deepcopy(records)
    [row for row in broken if row["kind"] == "plant"][1]["data"]["action"] = "anti_seize"
    with pytest.raises(ValueError, match="mismatched backend action"):
        await replay_linked_opentherm_plant_from_records(broken)
    broken = deepcopy(records)
    first_end = next(row for row in broken if row["data"].get("event") == "opentherm_update_end")
    second_plant = [row for row in broken if row["kind"] == "plant"][1]
    first_end["seq"], second_plant["seq"] = second_plant["seq"], first_end["seq"]
    broken.sort(key=lambda row: row["seq"])
    with pytest.raises(ValueError, match="overlapping or reordered operations"):
        await replay_linked_opentherm_plant_from_records(broken)


@pytest.mark.asyncio
async def test_stateful_opentherm_rejects_emergency_off_overlapping_update(recorder):
    """ASHRAE Addendum y §5.21.10: adapted interrupted-command observability."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="heat", attributes={"temperature": 30}, domain="climate")
    gate = asyncio.get_running_loop().create_future()
    entered = asyncio.Event()
    async def service(*args, **kwargs):
        entered.set()
        await gate
    hass.services.async_call = service
    backend = OpenThermBoilerBackend(
        hass, SimpleNamespace(plant_diagnostics=None, weather_sensor_id=None), "climate.boiler", 35, 75,
    )
    update = asyncio.create_task(backend.async_update(50, "A", False))
    await entered.wait()
    emergency = asyncio.create_task(backend.async_emergency_off())
    await asyncio.sleep(0)
    gate.set_result(None)
    await asyncio.gather(update, emergency)
    records = all_records(recorder)
    telemetry.set_recorder(None)
    with pytest.raises(ValueError, match="non-overlapping start"):
        await replay_stateful_opentherm_from_records(records)


@pytest.mark.asyncio
async def test_stateful_opentherm_replays_updates_and_rejects_missing_boundaries(recorder):
    """ASHRAE Addendum y §5.21.10 and §5.21.11: adapted command lifecycle replay."""
    hass = MagicMock()
    observed = {"state": "off"}
    hass.states.get.side_effect = lambda _: SimpleNamespace(
        state=observed["state"], attributes={"temperature": 30}, domain="climate",
    )
    hass.services.async_call = AsyncMock()
    backend = OpenThermBoilerBackend(
        hass, SimpleNamespace(plant_diagnostics=None, weather_sensor_id=None), "climate.boiler", 35, 75,
    )
    await backend.async_update(50, "A", False)
    observed["state"] = "unavailable"
    await backend.async_update(0, "A", False)
    observed["state"] = "off"
    hass.services.async_call.side_effect = RuntimeError("gateway unavailable")
    await backend.async_update(50, "A", False)
    hass.services.async_call.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await backend.async_update(50, "A", False)
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_stateful_opentherm_from_records(rows)
    assert [row["start"]["data"]["step"] for row in replayed] == [0, 1, 2, 3]
    assert [row["observation"]["data"]["action"] for row in replayed] == [
        "update", "unavailable", "update", "update",
    ]
    assert replayed[-1]["result"]["cancelled"]
    broken = deepcopy(rows)
    broken = [row for row in broken if row["data"].get("step") != 1
              and not (row["kind"] == "opentherm" and row["data"]["action"] == "unavailable")]
    with pytest.raises(ValueError, match="missing or duplicate update step"):
        await replay_stateful_opentherm_from_records(broken)
    broken = deepcopy(rows)
    last = [row for row in broken if row["kind"] == "opentherm"][-1]
    last["data"]["reason_before"] = "forged"
    with pytest.raises(ValueError, match="divergent previous backend state"):
        await replay_stateful_opentherm_from_records(broken)
    broken = deepcopy(rows)
    broken.pop()
    with pytest.raises(ValueError, match="incomplete update"):
        await replay_stateful_opentherm_from_records(broken)
    broken = deepcopy(rows)
    [row for row in broken if row["data"].get("event") == "opentherm_update_end"][0]["data"]["reason"] = "forged"
    with pytest.raises(ValueError, match="diverged during stateful replay"):
        await replay_stateful_opentherm_from_records(broken)


@pytest.mark.asyncio
async def test_stateful_opentherm_rejects_overlapping_updates_and_unlogged_steps(recorder):
    """ASHRAE Addendum y §5.21.10: adapted observation of overlapping commands."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off", attributes={"temperature": 30}, domain="climate")
    gate = asyncio.get_running_loop().create_future()
    entered = asyncio.Event()
    async def service(*args, **kwargs):
        entered.set()
        await gate
    hass.services.async_call = service
    backend = OpenThermBoilerBackend(
        hass, SimpleNamespace(plant_diagnostics=None, weather_sensor_id=None), "climate.boiler", 35, 75,
    )
    first = asyncio.create_task(backend.async_update(50, "A", False))
    await entered.wait()
    second = asyncio.create_task(backend.async_update(50, "A", False))
    await asyncio.sleep(0)
    gate.set_result(None)
    await asyncio.gather(first, second)
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    with pytest.raises(ValueError, match="overlapping or incomplete updates"):
        await replay_stateful_opentherm_from_records(rows)
    backend = OpenThermBoilerBackend(
        hass, SimpleNamespace(plant_diagnostics=None, weather_sensor_id=None), "climate.boiler", 35, 75,
    )
    telemetry.set_recorder(recorder)
    await backend.async_update(50, "A", False)
    telemetry.set_recorder(None)
    await backend.async_update(50, "A", False)
    telemetry.set_recorder(recorder)
    await backend.async_update(50, "A", False)
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    rows = rows[next(i for i, row in enumerate(rows)
                     if row["data"].get("epoch") == backend._replay_epoch):]
    with pytest.raises(ValueError, match="missing or duplicate update step"):
        await replay_stateful_opentherm_from_records(rows)


@pytest.mark.asyncio
async def test_opentherm_target_replay_from_real_records(recorder):
    """ASHRAE Addendum y §5.21.4: adapted mapping, not trim-and-respond."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get.side_effect = lambda entity: (
        SimpleNamespace(state="4", attributes={}) if entity == "sensor.outdoor"
        else SimpleNamespace(state="heat", attributes={"temperature": 40})
    )
    coord = SimpleNamespace(
        plant_diagnostics=None, weather_sensor_id="sensor.outdoor",
        get_persistent_data=lambda *_: 1.2,
        _thermal_models={"a": SimpleNamespace(cooling_rate=0.4), "b": SimpleNamespace(cooling_rate=0.2)},
    )
    backend = OpenThermBoilerBackend(hass, coord, "climate.boiler", 35, 75)
    for demand, frost in [(0, False), (20, False), (80, False), (100, True)]:
        await backend.async_update(demand, "A", frost)
    rows = all_records(recorder)
    replayed = await replay_opentherm_targets_from_records(rows)
    assert len(replayed) == 4
    for row, target in replayed:
        assert target == row["data"]["computed_target_temp"]
    assert replayed[1][0]["data"]["weather_cooling_rates"] == [0.4, 0.2]
    telemetry.set_recorder(None)
    command_replay = await replay_opentherm_commands_from_records(rows)
    assert len(command_replay) == 4
    assert command_replay[-1][1]["reason"] == command_replay[-1][0]["data"]["reason"]
    del next(row for row in rows if row["kind"] == "opentherm")["data"]["weather_cooling_rates"]
    with pytest.raises(ValueError, match="missing replay fields"):
        await replay_opentherm_targets_from_records(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("entity,state,attrs,ch_state,demand,fail", [
    ("climate.boiler", "off", {"temperature": 30}, None, 50, False),
    ("climate.boiler", "off", {"temperature": 30}, None, 50, True),
    ("number.boiler", "50", {"min": 10}, None, 0, False),
    ("number.boiler", "50", {"min": 10}, "on", 0, False),
    ("water_heater.boiler", "off", {"temperature": 40}, None, 25, False),
    ("climate.boiler", "unavailable", {}, None, 50, False),
])
async def test_opentherm_command_replay_uses_real_inputs_and_outcomes(recorder, entity, state, attrs, ch_state, demand, fail):
    """ASHRAE Addendum y §5.21.10: adapted replay of commands, not physical feedback."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("failed") if fail else None)
    hass.states.get.side_effect = lambda name: (
        SimpleNamespace(state=ch_state, attributes={}, domain="switch") if name == "switch.ch"
        else SimpleNamespace(state=state, attributes=attrs, domain=entity.split(".")[0])
    )
    coord = SimpleNamespace(plant_diagnostics=None, weather_sensor_id=None)
    backend = OpenThermBoilerBackend(hass, coord, entity, 35, 75, "switch.ch" if ch_state else None)
    await backend.async_update(demand, "A", False)
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    result = (await replay_opentherm_commands_from_records(rows))[0][1]
    rows = [row for row in rows if row["kind"] == "opentherm"]
    assert result["reason"] == backend.status_reason
    assert result["pending_command"] == backend._pending_command
    assert len(result["commands"]) == hass.services.async_call.await_count
    if fail:
        assert rows[0]["data"]["command_error"] == "failed"
    if rows[0]["data"]["trace"]:
        broken = dict(rows[0], data=dict(rows[0]["data"], trace=rows[0]["data"]["trace"][:-1]))
        with pytest.raises(ValueError):
            await replay_opentherm_commands_from_records([broken])


@pytest.mark.asyncio
async def test_opentherm_replay_preserves_command_cancellation(recorder):
    """ASHRAE Addendum y §5.21.10: adapted interrupted command replay."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off", attributes={"temperature": 30}, domain="climate")
    hass.services.async_call = AsyncMock(side_effect=asyncio.CancelledError)
    backend = OpenThermBoilerBackend(hass, SimpleNamespace(plant_diagnostics=None, weather_sensor_id=None),
                                    "climate.boiler", 35, 75)
    with pytest.raises(asyncio.CancelledError):
        await backend.async_update(50, "A", False)
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_opentherm_commands_from_records(rows)
    assert replayed[0][1]["cancelled"]


@pytest.mark.asyncio
async def test_opentherm_command_replay_preserves_pending_timeout_and_recovery(recorder):
    """ASHRAE Addendum y §5.21.10 and §5.21.11: adapted command-timeout observation."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off", attributes={"temperature": 30}, domain="climate")
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("gateway unavailable"))
    coord = SimpleNamespace(
        plant_diagnostics=SimpleNamespace(
            get_opentherm_diagnostics=lambda: {"flame_active": False},
            _current_boiler_start=None, record_boiler_state=lambda _: None,
        ),
        weather_sensor_id=None, record_diagnostic_event=MagicMock(),
    )
    backend = OpenThermBoilerBackend(hass, coord, "climate.boiler", 35, 75)
    backend._pending_command = ("on", 0)
    await backend.async_update(50, "A", False)
    hass.services.async_call.side_effect = None
    await backend.async_update(50, "A", False)
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    results = await replay_opentherm_commands_from_records(rows)
    rows = [row for row in rows if row["kind"] == "opentherm"]
    for row, result in results:
        assert result["reason"] == row["data"]["reason"]
        expected = row["data"]["pending_command"]
        assert result["pending_command"] == (tuple(expected) if expected is not None else None)
    assert rows[0]["data"]["command_error"] == "gateway unavailable"
    assert rows[1]["data"]["update_completed"]
    coord.record_diagnostic_event.assert_called_once()


def test_opentherm_dhw_diagnostic_sources_are_recorded(recorder):
    """ASHRAE Addendum y §5.21.11: adapted diagnostic inputs, not fault conformance."""
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(device_id="device")
    companions = [
        SimpleNamespace(entity_id="binary_sensor.boiler_dhw", original_device_class=None, device_class=None),
        SimpleNamespace(entity_id="sensor.boiler_fault", original_device_class=None, device_class=None),
    ]
    hass = MagicMock()
    hass.states.get.side_effect = lambda entity: {
        "climate.boiler": SimpleNamespace(state="heat", attributes={"flame_active": False, "temperature": 42}),
        "binary_sensor.boiler_dhw": SimpleNamespace(state="on"),
        "sensor.boiler_fault": SimpleNamespace(state="unknown"),
    }.get(entity)
    with patch("custom_components.multizone_thermostat.plant_diagnostics.anomalies.er.async_get", return_value=registry), \
         patch("custom_components.multizone_thermostat.plant_diagnostics.anomalies.er.async_entries_for_device",
               return_value=companions):
        result = get_opentherm_diagnostics(SimpleNamespace(opentherm_entity="climate.boiler"), hass)
    row = all_records(recorder)[0]["data"]
    assert result["dhw_active"] and not result["fault_active"]
    assert row["result"] == result
    assert [(item["entity"], item["state"]) for item in row["observed"]] == [
        ("climate.boiler", "heat"), ("binary_sensor.boiler_dhw", "on"), ("sensor.boiler_fault", "unknown"),
    ]
    records = all_records(recorder)
    telemetry.set_recorder(None)
    assert replay_opentherm_diagnostics_from_records(records)[0][1] == result


def test_external_sensor_raw_unavailable_and_invalid(recorder):
    zone = MagicMock()
    zone.entity_id = "climate.a"
    zone._temp_sensor = "sensor.a"
    for state in ("unknown", "bad", "20.5"):
        MultizoneVirtualThermostat._on_temp_changed(zone, SimpleNamespace(data={"new_state": SimpleNamespace(state=state)}))
    rows = all_records(recorder)
    assert [row["data"]["state"] for row in rows] == ["unknown", "bad", "20.5"]
    assert all(row["data"]["entity"] == "sensor.a" for row in rows)


def test_trv_sensor_event_uses_read_only_mapping(recorder):
    from types import MappingProxyType

    zone = MagicMock()
    zone.entity_id = "climate.a"
    zone._temp_sensor = "sensor.a"
    zone._safety_fallback_active = False
    state = SimpleNamespace(state="heat", attributes=MappingProxyType({"current_temperature": 18.5}))
    event = SimpleNamespace(data={"entity_id": "climate.trv", "new_state": state})
    MultizoneVirtualThermostat._on_trv_changed_wrapper(zone, event)
    row = all_records(recorder)[0]["data"]
    assert row == {"source": "trv", "entity": "climate.trv", "state": "heat", "temperature": 18.5}


def test_window_event_includes_unavailable_and_removal(recorder):
    manager = SimpleNamespace(zones=[])
    for state in ("on", "unavailable", None):
        ZoneManager.async_on_window_state_changed(manager, SimpleNamespace(data={
            "entity_id": "binary_sensor.window", "new_state": None if state is None else SimpleNamespace(state=state),
        }))
    rows = [r["data"] for r in all_records(recorder)]
    assert [r["state"] for r in rows] == ["on", "unavailable", None]
    assert all(r["source"] == "window" for r in rows)


@pytest.mark.asyncio
async def test_climate_event_and_entry_update_records(recorder):
    from types import MappingProxyType

    coord = SimpleNamespace(_async_refresh_zone=AsyncMock())
    state = SimpleNamespace(state="heat", attributes=MappingProxyType({
        "current_temperature": 20, "temperature": 21,
    }))
    await MultizoneCoordinator._async_on_climate_state_changed(
        coord, SimpleNamespace(data={"entity_id": "climate.a", "new_state": state}),
    )
    hass = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    entry = SimpleNamespace(entry_id="entry", data={"boiler_mode": "relay"}, options={"private": "secret"})
    await _async_update_listener(hass, entry)
    rows = [r["data"] for r in all_records(recorder)]
    assert rows[0] == {"source": "zone_climate", "state": "heat", "temperature": 20, "target": 21}
    assert rows[1] == {"source": "entry_update", "entry_id": "entry", "data_keys": ["boiler_mode"],
                       "option_keys": ["private"], "data": {"boiler_mode": "relay"}, "options": {}}
    assert "secret" not in repr(rows)
    coord._async_refresh_zone.assert_awaited_once()
    hass.config_entries.async_reload.assert_awaited_once_with("entry")


def test_configured_input_listener_records_initial_and_changes(recorder):
    coord = MagicMock(spec=MultizoneCoordinator)
    coord.boiler_switch = "switch.boiler"
    coord.opentherm_entity = None
    coord.opentherm_ch_switch = None
    coord.weather_sensor_id = "sensor.outside"
    coord.presence_sensor = None
    coord.zones = [{"name": "A", "temp_sensor": "sensor.room", "switch_entities": ["switch.valve"],
                    "climate_id": "climate.custom",
                    "calibrations": {"climate.trv": "number.calibration"}}]
    coord._unsub_listeners = []
    coord._telemetry_input_epoch = "epoch"
    coord._telemetry_input_step = 0
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = SimpleNamespace(
        state="12", attributes={"temperature": 12, "private": "do not log", "hvac_modes": ["heat", "off"]},
    )
    coord._record_telemetry_input = lambda entity, state, source, **kwargs: (
        MultizoneCoordinator._record_telemetry_input(coord, entity, state, source, **kwargs)
    )
    coord._record_telemetry_input_event = lambda event: (
        MultizoneCoordinator._record_telemetry_input_event(coord, event)
    )
    with patch("custom_components.multizone_thermostat.coordinator.async_track_state_change_event") as tracker:
        MultizoneCoordinator._start_telemetry_inputs(coord)
    tracked = tracker.call_args.args[1]
    assert tracked == ["climate.custom", "climate.multizone_thermostat_a", "number.calibration", "sensor.outside", "sensor.room",
                       "switch.boiler", "switch.valve"]
    coord._record_telemetry_input_event(SimpleNamespace(data={
        "entity_id": "sensor.room", "old_state": coord.hass.states.get.return_value, "new_state": None,
    }))
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    restored = SimpleNamespace(state="unavailable", attributes={}, last_changed=timestamp, last_updated=timestamp)
    coord._record_telemetry_input_event(SimpleNamespace(
        data={"entity_id": "sensor.room", "old_state": None, "new_state": restored}, time_fired=timestamp,
    ))
    coord.hass.states.get.return_value.attributes["hvac_modes"].append("private-after-recording")
    rows = [r["data"] for r in all_records(recorder) if r["kind"] == "sensor"]
    assert len(rows) == 9 and rows[0]["observation"] == "initial"
    assert rows[-2]["observation"] == "change" and rows[-2]["state"] is None
    assert rows[-1]["state"] == "unavailable" and rows[-1]["event_time"] == timestamp.isoformat()
    assert all("private" not in repr(row) for row in rows)
    records = all_records(recorder)
    history = reconstruct_configured_inputs(records)
    assert history[-2]["entity"] == "sensor.room" and not history[-2]["state"]["present"]
    assert history[-1]["state"]["last_updated"] == timestamp.isoformat()
    missing = [row for row in records if row["data"].get("step") != 2]
    with pytest.raises(ValueError, match="missing step"):
        reconstruct_configured_inputs(missing)
    changed = deepcopy(records)
    last = next(r for r in changed if r["data"].get("observation") == "change")
    last["data"]["old"]["state"] = "wrong"
    with pytest.raises(ValueError, match="previous state"):
        reconstruct_configured_inputs(changed)


def test_control_config_snapshot_copies_calibrations_and_presence_without_unrelated_data():
    config = {
        "presence_sensor": "person.home", "geofencing_enabled": True, "private": "secret",
        "zones": [{"name": "A", "climate_id": "climate.custom",
                   "calibrations": {"climate.trv": "number.calibration"}}],
    }
    snapshot = snapshot_control_config(config)
    config["zones"][0]["calibrations"]["climate.trv"] = "number.changed"
    assert snapshot["presence_sensor"] == "person.home"
    assert snapshot["geofencing_enabled"] is True
    assert snapshot["zones"][0]["climate_id"] == "climate.custom"
    assert snapshot["zones"][0]["calibrations"] == {"climate.trv": "number.calibration"}
    assert "secret" not in repr(snapshot)


def test_sensor_history_preserves_nonfinite_values(recorder):
    coord = object.__new__(MultizoneCoordinator)
    coord._telemetry_input_epoch, coord._telemetry_input_step = "epoch", 0
    initial = SimpleNamespace(state="heat", attributes={"current_temperature": math.nan})
    changed = SimpleNamespace(state="unavailable", attributes={"current_temperature": math.inf})
    recorder.record(telemetry.KIND_EVENT, None, event="configured_inputs_start",
                    epoch="epoch", entities=["climate.a"])
    coord._record_telemetry_input("climate.a", initial, "initial")
    recorder.record(telemetry.KIND_EVENT, None, event="configured_inputs_ready",
                    epoch="epoch", next_step=1)
    coord._record_telemetry_input("climate.a", changed, "change", old_state=initial)
    history = reconstruct_configured_inputs(all_records(recorder))
    assert math.isnan(history[0]["state"]["attributes"]["current_temperature"])
    assert history[1]["state"]["attributes"]["current_temperature"] == math.inf


def test_runtime_control_history_tracks_actual_setting_changes(recorder):
    coord = object.__new__(MultizoneCoordinator)
    coord._telemetry_runtime_epoch = "epoch"
    coord._telemetry_runtime_step = 0
    coord._min_cycle_on, coord._min_cycle_off, coord._valve_delay = 5, 5, 0
    coord._derivative_window_min, coord._derivative_limit_pct, coord._master_state = 15, 15, False
    coord.zone_controller = SimpleNamespace(pids={})
    coord.boiler_backend = MagicMock()
    initial = {"min_cycle_on": 5, "min_cycle_off": 5, "valve_opening_delay": 0,
               "derivative_window_min": 15, "derivative_limit_pct": 15, "master_enabled": False}
    recorder.record(telemetry.KIND_CONFIG, None, source="startup", runtime_epoch="epoch",
                    runtime_next_step=0, runtime_settings=initial)
    coord.set_min_cycle_on(10)
    coord.set_derivative_window(8)
    coord.set_master_state(True)
    rows = all_records(recorder)
    history = reconstruct_runtime_control_settings(rows)
    assert history[-1]["state"]["min_cycle_on"] == 10
    assert history[-1]["state"]["derivative_window_min"] == 8
    assert history[-1]["state"]["master_enabled"] is True
    altered = deepcopy(rows)
    next(r for r in altered if r["data"].get("key") == "derivative_window_min")["data"]["previous"] = 99
    with pytest.raises(ValueError, match="previous value"):
        reconstruct_runtime_control_settings(altered)
    with pytest.raises(ValueError, match="missing"):
        reconstruct_runtime_control_settings([r for r in rows if r["data"].get("step") != 1])


@pytest.mark.asyncio
async def test_persistent_setting_record_is_after_save_and_does_not_log_complex_data(recorder):
    coordinator = SimpleNamespace(_settings={}, _settings_store=SimpleNamespace(async_save=AsyncMock()))
    await MultizoneCoordinator.async_set_persistent_data(coordinator, "frost_temperature", 5.0)
    await MultizoneCoordinator.async_set_persistent_data(coordinator, "private", {"token": "secret"})
    rows = [r["data"] for r in all_records(recorder) if r["data"].get("source") == "persistent_setting"]
    assert rows[0]["value"] == 5.0
    assert rows[1] == {"source": "persistent_setting", "key": "private", "value_recorded": False}
    assert "secret" not in repr(rows)
    coordinator._settings_store.async_save.side_effect = RuntimeError("save failed")
    with pytest.raises(RuntimeError):
        await MultizoneCoordinator.async_set_persistent_data(coordinator, "test", 1)
    records = all_records(recorder)
    assert len([r for r in records if r["data"].get("source") == "persistent_setting"]) == 2
    assert records[-1]["data"]["completed"] is False
    assert coordinator._settings["test"] == 1


@pytest.mark.asyncio
async def test_setting_history_tracks_mutation_before_failure_and_snapshots(recorder):
    coord = SimpleNamespace(_settings={"frost_temperature": 4.0}, _settings_store=SimpleNamespace(
        async_save=AsyncMock(),
    ), _telemetry_settings_epoch="epoch", _telemetry_settings_step=0)
    recorder.record(telemetry.KIND_CONFIG, None, source="startup", settings_epoch="epoch",
                    settings_next_step=0, settings={"frost_temperature": 4.0})
    await MultizoneCoordinator.async_set_persistent_data(coord, "frost_temperature", 5.0)
    modes = {"climate.a": "primary"}
    await MultizoneCoordinator.async_set_persistent_data(coord, "zone_modes", modes)
    modes["climate.a"] = "bypass"
    coord._settings_store.async_save.side_effect = RuntimeError("private failure")
    with pytest.raises(RuntimeError):
        await MultizoneCoordinator.async_set_persistent_data(coord, "frost_temperature", 6.0)
    rows = all_records(recorder)
    history = reconstruct_persistent_settings(rows)
    assert history[0]["state"]["frost_temperature"]["value"] == 4.0
    assert [item["current"]["value"] for item in history[1:]] == [
        5.0, {"climate.a": "primary"}, 6.0,
    ]
    assert history[-1]["save"] == {"completed": False, "cancelled": False, "error": "RuntimeError"}
    assert "private failure" not in repr(rows)
    missing = [r for r in rows if not (r["data"].get("source") == "persistent_setting_mutation"
                                       and r["data"].get("step") == 1)]
    with pytest.raises(ValueError, match="missing step|outcome"):
        reconstruct_persistent_settings(missing)


def test_preset_checkpoint_and_mutations_reconstruct(recorder):
    coord = MagicMock()
    coord.zones = []
    coord._select_entities = {}
    coord._climate_callbacks = {}
    coord.get_persistent_data.return_value = {}
    hass = MagicMock()
    manager = ZoneManager(hass, coord)
    manager._presets = {"eco": {"climate.a": {"target_temp": 18.0, "private": "secret"}}}
    manager.record_preset_checkpoint()
    manager.set_global_preset("eco")
    manager.update_preset_temperature("eco", "climate.a", 19.0)
    manager.set_zone_mode("climate.a", "bypass")
    rows = all_records(recorder)
    history = reconstruct_preset_history(rows)
    assert history[-1]["state"]["presets"]["eco"]["climate.a"] == {
        "target_temp": 19.0, "mode": "bypass",
    }
    assert history[-1]["state"]["current_preset"] == "eco"
    assert history[-1]["state"]["zone_modes"]["climate.a"] == "bypass"
    assert "secret" not in repr(rows)
    missing = [r for r in rows if not (r["data"].get("source") == "preset_change"
                                       and r["data"].get("step") == 1)]
    with pytest.raises(ValueError, match="missing step"):
        reconstruct_preset_history(missing)


def test_window_override_history_tracks_open_and_unavailable(recorder):
    coord = MagicMock()
    coord.zones = [{"name": "A", "window_sensor": "binary_sensor.window"}]
    coord._select_entities = {}
    coord._climate_callbacks = {}
    coord.get_persistent_data.return_value = {}
    coord.is_zone_in_frost_emergency.return_value = False
    manager = ZoneManager(MagicMock(), coord)
    manager.record_preset_checkpoint()
    for state in ("on", "unavailable"):
        manager.async_on_window_state_changed(SimpleNamespace(data={
            "entity_id": "binary_sensor.window", "new_state": SimpleNamespace(state=state),
        }))
    history = reconstruct_preset_history(all_records(recorder))
    zone = "climate.multizone_thermostat_a"
    assert history[1]["state"]["pre_window_state"] == {zone: "primary"}
    assert history[2]["state"]["zone_modes"][zone] == "bypass"
    assert history[-1]["state"]["pre_window_state"] == {}
    assert history[-1]["state"]["zone_modes"][zone] == "primary"


@pytest.mark.asyncio
async def test_setting_history_handles_interleaved_saves_and_cancellation(recorder):
    gate, entered = asyncio.Event(), asyncio.Event()

    async def save(_):
        entered.set()
        await gate.wait()

    coord = SimpleNamespace(_settings={}, _settings_store=SimpleNamespace(async_save=save),
                            _telemetry_settings_epoch="epoch", _telemetry_settings_step=0)
    recorder.record(telemetry.KIND_CONFIG, None, source="startup", settings_epoch="epoch",
                    settings_next_step=0, settings={})
    first = asyncio.create_task(MultizoneCoordinator.async_set_persistent_data(coord, "frost_temperature", 5))
    await entered.wait()
    second = asyncio.create_task(MultizoneCoordinator.async_set_persistent_data(coord, "frost_temperature", 6))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    gate.set()
    await first
    history = reconstruct_persistent_settings(all_records(recorder))
    assert [item["current"]["value"] for item in history[1:]] == [5, 6]
    assert history[1]["save"]["completed"] is True
    assert history[2]["save"]["cancelled"] is True


@pytest.mark.asyncio
async def test_zone_mode_direct_write_and_save_are_both_in_setting_history(recorder):
    coord = object.__new__(MultizoneCoordinator)
    coord.zones = []
    coord._settings = {"zone_modes": {"climate.a": "primary"}}
    coord._settings_store = SimpleNamespace(async_save=AsyncMock())
    coord._telemetry_settings_epoch, coord._telemetry_settings_step = "epoch", 0
    coord._climate_callbacks = {}
    coord.demand_aggregator = MagicMock()
    coord._async_save_presets_storage = AsyncMock()
    tasks = []
    hass = SimpleNamespace(async_create_task=lambda coro: tasks.append(asyncio.create_task(coro)))
    manager = ZoneManager(hass, coord)
    recorder.record(telemetry.KIND_CONFIG, None, source="startup", settings_epoch="epoch",
                    settings_next_step=0, settings={"zone_modes": {"climate.a": "primary"}})
    manager.set_zone_mode("climate.a", "bypass", save_to_preset=False)
    await asyncio.gather(*tasks)
    history = reconstruct_persistent_settings(all_records(recorder))
    assert history[1]["current"]["value"] == {"climate.a": "bypass"}
    assert history[1]["save"] == {"scheduled_separately": True}
    assert history[2]["save"]["completed"] is True

@pytest.mark.asyncio
async def test_schedule_records_are_snapshots_after_successful_save(recorder):
    coord = SimpleNamespace(_settings={}, _settings_store=SimpleNamespace(async_save=AsyncMock()))
    manager = ScheduleProfileManager(coord)
    manager.profiles["standard"]["baseline_preset"] = "comfort"
    await manager.async_save_to_storage()
    manager.profiles["standard"]["baseline_preset"] = "eco"
    row = all_records(recorder)[0]["data"]
    assert row["source"] == "schedule_saved"
    assert row["schedule"]["profiles"]["standard"]["baseline_preset"] == "comfort"
    coord._settings_store.async_save.side_effect = RuntimeError("save failed")
    with pytest.raises(RuntimeError):
        await manager.async_save_to_storage()
    assert len(all_records(recorder)) == 1


@pytest.mark.asyncio
async def test_schedule_evaluation_observes_overrides_and_smart_start(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    hass = MagicMock()
    hass.states.get.return_value = None
    coord = SimpleNamespace(hass=hass, zones=[{"name": "A", "climate_id": "climate.a"}],
                            _climate_entities={}, _climate_callbacks={})
    manager = ScheduleProfileManager(coord)
    manager.scheduler_enabled = True
    manager.manual_overrides["climate.a"] = {"target_temp": 19, "until": (now - timedelta(minutes=1)).isoformat()}
    manager.evaluate_zone_slot = lambda *_: {"slot_id": "slot", "target_temp": 18}
    manager.get_upcoming_blocks = lambda *_args, **_kwargs: [{
        "start_dt": now + timedelta(minutes=30), "target_temp": 22, "preset": "comfort",
    }]
    manager._get_zone_current_temp = lambda _: 20
    manager._get_zone_heating_rate = lambda _: 2
    manager._notify_climates = lambda: None
    await manager.async_evaluate_schedule(now)
    manager.scheduler_enabled = False
    await manager.async_evaluate_schedule(now + timedelta(minutes=1))
    records = all_records(recorder)
    rows = [row["data"] for row in records if row["data"].get("event") == "schedule_evaluation"]
    assert len(rows) == 2
    first, disabled = rows
    assert first["before"]["manual_overrides"]["climate.a"]["target_temp"] == 19
    assert first["after"]["manual_overrides"] == {}
    assert first["zones"][0]["slot"]["target_temp"] == 18
    assert first["zones"][0]["temperature"] == 20
    assert first["zones"][0]["heating_rate"] == 2
    assert first["after"]["schedule_overrides"]["climate.a"] == 22
    assert first["after"]["smart_start_info"]["climate.a"]["source"] == "schedule"
    assert disabled["reason"] == "disabled"
    assert disabled["before"]["schedule_overrides"]["climate.a"] == 22
    assert disabled["after"]["schedule_overrides"] == {}
    telemetry.set_recorder(None)
    replayed = await replay_schedule_evaluations_from_records(records)
    assert [state for _, state in replayed] == [first["after"], disabled["after"]]
    broken = deepcopy(records)
    event = next(row["data"] for row in broken if row["data"].get("event") == "schedule_evaluation")
    del event["zones"][0]["heating_rate"]
    with pytest.raises(ValueError, match="lacks heating_rate"):
        await replay_schedule_evaluations_from_records(broken)
    broken = deepcopy(records)
    event = next(row["data"] for row in broken if row["data"].get("event") == "schedule_evaluation")
    event["after"]["schedule_overrides"]["climate.a"] = 17
    with pytest.raises(ValueError, match="diverged during replay"):
        await replay_schedule_evaluations_from_records(broken)
    broken = deepcopy(records)
    event = next(row["data"] for row in broken if row["data"].get("event") == "schedule_evaluation")
    event["before"]["smart_return"] = {"arrival_time": now.isoformat()}
    del event["smart_return_plan"]
    with pytest.raises(ValueError, match="lacks smart-return inputs"):
        await replay_schedule_evaluations_from_records(broken)


@pytest.mark.asyncio
async def test_schedule_replay_profile_expiry_manual_and_off(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    hass = MagicMock()
    hass.states.get.side_effect = lambda cid: SimpleNamespace(state="off" if cid == "climate.off" else "heat")
    coord = SimpleNamespace(
        hass=hass,
        zones=[{"name": name, "climate_id": f"climate.{name}"} for name in ("off", "manual", "base")],
        _climate_entities={}, _climate_callbacks={}, record_diagnostic_event=lambda *_: None,
    )
    manager = ScheduleProfileManager(coord)
    manager.scheduler_enabled = True
    manager.active_profile = "holiday"
    manager.profile_expires_at = (now - timedelta(minutes=1)).isoformat()
    manager.fallback_profile = "standard"
    manager.manual_overrides = {
        "climate.off": {"target_temp": 19},
        "climate.manual": {"target_temp": 21, "until": (now + timedelta(hours=1)).isoformat()},
    }
    manager.evaluate_zone_slot = lambda *_: {"slot_id": "slot", "target_temp": 18}
    manager.get_upcoming_blocks = lambda *_args, **_kwargs: []
    manager.async_save_to_storage = AsyncMock()
    manager._notify_climates = lambda: None
    await manager.async_evaluate_schedule(now)
    records = all_records(recorder)
    event = next(row["data"] for row in records if row["data"].get("event") == "schedule_evaluation")
    assert event["profile_expired"] and event["after"]["active_profile"] == "standard"
    assert [zone["decision"] for zone in event["zones"]] == ["off", "manual_override", "schedule"]
    telemetry.set_recorder(None)
    replayed = await replay_schedule_evaluations_from_records(records)
    assert replayed[0][1] == event["after"]


@pytest.mark.asyncio
async def test_schedule_replay_smart_return_plan_and_completion(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    arrival = now + timedelta(hours=2)
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off")
    mode_by_zone = {"climate.a": "primary", "climate.b": "bypass"}
    coord = SimpleNamespace(
        hass=hass, zones=[{"name": name, "climate_id": f"climate.{name}"} for name in ("a", "b")],
        _climate_entities={}, _climate_callbacks={}, record_diagnostic_event=lambda *_: None,
        get_zone_mode=lambda cid: mode_by_zone[cid],
        zone_manager=SimpleNamespace(async_set_global_preset=AsyncMock()),
    )
    manager = ScheduleProfileManager(coord)
    manager.smart_return = {"arrival_time": arrival.isoformat(), "preset": "comfort", "zones": None}
    plan = {
        "climate.a": {"ignition_time": (now + timedelta(minutes=10)).isoformat(),
                      "arrival_time": arrival.isoformat(), "target_temp": 21, "heating_rate": 2},
        "climate.b": {"ignition_time": (now - timedelta(minutes=5)).isoformat(),
                      "arrival_time": arrival.isoformat(), "target_temp": 20, "heating_rate": 1},
    }
    manager.calculate_smart_return_plan = lambda *_: deepcopy(plan)
    manager._async_wake_master = AsyncMock()
    manager._async_wake_zone = AsyncMock()
    manager.async_save_to_storage = AsyncMock()
    manager._notify_climates = lambda: None
    for when in (now, now + timedelta(minutes=15), arrival):
        await manager.async_evaluate_schedule(when)
    records = all_records(recorder)
    events = [row["data"] for row in records if row["data"].get("event") == "schedule_evaluation"]
    assert [event["smart_return_completed"] for event in events] == [False, False, True]
    assert events[1]["smart_return_actions"] == [
        {"action": "wake_master"}, {"action": "wake_zone", "id": "climate.a"},
    ]
    assert events[2]["smart_return_actions"] == [
        {"action": "wake_master"}, {"action": "wake_zone", "id": "climate.a"},
        {"action": "set_preset", "preset": "comfort"},
    ]
    telemetry.set_recorder(None)
    replayed = await replay_schedule_evaluations_from_records(records)
    assert [after for _, after in replayed] == [event["after"] for event in events]
    broken = deepcopy(records)
    events = [row["data"] for row in broken if row["data"].get("event") == "schedule_evaluation"]
    events[1]["smart_return_actions"].pop()
    with pytest.raises(ValueError, match="diverged during replay"):
        await replay_schedule_evaluations_from_records(broken)


@pytest.mark.parametrize("as_datetime", [False, True])
def test_smart_return_plan_replays_raw_inputs_and_zone_filter(recorder, as_datetime):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    arrival = now + timedelta(hours=2)
    coord = SimpleNamespace(zones=[
        {"name": "A", "climate_id": "climate.a"}, {"name": "B", "climate_id": "climate.b"},
    ])
    manager = ScheduleProfileManager(coord)
    manager._get_zone_current_temp = lambda _: 19.2
    manager._resolve_preset_temp = lambda *_: 21
    manager._get_zone_heating_rate = lambda _: 0.6734
    original = manager.calculate_smart_return_plan(
        arrival if as_datetime else arrival.isoformat(), "comfort", ["climate.a"], now,
    )
    records = all_records(recorder)
    event = next(row["data"] for row in records if row["data"].get("event") == "smart_return_plan")
    assert event["inputs"][0]["heating_rate"] == 0.6734
    assert event["inputs"][1]["selected"] is False
    assert original["climate.a"]["heating_rate"] == 0.67
    telemetry.set_recorder(None)
    assert replay_smart_return_plans_from_records(records)[0][1] == original
    broken = deepcopy(records)
    broken[0]["data"]["inputs"][0]["heating_rate"] = 2
    with pytest.raises(ValueError, match="diverged during replay"):
        replay_smart_return_plans_from_records(broken)
    broken = deepcopy(records)
    del broken[0]["data"]["inputs"][0]["temperature"]
    with pytest.raises(ValueError, match="lacks temperature"):
        replay_smart_return_plans_from_records(broken)


@pytest.mark.asyncio
async def test_smart_return_plan_links_to_recorded_evaluation(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off")
    coord = SimpleNamespace(
        hass=hass, zones=[{"name": "A", "climate_id": "climate.a"}], _climate_entities={},
        _climate_callbacks={}, get_zone_mode=lambda _: "primary", record_diagnostic_event=lambda *_: None,
    )
    manager = ScheduleProfileManager(coord)
    manager.smart_return = {"arrival_time": (now + timedelta(hours=2)).isoformat(),
                            "preset": "comfort", "zones": None}
    manager._get_zone_current_temp = lambda _: 19
    manager._resolve_preset_temp = lambda *_: 21
    manager._get_zone_heating_rate = lambda _: 1
    manager._async_wake_master = AsyncMock()
    manager._async_wake_zone = AsyncMock()
    manager._notify_climates = lambda: None
    await manager.async_evaluate_schedule(now)
    records = all_records(recorder)
    telemetry.set_recorder(None)
    linked = await replay_linked_smart_return_evaluations_from_records(records)
    assert len(linked) == 1
    assert linked[0]["plan"]["data"]["plan"] == linked[0]["evaluation"]["data"]["smart_return_plan"]
    assert linked[0]["plan"]["data"]["evaluation_id"] == linked[0]["evaluation"]["data"]["evaluation_id"]
    with pytest.raises(ValueError, match="unique matching"):
        await replay_linked_smart_return_evaluations_from_records([
            row for row in records if row["data"].get("event") != "smart_return_plan"
        ])
    plan = next(row for row in records if row["data"].get("event") == "smart_return_plan")
    evaluation = next(row for row in records if row["data"].get("event") == "schedule_evaluation")
    duplicate = [deepcopy(plan), deepcopy(plan), deepcopy(evaluation)]
    for index, row in enumerate(duplicate):
        row["seq"] = index + 1
    with pytest.raises(ValueError, match="unique matching"):
        await replay_linked_smart_return_evaluations_from_records(duplicate)
    broken = deepcopy(records)
    next(row for row in broken if row["data"].get("event") == "schedule_evaluation")["data"]["evaluation_id"] = None
    with pytest.raises(ValueError, match="invalid evaluation boundary"):
        await replay_linked_smart_return_evaluations_from_records(broken)
    second_plan, second_evaluation = deepcopy([plan, evaluation])
    second_plan["data"]["evaluation_id"] = "independent"
    second_evaluation["data"]["evaluation_id"] = "independent"
    interleaved = deepcopy([plan, second_plan, second_evaluation, evaluation])
    for index, row in enumerate(interleaved):
        row["seq"] = index + 1
    linked = await replay_linked_smart_return_evaluations_from_records(interleaved)
    assert [row["plan"]["data"]["evaluation_id"] for row in linked] == [
        "independent", plan["data"]["evaluation_id"],
    ]


@pytest.mark.asyncio
async def test_manual_override_mutations_link_to_schedule_evaluations(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    coord = SimpleNamespace(hass=hass, zones=[], _climate_entities={}, _climate_callbacks={})
    manager = ScheduleProfileManager(coord)
    manager.scheduler_enabled = True
    manager.evaluate_zone_slot = lambda *_: {"until": now + timedelta(hours=1), "slot_id": "slot"}
    manager.record_manual_override("climate.a", 19, now)
    await manager.async_evaluate_schedule(now)
    manager.record_manual_override("climate.b", 21, now)
    await manager.async_clear_manual_override("climate.a", now)
    await manager.clear_manual_override(now=now)
    records = all_records(recorder)
    telemetry.set_recorder(None)
    mutations = await replay_manual_schedule_overrides_from_records(records)
    assert [row["record"]["data"]["action"] for row in mutations] == ["set", "set", "clear", "clear"]
    assert [row["record"]["data"]["override_step"] for row in mutations] == [0, 1, 2, 3]
    assert mutations[0]["after"]["manual_overrides"]["climate.a"]["target_temp"] == 19
    assert mutations[-1]["after"]["manual_overrides"] == {}
    assert len(await replay_stateful_schedule_evaluations_from_records(records)) == 3
    broken = deepcopy(records)
    next(row for row in broken if row["data"].get("event") == "schedule_manual_override")["data"]["inputs"]["until"] = "forged"
    with pytest.raises(ValueError, match="diverged during replay"):
        await replay_manual_schedule_overrides_from_records(broken)
    broken = deepcopy(records)
    mutations = [row for row in broken if row["data"].get("event") == "schedule_manual_override"]
    broken.remove(mutations[1])
    with pytest.raises(ValueError, match="invalid or missing boundary"):
        await replay_stateful_schedule_evaluations_from_records(broken)


@pytest.mark.asyncio
async def test_manual_override_detects_unrecorded_mutation(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    coord = SimpleNamespace(hass=MagicMock(), zones=[], _climate_entities={}, _climate_callbacks={})
    manager = ScheduleProfileManager(coord)
    manager.evaluate_zone_slot = lambda *_: {"until": now, "slot_id": "slot"}
    manager.record_manual_override("climate.a", 19, now)
    telemetry.set_recorder(None)
    manager.record_manual_override("climate.a", 20, now)
    telemetry.set_recorder(recorder)
    manager.record_manual_override("climate.a", 21, now)
    records = all_records(recorder)
    telemetry.set_recorder(None)
    with pytest.raises(ValueError, match="invalid or missing boundary"):
        await replay_manual_schedule_overrides_from_records(records)


@pytest.mark.asyncio
async def test_manual_override_after_startup_checkpoint_and_overlapping_evaluation(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    coord = SimpleNamespace(hass=MagicMock(), zones=[], _climate_entities={}, _climate_callbacks={})
    manager = ScheduleProfileManager(coord)
    manager.scheduler_enabled = True
    manager.record_schedule_state("schedule_startup")
    manager.evaluate_zone_slot = lambda *_: {"until": now, "slot_id": "slot"}
    manager.record_manual_override("climate.a", 19, now)
    await manager.async_evaluate_schedule(now)
    records = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_stateful_schedule_evaluations_from_records(records)
    assert replayed[0]["checkpoint"]["data"]["source"] == "schedule_startup"
    assert replayed[0]["after"]["manual_overrides"]["climate.a"]["target_temp"] == 19
    broken = deepcopy(records)
    action = next(row for row in broken if row["data"].get("event") == "schedule_manual_override")
    start = next(row for row in broken if row["data"].get("event") == "schedule_evaluation_start")
    start["data"]["before"]["manual_overrides"] = deepcopy(action["data"]["before"]["manual_overrides"])
    start["data"]["before"]["schedule_overrides"] = deepcopy(action["data"]["before"]["schedule_overrides"])
    action["seq"], start["seq"] = start["seq"], action["seq"]
    broken.sort(key=lambda row: row["seq"])
    with pytest.raises(ValueError, match="overlapping manual override"):
        await replay_stateful_schedule_evaluations_from_records(broken)


@pytest.mark.asyncio
async def test_stateful_schedule_rejects_missing_overlapping_and_divergent_evaluations(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    coord = SimpleNamespace(hass=MagicMock(), zones=[], _climate_entities={}, _climate_callbacks={})
    manager = ScheduleProfileManager(coord)
    manager.scheduler_enabled = True
    for offset in range(3):
        await manager.async_evaluate_schedule(now + timedelta(minutes=offset))
    records = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_stateful_schedule_evaluations_from_records(records)
    assert len(replayed) == 3
    assert [row["start"]["data"]["evaluation_step"] for row in replayed] == [0, 1, 2]
    middle_id = replayed[1]["start"]["data"]["evaluation_id"]
    with pytest.raises(ValueError, match="missing or duplicate evaluation step"):
        await replay_stateful_schedule_evaluations_from_records([
            row for row in records if row["data"].get("evaluation_id") != middle_id
        ])
    with pytest.raises(ValueError, match="incomplete evaluation"):
        await replay_stateful_schedule_evaluations_from_records(records[:-1])
    overlap = deepcopy(records)
    overlap[1], overlap[2] = overlap[2], overlap[1]
    for index, row in enumerate(overlap):
        row["seq"] = index + 1
    with pytest.raises(ValueError, match="overlapping or incomplete"):
        await replay_stateful_schedule_evaluations_from_records(overlap)
    broken = deepcopy(records)
    for row in broken:
        if row["data"].get("evaluation_id") == middle_id:
            row["data"]["before"]["manual_overrides"]["climate.a"] = {"target_temp": 18}
            if row["data"]["event"] == "schedule_evaluation":
                row["data"]["after"]["manual_overrides"]["climate.a"] = {"target_temp": 18}
    with pytest.raises(ValueError, match="divergent previous state"):
        await replay_stateful_schedule_evaluations_from_records(broken)


@pytest.mark.asyncio
async def test_stateful_schedule_rejects_cancelled_evaluation(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    coord = SimpleNamespace(hass=MagicMock(), zones=[], _climate_entities={}, _climate_callbacks={})
    manager = ScheduleProfileManager(coord)
    manager.smart_return = {"arrival_time": now.isoformat(), "preset": "comfort", "zones": None}
    manager._async_wake_master = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await manager.async_evaluate_schedule(now)
    records = all_records(recorder)
    assert [row["data"]["event"] for row in records] == ["schedule_evaluation_start"]
    telemetry.set_recorder(None)
    with pytest.raises(ValueError, match="incomplete evaluation"):
        await replay_stateful_schedule_evaluations_from_records(records)


@pytest.mark.asyncio
async def test_stateful_schedule_accepts_saved_configuration_checkpoint(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    coord = SimpleNamespace(
        hass=MagicMock(), zones=[], _climate_entities={}, _climate_callbacks={},
        _settings={}, _settings_store=SimpleNamespace(async_save=AsyncMock()),
    )
    manager = ScheduleProfileManager(coord)
    manager.scheduler_enabled = True
    await manager.async_evaluate_schedule(now)
    manager.manual_overrides["climate.a"] = {"target_temp": 19}
    await manager.async_save_to_storage()
    await manager.async_evaluate_schedule(now + timedelta(minutes=1))
    records = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_stateful_schedule_evaluations_from_records(records)
    assert replayed[0]["checkpoint"] is None
    assert replayed[1]["checkpoint"]["data"]["source"] == "schedule_saved"
    assert replayed[1]["after"]["manual_overrides"]["climate.a"]["target_temp"] == 19
    broken = deepcopy(records)
    checkpoint = next(row["data"]["schedule"] for row in broken if row["kind"] == "config")
    checkpoint["manual_overrides"]["climate.a"]["target_temp"] = 20
    with pytest.raises(ValueError, match="divergent previous state"):
        await replay_stateful_schedule_evaluations_from_records(broken)


@pytest.mark.asyncio
async def test_stateful_schedule_detects_evaluation_while_telemetry_disabled(recorder):
    now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    coord = SimpleNamespace(hass=MagicMock(), zones=[], _climate_entities={}, _climate_callbacks={})
    manager = ScheduleProfileManager(coord)
    manager.scheduler_enabled = True
    await manager.async_evaluate_schedule(now)
    telemetry.set_recorder(None)
    await manager.async_evaluate_schedule(now + timedelta(minutes=1))
    telemetry.set_recorder(recorder)
    await manager.async_evaluate_schedule(now + timedelta(minutes=2))
    records = all_records(recorder)
    telemetry.set_recorder(None)
    steps = [row["data"]["evaluation_step"] for row in records
             if row["data"].get("event") == "schedule_evaluation_start"]
    assert steps == [0, 2]
    with pytest.raises(ValueError, match="missing or duplicate evaluation step"):
        await replay_stateful_schedule_evaluations_from_records(records)


def test_frost_fallback_inputs_are_observable(recorder):
    """ASHRAE Addendum y §5.21.11: adapted missing sensor context, not its alarm sequence."""
    hass = MagicMock()
    hass.states.get.side_effect = lambda entity: {
        "climate.a": SimpleNamespace(attributes={"current_temperature": None}),
        "climate.trv": SimpleNamespace(attributes={"current_temperature": 4}),
    }.get(entity)
    coord = MagicMock()
    coord.get_persistent_data.side_effect = lambda key, default: 10 if "temp" in key else True
    coord.plant_diagnostics.has_critical_sensor_fault.return_value = True
    coord.zone_manager.get_zone.return_value = {"climate_entities": ["climate.trv"]}
    assert FrostManager(hass, coord).is_zone_in_frost_emergency("climate.a")
    row = all_records(recorder)[0]["data"]
    assert row["source"] == "trv_average" and row["temperature"] == 4
    assert row["trv_samples"] == [{"entity": "climate.trv", "temperature": 4}]
    assert row["sensor_fault"] and row["result"]
    records = all_records(recorder)
    telemetry.set_recorder(None)
    assert replay_frost_checks_from_records(records)[0][1] is True
    records[0]["data"].pop("trv_samples")
    with pytest.raises(ValueError, match="lacks replay inputs"):
        replay_frost_checks_from_records(records)


@pytest.mark.asyncio
async def test_zone_mode_resolution_records_window_and_calendar_precedence(recorder):
    """ASHRAE Addendum y §5.21.11: adapted override state visibility."""
    hass = MagicMock()
    tasks = []
    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    hass.async_create_task = create_task
    coord = SimpleNamespace(
        zones=[{"name": "A"}], calendar_manager=SimpleNamespace(mode_overrides={}),
        demand_aggregator=SimpleNamespace(set_zone_mode=lambda *_: None),
        get_persistent_data=lambda *_: {}, _settings={}, _climate_callbacks={},
        async_set_persistent_data=AsyncMock(),
    )
    manager = ZoneManager(hass, coord)
    cid = "climate.multizone_thermostat_a"
    assert manager.get_zone_mode(cid) == "primary"
    coord.calendar_manager.mode_overrides[cid] = "secondary"
    assert manager.get_zone_mode(cid) == "secondary"
    manager.set_zone_mode(cid, "bypass", save_to_preset=False)
    manager._pre_window_state[cid] = "primary"
    assert manager.get_zone_mode(cid) == "bypass"
    manager._pre_window_state.clear()
    assert manager.get_zone_mode(cid) == "secondary"
    coord.calendar_manager.mode_overrides.clear()
    assert manager.get_zone_mode(cid) == "bypass"
    await asyncio.gather(*tasks)
    rows = [r["data"] for r in all_records(recorder) if r["kind"] == "event"]
    assert [(r["source"], r["mode"]) for r in rows if r["event"] == "zone_mode_resolved"] == [
        ("configured", "primary"), ("calendar", "secondary"), ("window", "bypass"),
        ("calendar", "secondary"), ("configured", "bypass"),
    ]
    changed = next(r for r in rows if r["event"] == "zone_mode_changed")
    assert changed["previous_mode"] == "primary" and changed["mode"] == "bypass"
    assert not changed["save_to_preset"]


@pytest.mark.asyncio
async def test_frost_actions_and_disabled_scan_are_observable(recorder):
    """ASHRAE Addendum y §5.21.11: adapted state and fault observations."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(
        state="off", attributes={"current_temperature": 4, "temperature": 10},
    )
    hass.services.async_call = AsyncMock()
    coord = MagicMock()
    coord.zones = [{"name": "A"}]
    coord.get_persistent_data.side_effect = lambda key, default: 10 if "temp" in key else True
    coord.plant_diagnostics.has_critical_sensor_fault.return_value = False
    coord._async_set_hvac_mode = AsyncMock()
    manager = FrostManager(hass, coord)
    assert await manager.async_handle_frost_emergencies()
    coord.get_persistent_data.side_effect = lambda *_: False
    assert not await manager.async_handle_frost_emergencies()
    records = all_records(recorder)
    rows = [r["data"] for r in records]
    assert [r["phase"] for r in rows if r["event"] == "frost_action"] == [
        "triggered", "mode_input", "heat_mode_requested", "target_input", "target_requested",
    ]
    assert rows[-1] == {"event": "frost_scan", "enabled": False}
    assert next(r["target"] for r in rows if r.get("phase") == "target_requested") == 13
    telemetry.set_recorder(None)
    scans = await replay_frost_scans_from_records(records)
    assert [scan["result"] for scan in scans] == [True, False]
    assert scans[0]["actions"] == [
        ("demand", "climate.multizone_thermostat_a", 100.0),
        ("mode", "climate.multizone_thermostat_a", "heat"),
        ("service", "climate", "set_temperature",
         {"entity_id": "climate.multizone_thermostat_a", "temperature": 13.0}, False),
    ]
    with pytest.raises(ValueError, match="Incomplete frost scan"):
        await replay_frost_scans_from_records(records[:-2])
    missing_input = [r for r in records if r["data"].get("phase") != "target_input"]
    with pytest.raises(ValueError, match="threshold"):
        await replay_frost_scans_from_records(missing_input)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,target", [("heat", 13), ("heat", 10), ("off", 13)])
async def test_frost_scan_replay_skips_unnecessary_actions(recorder, mode, target):
    """ASHRAE Addendum y §5.21.11: adapted emergency state/action replay."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(
        state=mode, attributes={"current_temperature": 4, "temperature": target},
    )
    hass.services.async_call = AsyncMock()
    coord = MagicMock()
    coord.zones = [{"name": "A"}]
    coord.get_persistent_data.side_effect = lambda key, default: 10 if "temp" in key else True
    coord.plant_diagnostics.has_critical_sensor_fault.return_value = False
    coord._async_set_hvac_mode = AsyncMock()
    await FrostManager(hass, coord).async_handle_frost_emergencies()
    records = all_records(recorder)
    telemetry.set_recorder(None)
    actions = (await replay_frost_scans_from_records(records))[0]["actions"]
    assert sum(action[0] == "mode" for action in actions) == (mode == "off")
    assert sum(action[0] == "service" for action in actions) == (target != 13)


@pytest.mark.asyncio
async def test_frost_scan_replays_setting_changes_and_unavailable_states(recorder):
    """ASHRAE Addendum y §5.21.11: adapted emergency input and action tracing."""
    hass = MagicMock()
    hass.states.get.side_effect = [
        SimpleNamespace(state="heat", attributes={"current_temperature": 4}),
        None, None,
    ]
    hass.services.async_call = AsyncMock()
    coord = MagicMock()
    coord.zones = [{"name": "A"}]
    temps = iter([10, float("nan")])
    coord.get_persistent_data.side_effect = lambda key, default: next(temps) if "temp" in key else True
    coord.plant_diagnostics.has_critical_sensor_fault.return_value = False
    coord._async_set_hvac_mode = AsyncMock()
    assert await FrostManager(hass, coord).async_handle_frost_emergencies()
    records = all_records(recorder)
    telemetry.set_recorder(None)
    actions = (await replay_frost_scans_from_records(records))[0]["actions"]
    assert [action[0] for action in actions] == ["demand", "service"]
    assert math.isnan(actions[-1][3]["temperature"])


@pytest.mark.asyncio
async def test_frost_scan_replay_handles_protection_disabled_between_checks(recorder):
    """ASHRAE Addendum y §5.21.11: adapted changing override input."""
    hass = MagicMock()
    coord = MagicMock()
    coord.zones = [{"name": "A"}]
    enabled = iter([True, False])
    coord.get_persistent_data.side_effect = lambda *_: next(enabled)
    assert not await FrostManager(hass, coord).async_handle_frost_emergencies()
    records = all_records(recorder)
    telemetry.set_recorder(None)
    scan = (await replay_frost_scans_from_records(records))[0]
    assert not scan["result"] and scan["actions"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["set_demand", "set_mode", "set_target"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_frost_scan_replays_action_failure_or_cancellation(recorder, operation, cancel):
    """ASHRAE Addendum y §5.21.11: adapted emergency action failure visibility."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(
        state="off", attributes={"current_temperature": 4, "temperature": 10},
    )
    hass.services.async_call = AsyncMock()
    coord = MagicMock()
    coord.zones = [{"name": "A"}, {"name": "B"}]
    coord.get_persistent_data.side_effect = lambda key, default: 10 if "temp" in key else True
    coord.plant_diagnostics.has_critical_sensor_fault.return_value = False
    coord._async_set_hvac_mode = AsyncMock()
    exception = asyncio.CancelledError() if cancel else RuntimeError("action failed")
    if operation == "set_demand":
        coord.set_zone_demand.side_effect = exception
    elif operation == "set_mode":
        coord._async_set_hvac_mode.side_effect = exception
    else:
        hass.services.async_call.side_effect = exception
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await FrostManager(hass, coord).async_handle_frost_emergencies()
    rows = all_records(recorder)
    terminal = rows[-1]["data"]
    assert terminal["phase"] == ("cancelled" if cancel else "failed")
    assert terminal["operation"] == operation
    telemetry.set_recorder(None)
    replayed = (await replay_frost_scans_from_records(rows))[0]
    assert replayed["outcome"] == terminal["phase"]
    assert replayed["result"] is None
    assert replayed["actions"][-1][0] == {
        "set_demand": "demand", "set_mode": "mode", "set_target": "service",
    }[operation]
    missing = deepcopy(rows)
    missing[-1]["data"]["operation"] = "unknown"
    with pytest.raises(ValueError, match="unsupported failure context"):
        await replay_frost_scans_from_records(missing)
    with pytest.raises(ValueError, match="Incomplete frost scan"):
        await replay_frost_scans_from_records(rows[:-1])


@pytest.mark.asyncio
async def test_anti_seize_phases_and_saved_values(recorder):
    """ASHRAE Addendum y §5.21.11: adapted operating state visibility."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off")
    coord = MagicMock()
    coord.zones = [{"name": "A"}]
    coord._valve_delay = 0
    coord.get_persistent_data.side_effect = lambda key, default: 0 if "duration" in key else default
    coord.get_zone_demand.return_value = 27
    coord._async_set_hvac_mode = AsyncMock()
    await AntiSeizeManager(hass, coord).async_execute()
    rows = [r["data"] for r in all_records(recorder)]
    assert [r["phase"] for r in rows] == [
        "start", "zones_selected", "zone_open_requested", "valve_wait", "boiler_decision",
        "duration", "zone_restored", "completed",
    ]
    assert list(rows[1]["saved_demands"].values()) == [27]
    telemetry.set_recorder(None)
    replayed = await replay_anti_seize_from_records([{"kind": "event", "data": row} for row in rows])
    assert replayed[0]["actions"] == [
        ("mode", "climate.multizone_thermostat_a", "heat"),
        ("demand", "climate.multizone_thermostat_a", 100.0),
        ("demand", "climate.multizone_thermostat_a", 27.0),
        ("mode", "climate.multizone_thermostat_a", "off"),
    ]
    assert replayed[0]["sleeps"] == [0]


@pytest.mark.asyncio
async def test_anti_seize_cancel_records_cleanup(recorder):
    """ASHRAE Addendum y §5.21.11: adapted operating state visibility."""
    hass = MagicMock()
    tasks = []
    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    hass.async_create_task = create_task
    hass.states.get.return_value = SimpleNamespace(state="off")
    coord = MagicMock()
    coord.zones = [{"name": "A"}]
    coord._valve_delay = 60
    coord.get_zone_demand.return_value = 27
    coord._async_set_hvac_mode = AsyncMock()
    coord.boiler_backend.async_update = AsyncMock()
    manager = AntiSeizeManager(hass, coord)
    task = asyncio.create_task(manager.async_execute())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(*tasks)
    phases = [r["data"]["phase"] for r in all_records(recorder)]
    assert phases == ["start", "zones_selected", "zone_open_requested", "valve_wait",
                      "cancelled", "boiler_off_status", "cancel_cleanup_completed"]
    assert not manager.is_running
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_anti_seize_from_records(rows)
    assert replayed[0]["sleeps"] == [60]
    assert ("boiler", 0.0, None, False) in replayed[0]["actions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_zones", [True, False])
async def test_anti_seize_boiler_and_empty_zone_cycle_replay(recorder, with_zones):
    """ASHRAE Addendum y §5.21.11: adapted lifecycle replay, not feedback proof."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off")
    coord = MagicMock()
    coord.zones = [{"name": "A", "anti_seize_zone_enable": with_zones}]
    coord._valve_delay = 0
    coord.get_zone_demand.return_value = 10
    coord._async_set_hvac_mode = AsyncMock()
    coord.boiler_backend.async_update = AsyncMock()
    coord.get_persistent_data.side_effect = lambda key, default: (
        True if key == "anti_seize_boiler_enable" else (0 if key == "anti_seize_duration" else default)
    )
    await AntiSeizeManager(hass, coord).async_execute()
    rows = all_records(recorder)
    telemetry.set_recorder(None)
    replayed = await replay_anti_seize_from_records(rows)
    boiler = [action for action in replayed[0]["actions"] if action[0] == "boiler"]
    assert boiler == ([("boiler", 100.0, "Anti-Seize", False), ("boiler", 0.0, None, False)]
                      if with_zones else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "zone_open_mode", "zone_open_demand", "valve_wait", "boiler_on",
    "duration_wait", "boiler_off", "zone_restore_demand", "zone_restore_mode",
])
async def test_anti_seize_failure_stage_and_cleanup_replay(recorder, failure):
    """ASHRAE Addendum y §5.21.11: adapted fault lifecycle replay."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off")
    coord = MagicMock()
    coord.zones = [{"name": "A"}]
    coord._valve_delay = 2 if failure == "valve_wait" else 0
    coord.get_zone_demand.return_value = 10
    mode_effect = ([RuntimeError("failure"), None] if failure == "zone_open_mode"
                   else [None, RuntimeError("failure"), None] if failure == "zone_restore_mode" else None)
    demand_effect = ([RuntimeError("failure"), None] if failure == "zone_open_demand"
                     else [None, RuntimeError("failure"), None] if failure == "zone_restore_demand" else None)
    boiler_effect = ([RuntimeError("failure"), None] if failure == "boiler_on"
                     else [None, RuntimeError("failure"), None] if failure == "boiler_off" else None)
    coord._async_set_hvac_mode = AsyncMock(side_effect=mode_effect)
    coord.set_zone_demand.side_effect = demand_effect
    coord.boiler_backend.async_update = AsyncMock(side_effect=boiler_effect)
    coord.get_persistent_data.side_effect = lambda key, default: (
        True if key == "anti_seize_boiler_enable" else (0 if key == "anti_seize_duration" else default)
    )
    async def sleep(seconds):
        if failure in ("valve_wait", "duration_wait"):
            raise RuntimeError("failure")

    with patch.object(anti_seize_module, "asyncio", SimpleNamespace(
        sleep=sleep, CancelledError=asyncio.CancelledError,
    )):
        await AntiSeizeManager(hass, coord).async_execute()
    rows = all_records(recorder)
    error = next(row["data"] for row in rows if row["data"].get("phase") == "error")
    assert error["stage"]["operation"] == failure
    assert rows[-1]["data"]["phase"] == "cleanup_completed"
    telemetry.set_recorder(None)
    replayed = await replay_anti_seize_from_records(rows)
    assert ("boiler", 0.0, None, False) in replayed[0]["actions"]
    unsupported = deepcopy(rows)
    next(r["data"] for r in unsupported if r["data"].get("phase") == "error")["stage"]["operation"] = "selection"
    with pytest.raises(ValueError, match="unsupported operation"):
        await replay_anti_seize_from_records(unsupported)
    cleanup_error = deepcopy(rows)
    cleanup_error[-1]["data"]["phase"] = "cleanup_error"
    with pytest.raises(ValueError, match="cleanup failure"):
        await replay_anti_seize_from_records(cleanup_error)
    if failure == "boiler_on":
        missing = [r for r in rows if r["data"].get("phase") != "boiler_decision"]
        with pytest.raises(ValueError, match="incomplete inputs"):
            await replay_anti_seize_from_records(missing)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_anti_seize_replays_boiler_hold_emergency_off(recorder, fails):
    """ASHRAE Addendum y §5.21.11: adapted emergency shutdown action."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off")
    backend = SimpleNamespace(
        status_reason="HOLD_ON - minimum cycle",
        async_update=AsyncMock(),
        async_emergency_off=AsyncMock(side_effect=[RuntimeError("shutdown failed"), None] if fails else None),
    )
    coord = MagicMock()
    coord.zones = [{"name": "A"}]
    coord._valve_delay = 0
    coord.get_zone_demand.return_value = 10
    coord._async_set_hvac_mode = AsyncMock()
    coord.boiler_backend = backend
    coord.get_persistent_data.side_effect = lambda key, default: (
        True if key == "anti_seize_boiler_enable" else (0 if key == "anti_seize_duration" else default)
    )
    await AntiSeizeManager(hass, coord).async_execute()
    records = all_records(recorder)
    telemetry.set_recorder(None)
    result = (await replay_anti_seize_from_records(records))[0]
    assert result["actions"].count(("emergency_off",)) == (2 if fails else 1)
    assert backend.async_emergency_off.await_count == (2 if fails else 1)
    missing = [row for row in records if row["data"].get("phase") != "boiler_off_status"]
    with pytest.raises(ValueError, match="incomplete inputs"):
        await replay_anti_seize_from_records(missing)


@pytest.mark.asyncio
async def test_anti_seize_replays_cancellation_during_emergency_off(recorder):
    """ASHRAE Addendum y §5.21.11: adapted interrupted emergency shutdown."""
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="off")
    cleanup_tasks = []
    def create_task(coro):
        task = asyncio.create_task(coro)
        cleanup_tasks.append(task)
        return task
    hass.async_create_task = create_task
    backend = SimpleNamespace(
        status_reason="HOLD_ON",
        async_update=AsyncMock(),
        async_emergency_off=AsyncMock(side_effect=[asyncio.CancelledError(), None]),
    )
    coord = MagicMock()
    coord.zones = [{"name": "A"}]
    coord._valve_delay = 0
    coord.get_zone_demand.return_value = 10
    coord._async_set_hvac_mode = AsyncMock()
    coord.boiler_backend = backend
    coord.get_persistent_data.side_effect = lambda key, default: (
        True if key == "anti_seize_boiler_enable" else (0 if key == "anti_seize_duration" else default)
    )
    with pytest.raises(asyncio.CancelledError):
        await AntiSeizeManager(hass, coord).async_execute()
    await asyncio.gather(*cleanup_tasks)
    records = all_records(recorder)
    telemetry.set_recorder(None)
    result = (await replay_anti_seize_from_records(records))[0]
    assert result["actions"].count(("emergency_off",)) == 2
    assert backend.async_emergency_off.await_count == 2
