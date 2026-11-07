"""Closed-loop plant simulation: real ZoneSyncManager(s) + real RelayBoilerBackend on a manual clock.

Wires the production valve PWM (per zone) and boiler PWM/relay logic to fake switches with optional
actuation latency, and measures the physical outcomes that matter: is the boiler firing into closed
valves, and how often do relays click. Only the 10 s tick orchestration (PlantOrchestrator) is
replaced by a minimal equivalent so that demand can be scripted.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable
from unittest.mock import MagicMock

from homeassistant.components.climate import HVACMode

from custom_components.multizone_thermostat import pwm_engine as pkg_pwm_engine
from custom_components.multizone_thermostat.boiler import relay_backend as relay_mod
from custom_components.multizone_thermostat.sync_manager import ZoneSyncManager

from .clock import ManualClock, patched_time

TICK = 10.0


class SimSwitches:
    """Stateful switches whose state changes `latency` seconds after the command."""

    def __init__(self, clock: ManualClock, names, latency: dict[str, float] | None = None):
        self.clock = clock
        self.state = {n: "off" for n in names}
        self.latency = latency or {}
        self.pending: list[tuple[float, str, str]] = []
        self.history: dict[str, list[tuple[float, str]]] = {n: [] for n in names}
        self.commands: list[tuple[float, str, str]] = []

    def get(self, entity_id):
        return MagicMock(state=self.state[entity_id], attributes={}) if entity_id in self.state else None

    async def call(self, domain, service, data, **kwargs):
        entity = data["entity_id"]
        self.commands.append((self.clock.now, entity, service))
        self.pending.append((self.clock.now + self.latency.get(entity, 0.0), entity,
                             "on" if service == "turn_on" else "off"))
        self.settle()

    def settle(self):
        due = [p for p in self.pending if p[0] <= self.clock.now]
        self.pending = [p for p in self.pending if p[0] > self.clock.now]
        for _, entity, value in sorted(due):
            if self.state[entity] != value:
                self.state[entity] = value
                self.history[entity].append((self.clock.now, value))


@dataclass
class PlantResult:
    duration: float
    boiler_on_s: float = 0.0
    boiler_into_closed_s: float = 0.0
    boiler_transitions: int = 0
    boiler_runs: list[float] = field(default_factory=list)
    valve_transitions: dict[str, int] = field(default_factory=dict)
    valve_commands: int = 0
    boiler_commands: int = 0
    timeline: list[tuple[float, bool, dict[str, bool]]] = field(default_factory=list)

    @property
    def into_closed_fraction(self) -> float:
        return self.boiler_into_closed_s / self.boiler_on_s if self.boiler_on_s else 0.0


def run_plant(
    demand_of: Callable[[float, str], float],
    zones: dict[str, str],
    *,
    hours: float = 6.0,
    boiler_min_on_min: float = 5.0,
    boiler_min_off_min: float = 5.0,
    valve_delay: float = 0.0,
    valve_latency: float = 0.0,
    boiler_latency: float = 0.0,
) -> PlantResult:
    """`zones` maps zone name -> valve switch entity. `demand_of(t, zone)` gives that zone's demand %."""
    clock = ManualClock(start=100000.0)
    boiler_switch = "switch.boiler"
    sw = SimSwitches(clock, [boiler_switch, *zones.values()],
                     {**{v: valve_latency for v in zones.values()}, boiler_switch: boiler_latency})

    hass = MagicMock()
    hass.states.get = sw.get
    hass.services.async_call = sw.call

    names = {name: f"climate.multizone_thermostat_{name.lower()}" for name in zones}
    coordinator = MagicMock()
    coordinator.zones = [{"name": n, "switches": [v], "climates": []} for n, v in zones.items()]
    coordinator.get_master_state.return_value = True
    coordinator.is_dhw_active = False
    coordinator.get_zone_mode.return_value = "primary"
    coordinator.get_zone_demand.side_effect = lambda cid: demand_of(
        clock.now - 100000.0, next(n for n, c in names.items() if c == cid))
    coordinator.plant_diagnostics._current_boiler_start = None

    managers = {}
    for name, valve in zones.items():
        zone_coordinator = MagicMock()
        zone_coordinator.get_zone_demand.side_effect = lambda cid, n=name: demand_of(clock.now - 100000.0, n)
        zone_coordinator.get_zone_mode.return_value = "primary"
        managers[name] = ZoneSyncManager(hass=hass, coordinator=zone_coordinator, zone_name=name,
                                         entity_id=names[name], climates=[], switches=[valve], calibrations={})

    backend = relay_mod.RelayBoilerBackend(hass, coordinator, boiler_switch, boiler_min_on_min,
                                           boiler_min_off_min, valve_delay)
    result = PlantResult(duration=hours * 3600)
    last_on, run_start = False, None

    async def drive():
        nonlocal last_on, run_start
        steps = int(hours * 3600 / TICK)
        for _ in range(steps):
            clock.advance(TICK)
            sw.settle()
            for name, mgr in managers.items():
                await mgr.async_pwm_tick(HVACMode.HEAT)
            demands = {n: demand_of(clock.now - 100000.0, n) for n in zones}
            peak = max(demands, key=demands.get)
            await backend.async_update(demands[peak], peak, False)
            sw.settle()
            boiler_on = sw.state[boiler_switch] == "on"
            valves = {n: sw.state[v] == "on" for n, v in zones.items()}
            demanding_open = any(valves[n] for n in zones if demands[n] > 0)
            if boiler_on:
                result.boiler_on_s += TICK
                if not demanding_open:
                    result.boiler_into_closed_s += TICK
            if boiler_on != last_on:
                result.boiler_transitions += 1
                if boiler_on:
                    run_start = clock.now
                elif run_start is not None:
                    result.boiler_runs.append(clock.now - run_start)
                last_on = boiler_on
            result.timeline.append((clock.now, boiler_on, valves))
        for task in (backend._pending_boiler_task,):
            if task:
                task.cancel()

    patches = (patched_time(pkg_pwm_engine, clock), patched_time(relay_mod, clock))
    with patches[0], patches[1]:
        asyncio.run(drive())

    result.boiler_commands = sum(1 for _, e, _ in sw.commands if e == boiler_switch)
    result.valve_commands = sum(1 for _, e, _ in sw.commands if e != boiler_switch)
    result.valve_transitions = {n: len(sw.history[v]) for n, v in zones.items()}
    return result
