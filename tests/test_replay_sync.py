"""Closed-loop switch/valve reconciliation against fault-injecting fake switches."""
from unittest.mock import MagicMock

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
