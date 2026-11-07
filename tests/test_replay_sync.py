"""Closed-loop switch/valve reconciliation against fault-injecting fake switches."""
from unittest.mock import MagicMock
from types import SimpleNamespace
from copy import deepcopy
import asyncio

import pytest

from homeassistant.components.climate import HVACMode
from custom_components.multizone_thermostat.sync_manager import ZoneSyncManager


class FakeSwitches:
    """Minimal hass stand-in: stateful switches with injectable faults."""

    def __init__(self, *names):
        self.state = {n: "off" for n in names}
        self.raises: dict[str, Exception] = {}
        self.drop_commands: set[str] = set()
        self.commands: list[tuple[str, str]] = []
        self.states = MagicMock()
        self.states.get = self._get
        self.services = MagicMock()
        self.services.async_call = self._call

    def _get(self, entity_id):
        return MagicMock(state=self.state[entity_id], attributes={}) if entity_id in self.state else None

    async def _call(self, domain, service, data, **kwargs):
        entity = data["entity_id"]
        self.commands.append((entity, service))
        if entity in self.raises:
            raise self.raises[entity]
        if entity in self.drop_commands or self.state[entity] == "unavailable":
            return
        self.state[entity] = "on" if service == "turn_on" else "off"


def make_manager(hass, demand=100.0):
    coordinator = MagicMock()
    coordinator.get_zone_demand.return_value = demand
    coordinator.get_zone_mode.return_value = "primary"
    return ZoneSyncManager(hass=hass, coordinator=coordinator, zone_name="Zone", entity_id="climate.zone",
                           climates=[], switches=list(hass.state), calibrations={}), coordinator


# ---- correct today --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_switch_converges_after_unavailable_window():
    hass = FakeSwitches("switch.v1")
    sm, _ = make_manager(hass)
    hass.state["switch.v1"] = "unavailable"
    for _ in range(6):
        await sm.async_pwm_tick(HVACMode.HEAT)
    assert hass.commands == []
    hass.state["switch.v1"] = "off"
    await sm.async_pwm_tick(HVACMode.HEAT)
    assert hass.state["switch.v1"] == "on"


@pytest.mark.asyncio
async def test_dropped_commands_are_retried_every_tick_until_they_land():
    hass = FakeSwitches("switch.v1")
    sm, _ = make_manager(hass)
    hass.drop_commands.add("switch.v1")
    for _ in range(5):
        await sm.async_pwm_tick(HVACMode.HEAT)
    assert hass.commands == [("switch.v1", "turn_on")] * 5
    hass.drop_commands.clear()
    await sm.async_pwm_tick(HVACMode.HEAT)
    assert hass.state["switch.v1"] == "on"


@pytest.mark.asyncio
async def test_master_off_forces_every_switch_off():
    hass = FakeSwitches("switch.v1", "switch.v2")
    sm, _ = make_manager(hass)
    await sm.async_pwm_tick(HVACMode.HEAT)
    assert set(hass.state.values()) == {"on"}
    await sm.async_pwm_tick(HVACMode.OFF)
    assert set(hass.state.values()) == {"off"}


@pytest.mark.asyncio
async def test_no_commands_once_in_sync():
    hass = FakeSwitches("switch.v1")
    sm, _ = make_manager(hass)
    await sm.async_pwm_tick(HVACMode.HEAT)
    hass.commands.clear()
    for _ in range(10):
        await sm.async_pwm_tick(HVACMode.HEAT)
    assert hass.commands == []


