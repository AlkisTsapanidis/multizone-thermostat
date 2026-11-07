"""Tests for PID recovery and integral/timestamp reset after DHW production (roadmap 4.5)."""
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.multizone_thermostat.zone_controller import ZoneController, make_zone_entity_id
from custom_components.multizone_thermostat.pid_wrapper import MultizonePID
from custom_components.multizone_thermostat import telemetry
from tests.mzt_replay.drivers import (
    replay_hysteresis_from_records, replay_weather_compensation_from_records, replay_zone_control_skips_from_records,
)


class DummyState:
    def __init__(self, state="heat", current_temp=19.0, target_temp=20.0):
        self.state = state
        self.attributes = {
            "current_temperature": current_temp,
            "temperature": target_temp,
        }


def _create_mock_coordinator():
    coord = MagicMock()
    coord.zones = [{"name": "Salone"}]
    demands = {}
    coord.demand_aggregator = MagicMock()
    coord.demand_aggregator.get_zone_demand = MagicMock(side_effect=lambda cid: demands.get(cid, 0.0))
    coord.demand_aggregator.set_zone_demand = MagicMock(side_effect=lambda cid, d: demands.update({cid: d}))
    coord.get_zone_demand = coord.demand_aggregator.get_zone_demand
    coord.get_zone_mode = MagicMock(return_value="auto")
    coord.plant_diagnostics = MagicMock()
    coord.plant_diagnostics.has_critical_sensor_fault = MagicMock(return_value=False)
    coord.weather_sensor_id = None
    coord.is_dhw_active = False
    return coord, demands


@pytest.mark.asyncio
async def test_pid_recovery_after_20_minute_dhw():
    """Verify that after a 20-minute DHW event, the PID resumes without integral surge or dt jump."""
    hass = MagicMock()
    coord, demands = _create_mock_coordinator()
    zc = ZoneController(hass, coord)
    cid = make_zone_entity_id("Salone")

    # Set up PID with Kp=10.0, Ki=0.05 (integrating), Kd=1.0
    tuner = zc.get_autotuner(cid)
    tuner.state = tuner.STATE_COMPLETED
    pid = zc.get_pid(cid)
    pid.set_pid_param(kp=10.0, ki=0.05, kd=1.0)

    # 1. Normal heating before DHW: Error = 1.0°C (19.0 -> 20.0)
    t0 = 1000.0
    with patch("time.monotonic", return_value=t0):
        st = DummyState("heat", current_temp=19.0, target_temp=20.0)
        res1 = await zc.async_update_zone(cid, st)
        # First sample: dt = 0, proportional = 10.0%, integral = 0.0
        assert res1 == pytest.approx(10.0)

    # Second sample 60s later: dt = 60s, error = 1.0 -> integral adds Ki * 1.0 * 60 = 3.0%
    with patch("time.monotonic", return_value=t0 + 60.0):
        res2 = await zc.async_update_zone(cid, st)
        assert res2 > res1
        assert pid._pid.integral == pytest.approx(3.0)

    # 2. DHW starts and lasts 20 minutes (1200 seconds)
    coord.is_dhw_active = True
    with patch("time.monotonic", return_value=t0 + 120.0):
        await zc.async_update_zone(cid, st)
        # PID frozen during DHW, clear_samples called
        assert pid._pid.integral == 0.0

    # 3. DHW ends 20 minutes later
    coord.is_dhw_active = False
    with patch("time.monotonic", return_value=t0 + 1260.0):
        # Resume heating
        res_post_dhw = await zc.async_update_zone(cid, st)

        # CRITICAL VERIFICATION:
        # If dt had not been reset, dt would be 1200s, and Ki * error * 1200 = 60% jump!
        # Instead, dt must be 0 on resumption and integral reset to 0.0.
        assert pid._pid._dt == 0.0
        assert pid._pid.integral == 0.0
        # Demand resumes cleanly from proportional baseline (10.0%) without sudden spike
        assert res_post_dhw == pytest.approx(10.0)

    # Subsequent sample 30s after resumption integrates normally with small dt=30s
    with patch("time.monotonic", return_value=t0 + 1290.0):
        res_subsequent = await zc.async_update_zone(cid, st)
        assert pid._pid._dt == pytest.approx(30.0)
        assert pid._pid.integral == pytest.approx(0.05 * 1.0 * 30.0)  # 1.5%
        assert res_subsequent == pytest.approx(11.5)


