"""ASHRAE Addendum y inspired plant simulations, not conformance or physical-flow tests."""
import pytest

from tests.mzt_replay.plant_orchestrated import replay_orchestrated_plant


@pytest.mark.asyncio
async def test_real_orchestrated_delay_and_master_shutdown_are_deterministic():
    """§4.11, §5.21.2.4: adapted delayed enable and request recheck."""
    events = [
        {"t": 1000, "demands": {"Room": 100}},
        {"t": 1010},
        {"t": 1030},
        {"t": 1040, "master": False},
    ]
    first = await replay_orchestrated_plant(events, {"Room": "switch.valve"}, valve_delay=30)
    second = await replay_orchestrated_plant(events, {"Room": "switch.valve"}, valve_delay=30)
    assert first == second
    assert first[0]["states"]["switch.valve"] == "on"
    assert first[0]["states"]["switch.boiler"] == "off" and first[0]["delay_pending"]
    assert first[2]["states"]["switch.boiler"] == "on"
    assert first[3]["states"]["switch.boiler"] == "off"


@pytest.mark.asyncio
async def test_real_orchestrated_delay_rechecks_master_before_ignition():
    """§4.11, §5.21.2.4: an adapted pending-start cancellation, not flow proof."""
    events = [
        {"t": 1000, "demands": {"Room": 100}},
        {"t": 1030, "master": False},
    ]
    result = await replay_orchestrated_plant(events, {"Room": "switch.valve"}, valve_delay=30)
    assert result[0]["delay_pending"]
    assert result[-1]["states"]["switch.boiler"] == "off"
    assert not any(entity == "switch.boiler" and command == "turn_on"
                   for _, entity, command in result[-1]["commands"])


@pytest.mark.asyncio
async def test_real_orchestrated_dhw_and_frost_overrides():
    """§5.21.2, §5.21.11: adapted domestic overrides, including DHW priority."""
    events = [
        {"t": 1000, "demands": {"Room": 100}},
        {"t": 1010, "dhw": True},
        {"t": 1020, "dhw": False, "frost": True, "master": False},
    ]
    result = await replay_orchestrated_plant(events, {"Room": "switch.valve"}, min_on=5)
    assert result[0]["states"]["switch.boiler"] == "on"
    assert result[1]["states"]["switch.boiler"] == "off"
    assert result[2]["demand"] == 100
    assert result[2]["states"]["switch.boiler"] == "on"


@pytest.mark.asyncio
async def test_real_orchestrated_unavailable_valve_and_recovery():
    """§4.11, §5.21.2.4: adapted reported path readiness and recovery."""
    events = [
        {"t": 1000, "demands": {"Room": 100}, "switches": {"switch.valve": "unavailable"}},
        {"t": 1010, "switches": {"switch.valve": "off"}},
        {"t": 1020, "tick_valves": False},
    ]
    result = await replay_orchestrated_plant(events, {"Room": "switch.valve"})
    assert result[0]["states"]["switch.boiler"] == "off"
    assert result[0]["reason"].startswith("HOLD_VALVES_CLOSED")
    assert result[1]["states"]["switch.valve"] == "on"
    assert result[1]["states"]["switch.boiler"] == "on"
