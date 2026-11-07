"""Tests for Calendar Manager."""

import asyncio
import copy
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone

import homeassistant.util.dt as dt_util
import pytest

from custom_components.multizone_thermostat.calendar_manager import CalendarManager
from custom_components.multizone_thermostat.const import CONF_ZONE_NAME
from custom_components.multizone_thermostat import telemetry
from tests.mzt_replay.drivers import verify_calendar_override_lifecycle_from_records

@pytest.fixture
def mock_coordinator():
    coord = MagicMock()
    coord.hass = MagicMock()
    coord.hass.services = AsyncMock()
    coord.hass.services.async_call = AsyncMock()
    coord.global_calendar_id = "calendar.test"
    coord.zones = [{CONF_ZONE_NAME: "Living Room"}, {CONF_ZONE_NAME: "Bedroom"}]
    
    # Mocks for Zone Manager
    coord.zone_manager = MagicMock()
    coord.zone_manager.current_global_preset = "eco"
    coord.zone_manager._presets = {
        "comfort": {"climate.multizone_thermostat_living_room": {"target_temp": 22.0}}
    }
    coord.zone_manager.async_set_global_preset = AsyncMock()
    coord.async_set_global_preset = coord.zone_manager.async_set_global_preset
    coord._async_save_presets_storage = AsyncMock()
    
    coord._select_entities = {}
    
    # Climate callbacks
    cb1 = MagicMock()
    cb2 = MagicMock()
    coord._climate_callbacks = {
        "climate.multizone_thermostat_living_room": cb1,
        "climate.multizone_thermostat_bedroom": cb2
    }
    
    # Thermal models
    tm = MagicMock()
    tm.heating_rate = 1.0 # 1 deg per hour
    coord._thermal_models = {
        "climate.multizone_thermostat_living_room": tm
    }
    
    return coord


@pytest.mark.asyncio
async def test_calendar_override_snapshots_do_not_expose_titles(mock_coordinator, tmp_path):
    """ASHRAE Addendum y §5.21.11: adapted operating override visibility."""
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    try:
        manager = CalendarManager(mock_coordinator)
        now = datetime.now(timezone.utc)
        await manager._async_process_calendar_events(
            [{"start": now.isoformat(), "summary": "Living Room: 21 bypass"}], None, now,
        )
        await manager._async_process_calendar_events([], None, now)
        recorder.flush()
        rows = [r["data"] for r in telemetry.read_records([str(p) for p in Path(tmp_path).glob("*.jsonl")])
                if r["data"].get("event") == "calendar_overrides"]
        zone = "climate.multizone_thermostat_living_room"
        assert len(rows) == 2
        assert rows[0]["active_event"] and rows[0]["current"] == {zone: 21}
        assert rows[0]["modes"] == {zone: "bypass"}
        assert not rows[1]["active_event"] and rows[1]["current"] == {} and rows[1]["modes"] == {}
        assert "summary" not in rows[0] and "active_event_id" not in rows[0]
    finally:
        telemetry.set_recorder(None)


@pytest.mark.asyncio
async def test_calendar_processing_lifecycle_and_privacy(mock_coordinator, tmp_path):
    """ASHRAE Addendum y §5.21.11: adapted override lifecycle observation."""
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    try:
        manager = CalendarManager(mock_coordinator)
        now = datetime.now(timezone.utc)
        start = now.isoformat()
        title = "Living Room: 21 bypass secret-private-event"
        future_title = "Living Room: 22 private-future-event"
        future = {"start": (now + timedelta(minutes=30)).isoformat(), "summary": future_title}
        state = MagicMock()
        state.attributes = {"current_temperature": 20.0}
        mock_coordinator.hass.states.get.return_value = state
        current = [{"start": start, "summary": title}]
        with patch.object(manager, "_parse_event_datetime", return_value=now + timedelta(minutes=30)):
            await manager._async_process_calendar_events(current, future, now)
            await manager._async_process_calendar_events(current, future, now)
            await manager._async_process_calendar_events(
                [{"start": start, "summary": "Bedroom: 19"}], None, now,
            )
            await manager._async_process_calendar_events([], None, now)
        recorder.flush()
        files = list(Path(tmp_path).glob("*.jsonl"))
        raw = "".join(p.read_text(encoding="utf-8") for p in files)
        assert title not in raw and future_title not in raw and f"{start}_{title}" not in raw
        rows = list(telemetry.read_records([str(p) for p in files]))
        steps = verify_calendar_override_lifecycle_from_records(rows)
        assert len(steps) == 4
        states = [item["end"]["data"]["state"] for item in steps]
        assert states[0]["active_event_token"] == states[1]["active_event_token"]
        assert states[2]["active_event_token"] != states[1]["active_event_token"]
        assert states[3]["active_event_token"] is None
        assert states[0]["smart_start"] == {"climate.multizone_thermostat_living_room": 22.0}
        assert states[0]["current"] != states[2]["current"]

        missing_step = [r for r in rows if r["data"].get("step") != 1]
        with pytest.raises(ValueError, match="missing or duplicate"):
            verify_calendar_override_lifecycle_from_records(missing_step)
        altered = copy.deepcopy(rows)
        next_start = next(r for r in altered if r["data"].get("event") == "calendar_processing_start"
                          and r["data"].get("step") == 1)
        next_start["data"]["state"]["current"] = {}
        with pytest.raises(ValueError, match="divergent previous state"):
            verify_calendar_override_lifecycle_from_records(altered)
        reordered = list(reversed(rows))
        with pytest.raises(ValueError, match="reordered"):
            verify_calendar_override_lifecycle_from_records(reordered)
        incomplete = copy.deepcopy(rows)
        next(r for r in incomplete if r["data"].get("event") == "calendar_overrides")["data"].pop(
            "pre_calendar_preset"
        )
        with pytest.raises(ValueError, match="snapshot diverged"):
            verify_calendar_override_lifecycle_from_records(incomplete)
        with pytest.raises(ValueError, match="no processing events"):
            verify_calendar_override_lifecycle_from_records([])
    finally:
        telemetry.set_recorder(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("private event detail"), asyncio.CancelledError()])