def test_multizone_pid_clear_samples_resets_all_timing_and_integral():
    """Direct verification of MultizonePID clear_samples contract."""
    pid = MultizonePID(kp=10.0, ki=0.1, kd=1.0)
    
    t0 = 500.0
    with patch("time.monotonic", return_value=t0):
        pid.calc(18.0, 20.0)

    with patch("time.monotonic", return_value=t0 + 50.0):
        pid.calc(18.0, 20.0)
        assert pid._pid.integral > 0.0
        assert pid._last_input_time == t0 + 50.0

    # Clear samples (as happens at DHW start/finish)
    pid.clear_samples()
    assert pid._last_input_time is None
    assert pid._pid.integral == 0.0

    # First call after clear has dt=0
    with patch("time.monotonic", return_value=t0 + 1500.0):
        demand = pid.calc(18.0, 20.0)
        assert pid._pid._dt == 0.0
        assert pid._pid.integral == 0.0
        assert demand == pytest.approx(20.0)  # Pure proportional: 10.0 * 2.0


@pytest.mark.asyncio
async def test_zone_dhw_and_weather_decisions_are_observable(tmp_path):
    """ASHRAE Addendum y §5.21.11: adapted DHW and weather decision visibility."""
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    try:
        coord, demands = _create_mock_coordinator()
        coord.get_persistent_data.side_effect = lambda key, default: 1.0 if key == "weather_curve" else default
        coord.weather_sensor_id = "sensor.outdoor"
        controller = ZoneController(MagicMock(), coord)
        cid = make_zone_entity_id("Salone")
        controller.get_autotuner(cid).state = controller.get_autotuner(cid).STATE_COMPLETED
        controller.get_pid(cid).set_pid_param(kp=10, ki=0, kd=0)
        controller._outdoor_temp = MagicMock(return_value=0)
        controller._thermal_models[cid].get_weather_multiplier = MagicMock(return_value=2)
        state = DummyState(current_temp=19.8, target_temp=20)
        result = await controller.async_update_zone(cid, state, learn=False)
        assert result == pytest.approx(18)
        coord.is_dhw_active = True
        assert await controller.async_update_zone(cid, state, learn=False) == result
        coord.is_dhw_active = False
        await controller.async_update_zone(cid, state, learn=False)
        recorder.flush()
        rows = [r["data"] for r in telemetry.read_records([str(p) for p in Path(tmp_path).glob("*.jsonl")])
                if r["kind"] == "event"]
        dhw = [r for r in rows if r["event"] == "zone_dhw"]
        assert [(r["active"], r["previously_active"], r["reset_all"], r["pid_frozen"]) for r in dhw] == [
            (False, False, False, False), (True, False, False, True), (False, True, True, False),
        ]
        assert next(r for r in rows if r["event"] == "zone_dhw_held_demand")["demand"] == result
        weather = next(r for r in rows if r["event"] == "weather_compensation")
        assert weather["raw_curve"] == weather["manual_curve"] == 1
        assert weather["effective_curve"] == 2 and weather["outdoor_temp"] == 0
        assert weather["feed_forward"] == pytest.approx(16)
        assert weather["pid_demand"] == pytest.approx(2)
        assert weather["demand"] == pytest.approx(result)
    finally:
        telemetry.set_recorder(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("current,outdoor,curve,expected_ff,applied", [
    (18.0, 0.0, 2.0, 40.0, True),
    (20.1, 0.0, 2.0, 0.0, True),
    (19.8, None, 2.0, 0.0, False),
    (19.8, 25.0, 2.0, 0.0, False),
    (19.8, 0.0, 0.0, 0.0, False),
])
async def test_weather_observation_covers_feed_forward_boundaries(tmp_path, current, outdoor, curve, expected_ff, applied):
    """ASHRAE Addendum y §5.21.4: adapted weather reset observations, not its sequence."""
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    try:
        coord, _ = _create_mock_coordinator()
        coord.get_persistent_data.side_effect = lambda key, default: curve if key == "weather_curve" else default
        coord.weather_sensor_id = "sensor.outdoor"
        controller = ZoneController(MagicMock(), coord)
        cid = make_zone_entity_id("Salone")
        controller.get_autotuner(cid).state = controller.get_autotuner(cid).STATE_COMPLETED
        controller.get_pid(cid).set_pid_param(kp=10, ki=0, kd=0)
        controller._outdoor_temp = MagicMock(return_value=outdoor)
        controller._thermal_models[cid].get_weather_multiplier = MagicMock(return_value=curve)
        result = await controller.async_update_zone(cid, DummyState(current_temp=current), learn=False)
        recorder.flush()
        records = list(telemetry.read_records([str(p) for p in Path(tmp_path).glob("*.jsonl")]))
        events = [r["data"] for r in records if r["kind"] == "event"
                  and r["data"].get("event") == "weather_compensation"]
        assert len(events) == 1
        assert events[0]["applied"] is applied
        assert events[0]["feed_forward"] == pytest.approx(expected_ff)
        assert events[0]["demand"] == pytest.approx(result)
        telemetry.set_recorder(None)
        replayed = await replay_weather_compensation_from_records(records)
        assert replayed[0][1] == pytest.approx(result)
        missing = deepcopy(records)
        next(row["data"] for row in missing if row["data"].get("event") == "weather_compensation").pop("raw_curve")
        with pytest.raises(ValueError, match="lacks replay inputs"):
            await replay_weather_compensation_from_records(missing)
        divergent = deepcopy(records)
        next(row["data"] for row in divergent if row["data"].get("event") == "weather_compensation")["demand"] += 1
        with pytest.raises(ValueError, match="diverged"):
            await replay_weather_compensation_from_records(divergent)
    finally:
        telemetry.set_recorder(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("current,previous,expected", [
    (19.7, 42, 100), (20.3, 42, 0), (20, 42, 42), (19.8, 0, 0), (20.2, 100, 100),
])
async def test_hysteresis_replays_captured_previous_demand(tmp_path, current, previous, expected):
    """ASHRAE Addendum y §5.21.11: adapted learning-mode decision recording."""
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    try:
        coord, demands = _create_mock_coordinator()
        controller = ZoneController(MagicMock(), coord)
        cid = make_zone_entity_id("Salone")
        demands[cid] = previous
        result = await controller.async_update_zone(cid, DummyState(current_temp=current), learn=False)
        assert result == expected
        recorder.flush()
        rows = list(telemetry.read_records([str(p) for p in Path(tmp_path).glob("*.jsonl")]))
        telemetry.set_recorder(None)
        assert (await replay_hysteresis_from_records(rows))[0][1] == expected
        missing = deepcopy(rows)
        next(row["data"] for row in missing if row["data"].get("event") == "hysteresis_demand").pop("previous_demand")
        with pytest.raises(ValueError, match="lacks replay inputs"):
            await replay_hysteresis_from_records(missing)
    finally:
        telemetry.set_recorder(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "missing_temperature", "invalid_temperature", "unregistered_zone", "critical_sensor_fault", "inactive_mode",
])
async def test_zone_control_early_exit_is_observable(tmp_path, reason):
    """ASHRAE Addendum y §5.21.11: adapted missing-input and operating-state observation."""
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    try:
        coord, demands = _create_mock_coordinator()
        controller = ZoneController(MagicMock(), coord)
        cid = make_zone_entity_id("Salone")
        demands[cid] = 50
        state = DummyState()
        if reason == "missing_temperature":
            state.attributes["current_temperature"] = None
        elif reason == "invalid_temperature":
            state.attributes["current_temperature"] = "unknown"
        elif reason == "unregistered_zone":
            controller._pids.pop(cid)
        elif reason == "critical_sensor_fault":
            coord.plant_diagnostics.has_critical_sensor_fault.return_value = True
        else:
            state.state = "off"
        assert await controller.async_update_zone(cid, state, learn=False) == 0
        recorder.flush()
        records = list(telemetry.read_records([str(p) for p in Path(tmp_path).glob("*.jsonl")]))
        rows = [r["data"] for r in records if r["data"].get("event") == "zone_control_skipped"]
        assert len(rows) == 1 and rows[0]["reason"] == reason
        assert rows[0]["demand_updated"] is (reason != "unregistered_zone")
        assert demands[cid] == (50 if reason == "unregistered_zone" else 0)
        telemetry.set_recorder(None)
        replayed = (await replay_zone_control_skips_from_records(records))[0]
        assert replayed["result"] == 0
        assert replayed["updates"] == ([] if reason == "unregistered_zone" else [(cid, 0)])
        divergent = deepcopy(records)
        next(r["data"] for r in divergent if r["data"].get("event") == "zone_control_skipped")["demand_updated"] = False
        if reason != "unregistered_zone":
            with pytest.raises(ValueError, match="diverged"):
                await replay_zone_control_skips_from_records(divergent)
    finally:
        telemetry.set_recorder(None)