@pytest.mark.asyncio
async def test_production_telemetry_round_trips_through_replay(tmp_path):
    """Real ZoneSyncManager -> installed recorder -> JSONL -> replay through PWMEngine: same decisions."""
    from mzt_replay.drivers import replay_pwm_from_records
    from mzt_replay.loader import load_prod_module
    from custom_components.multizone_thermostat import telemetry as prod_telemetry

    hass = FakeSwitches("switch.v1")
    sm, coordinator = make_manager(hass)
    recorder = prod_telemetry.JsonlRecorder(str(tmp_path))
    prod_telemetry.set_recorder(recorder)
    try:
        for i in range(200):
            coordinator.get_zone_demand.return_value = [0.0, 35.0, 80.0, 3.0, 100.0][(i // 7) % 5]
            await sm.async_pwm_tick(HVACMode.OFF if i % 53 == 52 else HVACMode.HEAT)
        recorder.flush()
    finally:
        prod_telemetry.set_recorder(None)

    records = list(load_prod_module("telemetry").read_records(sorted(str(p) for p in tmp_path.glob("*.jsonl"))))
    kinds = {r["kind"] for r in records}
    assert {"pwm", "sync"} <= kinds
    replayed = replay_pwm_from_records(records, zone="Zone")
    assert replayed, "no replayable PWM records"
    for record, state in replayed:
        assert state == record["data"]["state"], f"diverged at seq {record['seq']}"


# ---- known defects --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_real_log_pwm_replay_preserves_calculation_time_at_boundary(tmp_path):
    """A delayed record timestamp must not change the recorded PWM decision."""
    from custom_components.multizone_thermostat import pwm_engine, telemetry
    from tests.mzt_replay.clock import ManualClock, patched_time
    from tests.mzt_replay.drivers import replay_pwm_from_records

    clock = ManualClock(start=449.9)
    hass = FakeSwitches("switch.v1")
    recorder = telemetry.JsonlRecorder(str(tmp_path), mono_clock=lambda: clock.now + 1)
    telemetry.set_recorder(recorder)
    try:
        with patched_time(pwm_engine, clock):
            manager, _ = make_manager(hass, demand=50)
            manager._local_pwm.cycle_start_time = 0
            manager._local_pwm._prev_demand = 50
            await manager.async_pwm_tick(HVACMode.HEAT)
        recorder.flush()
    finally:
        telemetry.set_recorder(None)
    records = list(telemetry.read_records(str(p) for p in tmp_path.glob("*.jsonl")))
    record, replayed = replay_pwm_from_records(records, zone="Zone", require_calculation_time=True)[0]
    assert record["data"]["calculation_time"] == 449.9
    assert record["mono"] == 450.9
    assert record["data"]["state"] is True
    assert replayed == record["data"]["state"]
    record["data"].pop("calculation_time")
    with pytest.raises(ValueError, match="lacks the calculation time"):
        replay_pwm_from_records(records, zone="Zone", require_calculation_time=True)
    assert replay_pwm_from_records(records, zone="Zone")[0][1] is False


@pytest.mark.asyncio
async def test_relay_boiler_replay_uses_pwm_calculation_time(tmp_path):
    """The boiler record retains the engine clock read even when the record is timestamped later."""
    from custom_components.multizone_thermostat import pwm_engine, telemetry
    from custom_components.multizone_thermostat.boiler import relay_backend
    from tests.mzt_replay.clock import ManualClock, patched_time
    from tests.mzt_replay.drivers import replay_pwm_from_records, replay_stateful_pwm_from_records

    clock = ManualClock(start=449.9)
    hass = FakeSwitches("switch.boiler")
    hass.state["switch.boiler"] = "on"
    coordinator = SimpleNamespace(
        zones=[], is_dhw_active=False, plant_diagnostics=SimpleNamespace(_current_boiler_start=1),
        reset_last_active_time=lambda: None, record_diagnostic_event=lambda *_: None,
    )
    recorder = telemetry.JsonlRecorder(str(tmp_path), mono_clock=lambda: clock.now + 1)
    telemetry.set_recorder(recorder)
    try:
        with patched_time(pwm_engine, clock), patched_time(relay_backend, clock):
            backend = relay_backend.RelayBoilerBackend(hass, coordinator, "switch.boiler", 0, 0, 0)
            backend._boiler_pwm.cycle_start_time = 0
            backend._boiler_pwm._prev_demand = 50
            backend._last_known_boiler_on = True
            await backend.async_update(50, "Zone", False)
        recorder.flush()
    finally:
        telemetry.set_recorder(None)
    records = list(telemetry.read_records(str(p) for p in tmp_path.glob("*.jsonl")))
    record, replayed = replay_pwm_from_records(records, zone="boiler", require_calculation_time=True)[0]
    assert record["data"]["calculation_time"] == 449.9 and record["mono"] == 450.9
    assert replayed is record["data"]["state"] is True
    assert replay_stateful_pwm_from_records(records, "boiler", "boiler")[0][1] is True


@pytest.mark.asyncio
async def test_stateful_pwm_replay_detects_missing_and_divergent_steps(tmp_path):
    """Recorded engine calls replay continuously and detect interior gaps."""
    from custom_components.multizone_thermostat import pwm_engine, telemetry
    from tests.mzt_replay.clock import ManualClock, patched_time
    from tests.mzt_replay.drivers import replay_stateful_pwm_from_records

    clock = ManualClock(start=100)
    hass = FakeSwitches("switch.v1")
    recorder = telemetry.JsonlRecorder(str(tmp_path), mono_clock=clock.monotonic)
    telemetry.set_recorder(recorder)
    try:
        with patched_time(pwm_engine, clock):
            manager, coord = make_manager(hass, demand=50)
            for value in (100, 549, 551):
                clock.set(value)
                await manager.async_pwm_tick(HVACMode.HEAT)
        recorder.flush()
    finally:
        telemetry.set_recorder(None)
    records = list(telemetry.read_records(str(p) for p in tmp_path.glob("*.jsonl")))
    pwm = [r for r in records if r["kind"] == "pwm"]
    assert [state for _, state in replay_stateful_pwm_from_records(pwm, "Zone", "pwm")] == [True, True, False]
    master_off = deepcopy(pwm[0])
    master_off["data"].update(demand=None, state=False, master_off=True, calculation_time=None,
                              pwm_epoch=None, pwm_step=None)
    with_off = pwm[:1] + [master_off] + pwm[1:]
    assert [state for _, state in replay_stateful_pwm_from_records(with_off, "Zone", "pwm")] == [
        True, True, False,
    ]
    master_off["data"]["state"] = True
    with pytest.raises(ValueError, match="lacks the demand input"):
        replay_stateful_pwm_from_records(with_off, "Zone", "pwm")
    with pytest.raises(ValueError, match="missing or duplicate step"):
        replay_stateful_pwm_from_records(pwm[::2], "Zone", "pwm")
    bad = deepcopy(pwm)
    bad[1]["data"]["cycle_start_before"] = 10
    with pytest.raises(ValueError, match="divergent previous state"):
        replay_stateful_pwm_from_records(bad, "Zone", "pwm")
    bad = deepcopy(pwm)
    bad[1]["data"]["state"] = not bad[1]["data"]["state"]
    with pytest.raises(ValueError, match="diverged"):
        replay_stateful_pwm_from_records(bad, "Zone", "pwm")
    for missing in ("absent", None):
        bad = deepcopy(pwm)
        if missing == "absent":
            del bad[1]["data"]["demand"]
        else:
            bad[1]["data"]["demand"] = missing
        with pytest.raises(ValueError, match="lacks the demand input"):
            replay_stateful_pwm_from_records(bad, "Zone", "pwm")


@pytest.mark.asyncio
async def test_stateful_pwm_replay_accepts_actual_master_off_tick(tmp_path):
    """Master-off records do not represent a PWM-engine calculation."""
    from custom_components.multizone_thermostat import pwm_engine, telemetry
    from tests.mzt_replay.clock import ManualClock, patched_time
    from tests.mzt_replay.drivers import replay_stateful_pwm_from_records

    clock = ManualClock(start=100)
    recorder = telemetry.JsonlRecorder(str(tmp_path), mono_clock=clock.monotonic)
    telemetry.set_recorder(recorder)
    try:
        with patched_time(pwm_engine, clock):
            manager, _ = make_manager(FakeSwitches("switch.v1"), demand=50)
            for mode in (HVACMode.HEAT, HVACMode.OFF, HVACMode.HEAT):
                await manager.async_pwm_tick(mode)
        recorder.flush()
    finally:
        telemetry.set_recorder(None)
    rows = list(telemetry.read_records(str(p) for p in tmp_path.glob("*.jsonl")))
    pwm = [row for row in rows if row["kind"] == "pwm"]
    assert pwm[1]["data"]["master_off"] is True and pwm[1]["data"]["demand"] is None
    assert len(replay_stateful_pwm_from_records(pwm, "Zone", "pwm")) == 2


@pytest.mark.asyncio
async def test_stateful_boiler_pwm_rejects_out_of_order_completed_updates(tmp_path):
    """Overlapping boiler updates must not be mistaken for an ordered engine history."""
    from custom_components.multizone_thermostat import telemetry
    from custom_components.multizone_thermostat.boiler.relay_backend import RelayBoilerBackend
    from tests.mzt_replay.drivers import replay_stateful_pwm_from_records

    hass = FakeSwitches("switch.boiler")
    coordinator = SimpleNamespace(
        zones=[], is_dhw_active=False, plant_diagnostics=SimpleNamespace(_current_boiler_start=None),
        get_zone_mode=lambda _: "primary", get_zone_demand=lambda _: 0,
        record_diagnostic_event=lambda *_: None,
    )
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        backend = RelayBoilerBackend(hass, coordinator, "switch.boiler", 0, 0, 0)
        async def hold_on():
            entered.set()
            await release.wait()
        backend._force_boiler_on = hold_on
        first = asyncio.create_task(backend.async_update(100, "Zone", False))
        await entered.wait()
        await backend.async_update(0, None, False)
        release.set()
        await first
        recorder.flush()
    finally:
        telemetry.set_recorder(None)
    rows = list(telemetry.read_records(str(p) for p in tmp_path.glob("*.jsonl")))
    boiler = [row for row in rows if row["kind"] == "boiler"]
    assert [row["data"]["pwm_step"] for row in boiler] == [1, 0]
    assert [row["data"]["calculation_state"] for row in boiler] == [False, True]
    with pytest.raises(ValueError, match="missing or duplicate step"):
        replay_stateful_pwm_from_records(boiler, "boiler", "boiler")


@pytest.mark.known_defect("SYNC-001", "an exception commanding one switch aborts the loop: later switches are never commanded")
@pytest.mark.asyncio
async def test_failing_switch_does_not_block_other_switches():
    hass = FakeSwitches("switch.v1", "switch.v2")
    hass.raises["switch.v1"] = RuntimeError("mqtt timeout")
    sm, _ = make_manager(hass)
    try:
        await sm.async_pwm_tick(HVACMode.HEAT)
    except RuntimeError:
        pass
    assert hass.state["switch.v2"] == "on"


@pytest.mark.known_defect("SYNC-002", "a service-call exception propagates out of the 10s PWM tick")
@pytest.mark.asyncio
async def test_service_exception_does_not_escape_the_tick():
    hass = FakeSwitches("switch.v1")
    hass.raises["switch.v1"] = RuntimeError("mqtt timeout")
    sm, _ = make_manager(hass)
    await sm.async_pwm_tick(HVACMode.HEAT)


@pytest.mark.known_defect("SYNC-003", "valve switches are driven by a private 0/0 PWM engine: demand flutter clicks the valves")
@pytest.mark.asyncio
async def test_valve_switch_does_not_click_on_demand_flutter():
    hass = FakeSwitches("switch.v1")
    sm, coordinator = make_manager(hass)
    for i in range(360):
        coordinator.get_zone_demand.return_value = 2.0 if (i // 3) % 2 == 0 else 0.0
        await sm.async_pwm_tick(HVACMode.HEAT)
    assert len(hass.commands) <= 6, f"{len(hass.commands)} valve commands in 1h"