async def test_calendar_processing_interruption_is_recorded_and_rejected(mock_coordinator, tmp_path, failure):
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    try:
        manager = CalendarManager(mock_coordinator)
        mock_coordinator.zone_manager.async_set_global_preset.side_effect = failure
        now = datetime.now(timezone.utc)
        with pytest.raises(type(failure)):
            await manager._async_process_calendar_events([{"start": now.isoformat(), "summary": "comfort"}], None, now)
        recorder.flush()
        rows = list(telemetry.read_records([str(p) for p in Path(tmp_path).glob("*.jsonl")]))
        end = next(r["data"] for r in rows if r["data"].get("event") == "calendar_processing_end")
        assert end["completed"] is False
        assert end["cancelled"] is isinstance(failure, asyncio.CancelledError)
        assert "private event detail" not in json.dumps(rows)
        with pytest.raises(ValueError, match="incomplete|interrupted"):
            verify_calendar_override_lifecycle_from_records(rows)
    finally:
        telemetry.set_recorder(None)


@pytest.mark.asyncio
async def test_calendar_processing_disabled_recording_gap_is_detected(mock_coordinator, tmp_path):
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    manager = CalendarManager(mock_coordinator)
    now = datetime.now(timezone.utc)
    try:
        telemetry.set_recorder(recorder)
        await manager._async_process_calendar_events([], None, now)
        telemetry.set_recorder(None)
        await manager._async_process_calendar_events([], None, now)
        telemetry.set_recorder(recorder)
        await manager._async_process_calendar_events([], None, now)
        recorder.flush()
        rows = list(telemetry.read_records([str(p) for p in Path(tmp_path).glob("*.jsonl")]))
        with pytest.raises(ValueError, match="missing or duplicate"):
            verify_calendar_override_lifecycle_from_records(rows)
    finally:
        telemetry.set_recorder(None)


@pytest.mark.asyncio
async def test_calendar_identity_tokens_are_keyed_per_manager(mock_coordinator, tmp_path):
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    try:
        now = datetime.now(timezone.utc)
        event = [{"start": now.isoformat(), "summary": "Bedroom: 19"}]
        for _ in range(2):
            await CalendarManager(mock_coordinator)._async_process_calendar_events(event, None, now)
        recorder.flush()
        rows = list(telemetry.read_records([str(p) for p in Path(tmp_path).glob("*.jsonl")]))
        steps = verify_calendar_override_lifecycle_from_records(rows)
        first, second = [step["end"]["data"]["state"]["active_event_token"] for step in steps]
        assert first != second and len(first) == len(second) == 64
    finally:
        telemetry.set_recorder(None)


