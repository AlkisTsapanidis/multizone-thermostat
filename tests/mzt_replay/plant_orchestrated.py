"""Deterministic synthetic replay of the real relay plant orchestration path."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate import HVACMode
from custom_components.multizone_thermostat import pwm_engine
from custom_components.multizone_thermostat.boiler import relay_backend
from custom_components.multizone_thermostat.boiler.plant_orchestrator import PlantOrchestrator
from custom_components.multizone_thermostat.const import make_zone_entity_id
from custom_components.multizone_thermostat.demand_aggregator import DemandAggregator
from custom_components.multizone_thermostat.sync_manager import ZoneSyncManager

from .clock import ManualClock, patched_time
from .plant_sim import SimSwitches


async def replay_orchestrated_plant(events, zones, *, valve_delay=0.0, min_on=0.0, min_off=0.0,
                                    switch_latency=None):
    """Run ordered synthetic events against real aggregation, orchestration, PWM and relay code.

    Events specify monotonic ``t``, optional ``demands`` by zone name, ``modes`` by zone name,
    ``master``, ``dhw``, ``frost``, and observed ``switches``. Each event then runs a valve and
    plant tick unless ``tick_valves`` or ``tick_plant`` is false. Simulated switch states and
    scheduled delays are not physical feedback.
    """
    clock = ManualClock()
    switches = SimSwitches(clock, ["switch.boiler", *zones.values()], switch_latency)
    hass = MagicMock()
    hass.states.get = switches.get
    hass.services.async_call = switches.call
    tasks = []

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    hass.async_create_task = create_task
    aggregator = DemandAggregator([{"name": name, "switch_entities": [switch]} for name, switch in zones.items()])
    ids = {name: make_zone_entity_id(name) for name in zones}
    settings = {"master": True, "dhw": False, "frost": False}
    diag = SimpleNamespace(_current_boiler_start=None)

    def record_boiler_state(on):
        diag._current_boiler_start = clock.now if on else None

    diag.record_boiler_state = record_boiler_state
    coordinator = SimpleNamespace(
        zones=[{"name": name, "switch_entities": [switch]} for name, switch in zones.items()],
        _climate_entities={}, _climate_callbacks={}, is_dhw_active=False,
        plant_diagnostics=diag, get_master_state=lambda: settings["master"],
        get_zone_mode=aggregator.get_zone_mode, get_zone_demand=aggregator.get_zone_demand,
        reset_last_active_time=lambda: None, record_diagnostic_event=lambda *_: None,
        frost_manager=SimpleNamespace(async_handle_frost_emergencies=AsyncMock(
            side_effect=lambda: settings["frost"])),
    )
    backend = relay_backend.RelayBoilerBackend(hass, coordinator, "switch.boiler", min_on, min_off, valve_delay)
    plant = PlantOrchestrator(hass, aggregator, backend, coordinator)
    managers = {
        name: ZoneSyncManager(hass, coordinator, name, ids[name], [], [switch], {})
        for name, switch in zones.items()
    }
    waiters = []

    async def sleep(seconds):
        future = asyncio.get_running_loop().create_future()
        waiters.append((clock.now + seconds, future))
        await future

    async_shim = SimpleNamespace(sleep=sleep, current_task=asyncio.current_task, CancelledError=asyncio.CancelledError)
    old_asyncio = relay_backend.asyncio
    result = []
    try:
        relay_backend.asyncio = async_shim
        with patched_time(pwm_engine, clock), patched_time(relay_backend, clock):
            for event in events:
                clock.set(event["t"])
                for key in ("master", "dhw", "frost"):
                    if key in event:
                        settings[key] = event[key]
                coordinator.is_dhw_active = settings["dhw"]
                for name, demand in event.get("demands", {}).items():
                    aggregator.set_zone_demand(ids[name], demand)
                for name, mode in event.get("modes", {}).items():
                    aggregator.set_zone_mode(ids[name], mode)
                switches.settle()
                for entity, state in event.get("switches", {}).items():
                    if entity not in switches.state:
                        raise ValueError(f"Unknown switch {entity}")
                    switches.state[entity] = state
                for deadline, future in waiters:
                    if deadline <= clock.now and not future.done():
                        future.set_result(None)
                await asyncio.sleep(0)
                if event.get("tick_valves", True):
                    for manager in managers.values():
                        await manager.async_pwm_tick(HVACMode.HEAT if settings["master"] else HVACMode.OFF)
                if event.get("tick_plant", True):
                    await plant.async_tick(None)
                await asyncio.sleep(0)
                switches.settle()
                result.append({
                    "t": clock.now, "states": switches.state.copy(), "reason": backend.status_reason,
                    "commands": list(switches.commands), "demand": backend._last_demand,
                    "delay_pending": backend._pending_boiler_task is not None,
                })
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        relay_backend.asyncio = old_asyncio
    return result