@pytest.mark.asyncio
async def test_calendar_processing_overlaps_are_rejected(mock_coordinator, tmp_path):
    recorder = telemetry.JsonlRecorder(str(tmp_path))
    telemetry.set_recorder(recorder)
    gate, entered = asyncio.Event(), asyncio.Event()
    try:
        manager = CalendarManager(mock_coordinator)
        now = datetime.now(timezone.utc)

        async def hold_first_preset(_):
            entered.set()
            await gate.wait()

        mock_coordinator.zone_manager.async_set_global_preset.side_effect = hold_first_preset
        first = asyncio.create_task(manager._async_process_calendar_events(
            [{"start": now.isoformat(), "summary": "comfort"}], None, now,
        ))
        await entered.wait()
        try:
            await manager._async_process_calendar_events(
                [{"start": now.isoformat(), "summary": "Bedroom: 19"}], None, now,
            )
        finally:
            gate.set()
            await first
        recorder.flush()
        rows = list(telemetry.read_records([str(p) for p in Path(tmp_path).glob("*.jsonl")]))
        with pytest.raises(ValueError, match="overlapping"):
            verify_calendar_override_lifecycle_from_records(rows)
    finally:
        telemetry.set_recorder(None)


@pytest.mark.asyncio
@patch('custom_components.multizone_thermostat.calendar_manager.dt_util')
async def test_calendar_no_events(mock_dt_util, mock_coordinator):
    """Test calendar parsing when no events are returned."""
    manager = CalendarManager(mock_coordinator)
    now = datetime.now(timezone.utc)
    mock_dt_util.utcnow.return_value = now
    
    # Mock empty response
    mock_coordinator.hass.services.async_call.return_value = {"calendar.test": {"events": []}}
    
    await manager.async_check_global_calendar(now)
    
    assert manager.active_event_id is None
    assert len(manager.temp_overrides) == 0


@pytest.mark.asyncio
@patch('custom_components.multizone_thermostat.calendar_manager.dt_util')
async def test_calendar_current_event(mock_dt_util, mock_coordinator):
    """Test calendar applying a current event."""
    manager = CalendarManager(mock_coordinator)
    now = datetime.now(timezone.utc)
    mock_dt_util.utcnow.return_value = now
    mock_dt_util.DEFAULT_TIME_ZONE = timezone.utc
    mock_dt_util.as_local.side_effect = lambda x: x
    mock_dt_util.parse_datetime.side_effect = lambda x: datetime.fromisoformat(x.replace('Z', '+00:00')) if x else None
    
    # Event that started 1 hour ago and ends in 1 hour
    start = (now - timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    
    # Summary with a global preset override
    mock_coordinator.hass.services.async_call.return_value = {
        "calendar.test": {
            "events": [
                {
                    "start": start,
                    "end": end,
                    "summary": "comfort"
                }
            ]
        }
    }
    
    await manager.async_check_global_calendar(now)
    
    assert manager.active_event_id == f"{start}_comfort"
    assert manager.pre_calendar_preset == "eco"
    mock_coordinator.async_set_global_preset.assert_called_once_with("comfort")
    
    # Verify callbacks were called to refresh climates
    mock_coordinator._climate_callbacks["climate.multizone_thermostat_living_room"].assert_called()


@pytest.mark.asyncio
@patch('custom_components.multizone_thermostat.calendar_manager.dt_util')
async def test_calendar_smart_start(mock_dt_util, mock_coordinator):
    """Test Smart Start triggering for an upcoming event."""
    manager = CalendarManager(mock_coordinator)
    now = datetime.now(timezone.utc)
    mock_dt_util.utcnow.return_value = now
    mock_dt_util.DEFAULT_TIME_ZONE = timezone.utc
    mock_dt_util.as_local.side_effect = lambda x: x
    mock_dt_util.parse_datetime.side_effect = lambda x: datetime.fromisoformat(x.replace('Z', '+00:00')) if x else None
    
    # Event starting in 30 minutes, requires heating
    start = (now + timedelta(minutes=30)).isoformat()
    end = (now + timedelta(hours=2)).isoformat()
    
    # Mock current temperature in HA to be 20.0
    state_mock = MagicMock()
    state_mock.attributes = {"current_temperature": 20.0}
    mock_coordinator.hass.states.get.return_value = state_mock
    
    # Summary specifies global temp 22.0
    mock_coordinator.hass.services.async_call.return_value = {
        "calendar.test": {
            "events": [
                {
                    "start": start,
                    "end": end,
                    "summary": "22.0"
                }
            ]
        }
    }
    
    await manager.async_check_global_calendar(now)
    
    # Delta T is 2.0. Heating rate is 1.0 deg/h. Needs 2 hours.
    # Start is in 30 mins (0.5 h). We have less time than needed (0.5 < 2.0).
    # So we should start heating NOW (Smart Start Triggered)
    
    assert "climate.multizone_thermostat_living_room" in manager.temp_overrides
    assert manager.temp_overrides["climate.multizone_thermostat_living_room"] == 22.0
    
    # Verify callback triggered to wake up zone
    mock_coordinator._climate_callbacks["climate.multizone_thermostat_living_room"].assert_called()
