"""Drivers that replay inputs through production numerical code and emit telemetry."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Iterable, Sequence

from .clock import ManualClock, patched_time
from .loader import load_prod_module
from .synthetic import Sample, to_datetime


@dataclass
class PwmTick:
    t: float
    demand: float
    state: bool
    time_on: float
    time_off: float


def replay_pwm(demands: Iterable[tuple[float, float]], *, pwm_interval: float = 900.0,
               min_on: float = 0.0, min_off: float = 0.0, recorder=None, zone: str | None = None,
               clock: ManualClock | None = None) -> list[PwmTick]:
    """Feed (t, demand) pairs to the real PWMEngine at the given times and trace its decisions.

    Pass the same `clock` to a JsonlRecorder (mono_clock=clock.monotonic) so logged timestamps match.
    """
    module = load_prod_module("pwm_engine")
    clock = clock or ManualClock()
    ticks: list[PwmTick] = []
    with patched_time(module, clock):
        engine = module.PWMEngine(pwm_interval=pwm_interval, min_on=min_on, min_off=min_off)
        for t, demand in demands:
            clock.set(t)
            cycle_start, prev_demand = engine.cycle_start_time, getattr(engine, "_prev_demand", 0.0)
            state = engine.calculate(demand)
            ticks.append(PwmTick(t, demand, state, engine.time_on, engine.time_off))
            if recorder is not None:
                recorder.record("pwm", zone, demand=demand, state=state, time_on=engine.time_on,
                                time_off=engine.time_off, pwm_interval=pwm_interval,
                                min_on=min_on, min_off=min_off, cycle_start_before=cycle_start,
                                prev_demand_before=prev_demand, calculation_time=engine.last_calculation_time)
    return ticks


def replay_pwm_from_records(records: Iterable[dict], zone: str | None = None,
                            kinds: tuple[str, ...] = ("pwm", "boiler"),
                            require_calculation_time: bool = False) -> list[tuple[dict, bool]]:
    """Re-run recorded PWM decisions through the current PWMEngine.

    Handles "pwm" and "boiler" records (both carry demand/state/min_on/min_off/cycle_start_before).
    `zone` selects one engine's records when a log holds several. Returns (record, replayed_state)
    pairs; the caller compares with record["data"]["state"]. Engine state before the first record is
    injected from the log. Older records fall back to their later timestamp unless strict time is
    requested; use the stateful driver to detect missing or divergent engine steps.
    """
    module = load_prod_module("pwm_engine")
    clock = ManualClock()
    out: list[tuple[dict, bool]] = []
    engine = None
    with patched_time(module, clock):
        for rec in records:
            if rec["kind"] not in kinds or (zone is not None and rec.get("zone") != zone):
                continue
            d = rec["data"]
            if d.get("demand") is None:
                continue
            if engine is None:
                engine = module.PWMEngine(d["pwm_interval"], d["min_on"], d["min_off"])
                engine.cycle_start_time = d["cycle_start_before"]
                engine._prev_demand = d["prev_demand_before"]
            engine.set_params(d["pwm_interval"], d["min_on"], d["min_off"])
            calculation_time = d.get("calculation_time")
            if calculation_time is None and require_calculation_time:
                raise ValueError(f"PWM record {rec.get('seq')} lacks the calculation time")
            clock.now = calculation_time if calculation_time is not None else rec["mono"]
            out.append((rec, engine.calculate(d["demand"])))
    return out


def replay_stateful_pwm_from_records(records: Iterable[dict], zone: str, kind: str) -> list[tuple[dict, bool]]:
    """Replay contiguous PWM engine calls, rejecting missing steps and divergent state."""
    import math

    if kind not in ("pwm", "boiler"):
        raise ValueError("PWM replay requires a pwm or boiler kind")
    module = load_prod_module("pwm_engine")
    clock = ManualClock()
    engine = None
    epoch = None
    next_step = None
    previous_time = None
    seen_epochs = set()
    rows = []

    def equal(a, b):
        return a == b or (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b))

    with patched_time(module, clock):
        for rec in records:
            if rec.get("kind") != kind or rec.get("zone") != zone:
                continue
            data = rec["data"]
            if data.get("demand") is None:
                if (kind == "pwm" and data.get("master_off") is True
                        and data.get("state") is False
                        and all(data.get(key) is None for key in ("calculation_time", "pwm_epoch", "pwm_step"))):
                    continue
                raise ValueError(f"PWM record {rec.get('seq')} lacks the demand input")
            required = ("pwm_epoch", "pwm_step", "calculation_time", "cycle_start_before",
                        "prev_demand_before")
            params = (("calculation_interval", "calculation_min_on", "calculation_min_off") if kind == "boiler"
                      else ("pwm_interval", "min_on", "min_off"))
            required += params
            if any(key not in data or data[key] is None for key in required):
                raise ValueError(f"PWM record {rec.get('seq')} lacks replay inputs")
            if not isinstance(data["pwm_epoch"], str) or not isinstance(data["pwm_step"], int):
                raise ValueError(f"PWM record {rec.get('seq')} has an invalid replay boundary")
            if data["pwm_epoch"] != epoch:
                if data["pwm_epoch"] in seen_epochs:
                    raise ValueError(f"PWM record {rec.get('seq')} has an overlapping or reordered epoch")
                epoch = data["pwm_epoch"]
                seen_epochs.add(epoch)
                engine = module.PWMEngine(*(data[key] for key in params))
                engine.cycle_start_time = data["cycle_start_before"]
                engine._prev_demand = data["prev_demand_before"]
                next_step = data["pwm_step"]
                previous_time = None
            if data["pwm_step"] != next_step:
                raise ValueError(f"PWM record {rec.get('seq')} has a missing or duplicate step")
            if previous_time is not None and data["calculation_time"] < previous_time:
                raise ValueError(f"PWM record {rec.get('seq')} has a reordered clock")
            if not equal(engine.cycle_start_time, data["cycle_start_before"]) or not equal(
                getattr(engine, "_prev_demand", 0.0), data["prev_demand_before"]
            ):
                raise ValueError(f"PWM record {rec.get('seq')} has divergent previous state")
            engine.set_params(*(data[key] for key in params))
            clock.now = data["calculation_time"]
            state = engine.calculate(data["demand"])
            state_key = "calculation_state" if kind == "boiler" else "state"
            on_key = "calculation_time_on" if kind == "boiler" else "time_on"
            off_key = "calculation_time_off" if kind == "boiler" else "time_off"
            if any(key not in data for key in (state_key, on_key, off_key)) or (
                not equal(state, data[state_key]) or not equal(engine.time_on, data[on_key])
                or not equal(engine.time_off, data[off_key])
            ):
                raise ValueError(f"PWM record {rec.get('seq')} diverged during replay")
            rows.append((rec, state))
            next_step += 1
            previous_time = data["calculation_time"]
    return rows


def constant_rate_ticks(fn, duration_s: float, tick_s: float = 10.0) -> list[tuple[float, float]]:
    """Build (t, demand) pairs by sampling fn(t) every tick_s."""
    n = int(duration_s // tick_s)
    return [(i * tick_s, fn(i * tick_s)) for i in range(1, n + 1)]


@dataclass
class RunLengths:
    on: list[float]
    off: list[float]
    transitions: int


def run_lengths(ticks: Sequence[PwmTick], tick_s: float = 10.0) -> RunLengths:
    """Durations of completed ON and OFF runs (the first and last, truncated, runs are excluded)."""
    on: list[float] = []
    off: list[float] = []
    transitions = 0
    if not ticks:
        return RunLengths(on, off, 0)
    run_start = ticks[0].t
    current = ticks[0].state
    first = True
    for tk in ticks[1:]:
        if tk.state != current:
            transitions += 1
            if not first:
                (on if current else off).append(tk.t - run_start)
            first = False
            run_start = tk.t
            current = tk.state
    return RunLengths(on, off, transitions)


def replay_estimator(estimator, samples: Iterable[Sample], recorder=None, zone: str | None = None) -> list[tuple[float, float, float]]:
    """Feed samples to an estimator; return (t, heating_rate, cooling_rate) after every update."""
    trace = []
    for s in samples:
        estimator.update(to_datetime(s.t), s.temp, s.heating)
        trace.append((s.t, estimator.heating_rate, estimator.cooling_rate))
        if recorder is not None:
            recorder.record("estimator", zone, temp=s.temp, is_heating=s.heating,
                            heating_rate=estimator.heating_rate, cooling_rate=estimator.cooling_rate, sim_t=s.t)
    return trace


def replay_estimator_from_records(records: Iterable[dict], zone: str) -> list[tuple[dict, float, float]]:
    """Replay contiguous fresh estimator epochs; reject older logs and incomplete state."""
    from custom_components.multizone_thermostat.rate_estimators import (
        LegacyEmaEstimator, create_estimator, load_estimator_state,
    )

    epoch = None
    expected_step = 0
    estimator = None
    rows = []
    for rec in records:
        if rec.get("kind") != "estimator" or rec.get("zone") != zone:
            continue
        data = rec["data"]
        required = ("replay_epoch", "replay_step", "replay_fresh", "estimator_type",
                    "replay_seed", "temp_delta_threshold", "ts", "temp", "is_heating", "outdoor_temp", "estimator")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"Estimator record {rec.get('seq')} missing replay fields: {missing}")
        if data["replay_epoch"] != epoch:
            if data["replay_step"] != 0 or not data["replay_fresh"]:
                raise ValueError(f"Estimator record {rec.get('seq')} does not start at a fresh boundary")
            epoch = data["replay_epoch"]
            expected_step = 0
            if data["estimator"] == "legacy":
                estimator = LegacyEmaEstimator(zone, data["temp_delta_threshold"])
            else:
                estimator = create_estimator(data["estimator"], zone)
            if type(estimator).__name__ != data["estimator_type"]:
                raise ValueError(f"Estimator implementation mismatch at record {rec.get('seq')}")
            if data["replay_seed"] is not None:
                seed = data["replay_seed"]
                estimator.heating_rate = seed["heating_rate"]
                estimator.cooling_rate = seed["cooling_rate"]
                load_estimator_state(estimator, seed["estimator_state"])
        if data["replay_step"] != expected_step:
            raise ValueError(f"Estimator record {rec.get('seq')} has a missing or duplicate step")
        expected_step += 1
        set_outdoor = getattr(estimator, "set_outdoor", None)
        if callable(set_outdoor):
            set_outdoor(data["outdoor_temp"])
        estimator.update(datetime.fromtimestamp(data["ts"], timezone.utc), data["temp"], data["is_heating"])
        rows.append((rec, estimator.heating_rate, estimator.cooling_rate))
    return rows


def _recorded_weight_getter(aggregator, data, seq):
    from collections import deque

    if "weight_calls" not in data:
        raise ValueError(f"Aggregation record {seq} lacks effective weight calls")
    weights = deque(data["weight_calls"])

    def get_weight(cid):
        if not weights or weights[0]["id"] != cid:
            raise ValueError(f"Aggregation record {seq} has missing or divergent weight calls")
        return weights.popleft()["weight"]

    aggregator.get_zone_weight = get_weight
    return weights


def replay_aggregation_from_records(records: Iterable[dict]) -> list[tuple[dict, object]]:
    """Replay captured effective aggregation inputs through the production aggregator."""
    from custom_components.multizone_thermostat.demand_aggregator import DemandAggregator

    rows = []
    for rec in records:
        if rec.get("kind") != "aggregation":
            continue
        data = rec["data"]
        aggregator = DemandAggregator(strategy=data["strategy"])
        for zone in data["zones"]:
            cid = aggregator.register_zone(zone["name"], climate_id=zone["id"],
                                           weight=zone["configured_weight"], mode=zone["mode"])
            aggregator.set_zone_demand(cid, zone["effective_demand"])
        weights = _recorded_weight_getter(aggregator, data, rec.get("seq"))
        result = aggregator.calculate(is_dhw_active=data["dhw_active"], frost_emergency=data["frost_emergency"])
        if weights:
            raise ValueError(f"Aggregation record {rec.get('seq')} has unconsumed weight calls")
        rows.append((rec, result))
    return rows


def replay_frost_checks_from_records(records: Iterable[dict]) -> list[tuple[dict, bool]]:
    """Re-run recorded frost checks through FrostManager with the observed temperature sources."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat.const import (
        KEY_ANTI_FROST_ENABLED, KEY_FROST_PROTECTION_TEMP,
    )
    from custom_components.multizone_thermostat.safety.frost import FrostManager

    rows = []
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "frost_check":
            continue
        if "enabled" not in data:
            raise ValueError(f"Frost record {rec.get('seq')} lacks enabled state")
        if not data["enabled"]:
            coordinator = SimpleNamespace(get_persistent_data=lambda *_: False)
            rows.append((rec, FrostManager(MagicMock(), coordinator).is_zone_in_frost_emergency(rec["zone"])))
            continue
        required = ("temperature", "source", "sensor_fault", "trv_samples", "result")
        if any(key not in data for key in required):
            raise ValueError(f"Frost record {rec.get('seq')} lacks replay inputs")
        if data["temperature"] is not None and "threshold" not in data:
            raise ValueError(f"Frost record {rec.get('seq')} lacks its threshold")
        zone_id = rec["zone"]
        zone_temp = data["temperature"] if data["source"] == "zone" else None
        states = {zone_id: SimpleNamespace(attributes={"current_temperature": zone_temp})}
        for sample in data["trv_samples"]:
            states[sample["entity"]] = SimpleNamespace(attributes={"current_temperature": sample["temperature"]})
        hass = MagicMock()
        hass.states.get = states.get
        coordinator = SimpleNamespace(
            get_persistent_data=lambda key, default: {
                KEY_ANTI_FROST_ENABLED: True,
                KEY_FROST_PROTECTION_TEMP: data.get("threshold", default),
            }.get(key, default),
            plant_diagnostics=SimpleNamespace(has_critical_sensor_fault=lambda _: data["sensor_fault"]),
            zone_manager=SimpleNamespace(get_zone=lambda _: {
                "climate_entities": [sample["entity"] for sample in data["trv_samples"]],
            }),
        )
        rows.append((rec, FrostManager(hass, coordinator).is_zone_in_frost_emergency(zone_id)))
    return rows


async def replay_proportional_valves_from_records(records: Iterable[dict]) -> list[dict]:
    """Replay each valve decision from its recorded pre-state, not a connected plant history."""
    import asyncio
    import math
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from homeassistant.components.climate import HVACMode
    from custom_components.multizone_thermostat import sync_manager as module

    def equal(left, right):
        if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
            return True
        if isinstance(left, dict) and isinstance(right, dict):
            return left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
        return left == right

    results = []
    for rec in records:
        if rec.get("kind") != "valve":
            continue
        data = rec.get("data", {})
        required = ("entity", "zone_entity", "raw_demand", "master_off", "scale", "zone_mode",
                    "previous_command", "state_exists", "observed", "target_pct", "action",
                    "replay_epoch", "replay_step", "valve_index", "valve_count")
        if any(key not in data for key in required) or "zone" not in rec:
            raise ValueError(f"Valve record {rec.get('seq')} lacks replay inputs")
        action = data["action"]
        if action not in ("skipped_unavailable", "deadband", "command_attempt", "unsupported_domain"):
            raise ValueError(f"Valve record {rec.get('seq')} has an unsupported action")
        if action in ("command_attempt", "unsupported_domain") and (
            "command_error" not in data or "command_completed" not in data or "command_cancelled" not in data
        ):
            raise ValueError(f"Valve record {rec.get('seq')} lacks command outcome")
        if action == "command_attempt" and not any(
            (data["command_completed"], data["command_cancelled"], data["command_error"] is not None)
        ):
            raise ValueError(f"Valve record {rec.get('seq')} has an unsupported incomplete outcome")
        if action != "skipped_unavailable" and "target_value" not in data:
            raise ValueError(f"Valve record {rec.get('seq')} lacks target value")
        if not data["master_off"] and data["zone_mode"] != "bypass" and data["raw_demand"] is None:
            raise ValueError(f"Valve record {rec.get('seq')} lacks demand")

        emitted = []
        commands = []
        cancelled = False

        class Capture:
            def record(self, kind, zone, **payload):
                emitted.append({"kind": kind, "zone": zone, "data": payload})

        async def service(domain, name, payload, **kwargs):
            commands.append((domain, name, dict(payload)))
            if data.get("command_error") is not None:
                raise RuntimeError(data["command_error"])
            if data.get("command_cancelled"):
                raise asyncio.CancelledError

        hass = MagicMock()
        hass.states.get.return_value = (
            SimpleNamespace(state=data["observed"]) if data["state_exists"] else None
        )
        hass.services.async_call = service
        coordinator = SimpleNamespace(
            get_zone_mode=lambda _: data["zone_mode"],
            get_zone_demand=lambda _: data["raw_demand"],
        )
        manager = module.ZoneSyncManager(
            hass, coordinator, rec["zone"], data["zone_entity"], [], [], {},
            valves=[data["entity"]], valve_scale=data["scale"],
        )
        manager._valve_replay_epoch = data["replay_epoch"]
        manager._valve_replay_step = data["replay_step"]
        if data["previous_command"] is not None:
            manager._last_valve_positions[data["entity"]] = data["previous_command"]
        original = module.telemetry
        module.telemetry = SimpleNamespace(KIND_VALVE=original.KIND_VALVE, get_recorder=lambda: Capture())
        try:
            try:
                await manager.async_sync_proportional_valves(HVACMode.OFF if data["master_off"] else HVACMode.HEAT)
            except asyncio.CancelledError:
                cancelled = True
        finally:
            module.telemetry = original
        if emitted:
            emitted[0]["data"]["valve_index"] = data["valve_index"]
            emitted[0]["data"]["valve_count"] = data["valve_count"]
        if len(emitted) != 1 or not equal(emitted[0]["data"], data):
            raise ValueError(f"Valve record {rec.get('seq')} diverged during replay")
        results.append({
            "record": rec, "commands": commands, "cancelled": cancelled,
            "last_command": manager._last_valve_positions.get(data["entity"]),
        })
    return results


async def replay_stateful_proportional_valves_from_records(records: Iterable[dict]) -> list[dict]:
    """Replay contiguous valve calls, checking state continuity across recorded decisions."""
    from collections import defaultdict
    import math

    def equal(left, right):
        return left == right or (
            isinstance(left, float) and isinstance(right, float)
            and math.isnan(left) and math.isnan(right)
        )

    records = [rec for rec in records if rec.get("kind") == "valve"]
    expected_steps = {}
    positions = defaultdict(dict)
    identities = {}
    result = []
    offset = 0
    while offset < len(records):
        first = records[offset]
        data = first.get("data", {})
        required = ("replay_epoch", "replay_step", "valve_index", "valve_count",
                    "zone_entity", "entity", "previous_command", "zone_mode", "master_off", "raw_demand", "scale")
        if any(key not in data for key in required):
            raise ValueError("Valve call lacks replay boundaries or inputs")
        epoch, step, count = data["replay_epoch"], data["replay_step"], data["valve_count"]
        if not isinstance(epoch, str) or not epoch or not isinstance(step, int) or step < 0:
            raise ValueError("Valve call has invalid replay boundary")
        if not isinstance(count, int) or count <= 0 or data["valve_index"] != 0:
            raise ValueError("Valve call has incomplete or divergent valve ordering")
        if epoch in expected_steps and step != expected_steps[epoch]:
            raise ValueError("Valve call has a missing, duplicate or reordered step")
        identity = (first.get("zone"), data["zone_entity"])
        if epoch in identities and identities[epoch] != identity:
            raise ValueError("Valve epoch has a divergent zone identity")
        identities[epoch] = identity
        expected_steps[epoch] = step + 1
        seen = set()
        for index in range(count):
            if offset >= len(records):
                raise ValueError("Incomplete valve call")
            rec = records[offset]
            row = rec.get("data", {})
            if any(key not in row for key in required):
                raise ValueError("Valve call lacks replay boundaries or inputs")
            if (row["replay_epoch"], row["replay_step"], row["valve_index"], row["valve_count"]) != (
                epoch, step, index, count
            ) or rec.get("zone") != identity[0] or row["zone_entity"] != identity[1]:
                raise ValueError("Valve call has incomplete, overlapping or divergent valve ordering")
            if any(not equal(row[key], data[key]) for key in ("zone_mode", "master_off", "raw_demand", "scale")):
                raise ValueError("Valve call has divergent shared inputs")
            entity = row["entity"]
            if entity in seen:
                raise ValueError("Valve call has duplicate valve entities")
            seen.add(entity)
            prior = row["previous_command"]
            if entity in positions[epoch] and not equal(prior, positions[epoch][entity]):
                raise ValueError("Valve call has divergent previous command")
            replayed = (await replay_proportional_valves_from_records([rec]))[0]
            positions[epoch][entity] = replayed["last_command"]
            result.append(replayed)
            offset += 1
            if replayed["cancelled"]:
                break
    return result


def replay_relay_switch_paths_from_records(records: Iterable[dict]) -> list[tuple[dict, bool | None]]:
    """Re-run reported switch-path checks from their captured ordered inputs."""
    from collections import deque
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat.boiler import relay_backend as module

    results = []
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "relay_switch_path":
            continue
        zones = data.get("zones")
        if not isinstance(zones, list) or "result" not in data:
            raise ValueError(f"Switch path record {rec.get('seq')} lacks inputs")
        modes, demands, states = deque(), deque(), deque()
        configured = []
        for sample in zones:
            if any(key not in sample for key in ("name", "id", "mode")):
                raise ValueError(f"Switch path record {rec.get('seq')} lacks zone inputs")
            modes.append((sample["id"], sample["mode"]))
            if sample["mode"] == "primary":
                if "demand" not in sample:
                    raise ValueError(f"Switch path record {rec.get('seq')} lacks demand")
                demands.append((sample["id"], sample["demand"]))
            if "trvs" in sample:
                if not isinstance(sample["trvs"], list) or not isinstance(sample.get("switches"), list):
                    raise ValueError(f"Switch path record {rec.get('seq')} has unsupported zone inputs")
                if not isinstance(sample.get("states"), list):
                    raise ValueError(f"Switch path record {rec.get('seq')} lacks switch readings")
                states.extend((item["entity"], item["state"]) for item in sample["states"])
            configured.append({"name": sample["name"], "climate_entities": sample.get("trvs") or [],
                               "switch_entities": sample.get("switches") or []})

        def pop(queue, entity, label):
            if not queue or queue[0][0] != entity:
                raise ValueError(f"Switch path record {rec.get('seq')} has divergent {label} reads")
            return queue.popleft()[1]

        hass = MagicMock()
        def get_state(entity):
            state = pop(states, entity, "state")
            return None if state is None else SimpleNamespace(state=state)
        hass.states.get = get_state
        coordinator = SimpleNamespace(
            zones=configured, get_zone_mode=lambda cid: pop(modes, cid, "mode"),
            get_zone_demand=lambda cid: pop(demands, cid, "demand"),
        )
        backend = module.RelayBoilerBackend(hass, coordinator, "switch.boiler", 0, 0, 0)
        emitted = []
        class Capture:
            enabled = True
            def record(self, kind, zone, **payload):
                emitted.append(payload)
        original = module.telemetry
        module.telemetry = SimpleNamespace(KIND_EVENT=original.KIND_EVENT, get_recorder=lambda: Capture())
        try:
            result = backend._switch_valve_path_open()
        finally:
            module.telemetry = original
        if modes or demands or states or len(emitted) != 1 or emitted[0] != data:
            raise ValueError(f"Switch path record {rec.get('seq')} diverged during replay")
        results.append((rec, result))
    return results


def replay_relay_delay_checks_from_records(records: Iterable[dict]) -> list[tuple[dict, bool]]:
    """Re-run recorded delayed-start rechecks; path values are HA-reported, not flow proof."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat.boiler import relay_backend as module

    rows = []
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "relay_delay_check":
            continue
        if "frost" not in data or "result" not in data:
            raise ValueError(f"Relay recheck {rec.get('seq')} lacks inputs")
        if not data["frost"]:
            if "master_on" not in data:
                raise ValueError(f"Relay recheck {rec.get('seq')} lacks master state")
            if data["master_on"] and "dhw_active" not in data:
                raise ValueError(f"Relay recheck {rec.get('seq')} lacks DHW state")
            if data["master_on"] and not data["dhw_active"]:
                if "switch_path" not in data or "trv_path" not in data:
                    raise ValueError(f"Relay recheck {rec.get('seq')} lacks path inputs")
                if (data["switch_path"] is not False or data["trv_path"]) and "demand" not in data:
                    raise ValueError(f"Relay recheck {rec.get('seq')} lacks demand")
        coordinator = SimpleNamespace(
            get_master_state=lambda: data["master_on"],
            is_dhw_active=data.get("dhw_active"),
        )
        backend = module.RelayBoilerBackend(MagicMock(), coordinator, "switch.boiler", 0, 0, 0)
        backend._last_frost_emergency = data["frost"]
        backend._last_demand = data.get("demand", 0)
        backend._trv_path_ready = data.get("trv_path")
        backend._switch_valve_path_open = lambda: data["switch_path"]
        original = module.telemetry
        module.telemetry = SimpleNamespace(
            KIND_EVENT=original.KIND_EVENT, get_recorder=lambda: SimpleNamespace(record=lambda *_args, **_kwargs: None),
        )
        try:
            result = backend._conditions_still_valid()
        finally:
            module.telemetry = original
        if result != data["result"]:
            raise ValueError(f"Relay recheck {rec.get('seq')} diverged during replay")
        rows.append((rec, result))
    return rows


async def replay_relay_delay_cycles_from_records(
    records: Iterable[dict], *, _allow_wait_cancel_reason_change: bool = False,
) -> list[dict]:
    """Replay complete recorded relay delay tasks with captured recheck results and mocked actions.

    The private option delegates validation of a changed cancellation reason to the caller.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat.boiler import relay_backend as module

    cycles = {}
    order = []
    next_steps = {}
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") not in ("relay_delay", "relay_delay_check"):
            continue
        if "epoch" not in data or "step" not in data:
            if data.get("event") == "relay_delay_check":
                continue
            raise ValueError("Relay delay record lacks task identity")
        key = (data["epoch"], data["step"])
        if data.get("phase") == "scheduled":
            epoch, step = key
            if (not isinstance(epoch, str) or not epoch or not isinstance(step, int)
                    or isinstance(step, bool) or step < 0):
                raise ValueError("Relay delay task has an invalid replay boundary")
            if epoch in next_steps and step != next_steps[epoch]:
                raise ValueError("Relay delay task has a missing or duplicate step")
            next_steps[epoch] = step + 1
            if key in cycles:
                raise ValueError("Relay delay task has duplicate start")
            cycles[key] = []
            order.append(key)
        if key not in cycles or (cycles[key] and cycles[key][-1]["data"].get("phase") == "finished"):
            raise ValueError("Relay delay task lacks a start or has events after completion")
        cycles[key].append(rec)
    results = []
    for epoch, step in order:
        rows = cycles[(epoch, step)]
        if rows[-1]["data"].get("phase") != "finished":
            raise ValueError("Incomplete relay delay task")
        scheduled = rows[0]["data"]
        if any(key not in scheduled for key in ("seconds", "turn_on", "reason_before")):
            raise ValueError("Relay delay task lacks scheduling inputs")
        checks = [row for row in rows if row["data"].get("event") == "relay_delay_check"]
        if len(checks) > 1:
            raise ValueError("Relay delay task has multiple rechecks")
        replay_relay_delay_checks_from_records(checks)
        check = checks[0]["data"] if checks else None
        phases = [row["data"].get("phase") for row in rows if row["data"].get("event") == "relay_delay"]
        if "expired" not in phases and "cancelled" not in phases:
            raise ValueError("Relay delay task lacks expiry or cancellation")
        start = next((row["data"] for row in rows if row["data"].get("phase") == "on_request_started"), None)
        if start is not None and "demand" not in start:
            raise ValueError("Relay delay task lacks request demand")
        failure = next((row["data"] for row in rows if row["data"].get("phase") == "error"), None)
        if failure is not None and not isinstance(failure.get("error"), str):
            raise ValueError("Relay delay task lacks error context")
        if failure is not None and not any(phase in phases for phase in ("on_request_started", "off_request_started")):
            raise ValueError("Relay delay task has unsupported failure context")
        if "expired" in phases and scheduled["turn_on"] and check is None:
            raise ValueError("Relay delay task lacks its recheck")
        coordinator = SimpleNamespace(
            get_master_state=lambda: check["master_on"],
            is_dhw_active=check.get("dhw_active") if check else None,
        )
        hass = MagicMock()
        hass.async_create_task = asyncio.create_task
        backend = module.RelayBoilerBackend(hass, coordinator, "switch.boiler", 0, 0, 0)
        backend._delay_epoch = epoch
        backend._delay_step = step
        backend._status_reason = scheduled["reason_before"]
        backend._last_frost_emergency = check["frost"] if check else False
        backend._last_demand = start["demand"] if start is not None else (check.get("demand", 0) if check else 0)
        backend._trv_path_ready = check.get("trv_path") if check else False
        backend._switch_valve_path_open = lambda: check["switch_path"]
        actions = []
        emitted = []
        class Capture:
            enabled = False
            def record(self, kind, zone, **data):
                if data.get("event") != "relay_delay_command":
                    emitted.append({"kind": kind, "zone": zone, "data": data})
        async def sleep(_seconds):
            if "cancelled" in phases and "expired" not in phases:
                if _allow_wait_cancel_reason_change:
                    backend._status_reason = rows[-1]["data"]["reason"]
                raise asyncio.CancelledError
        async def action(name):
            actions.append(name)
            if "cancelled" in phases:
                raise asyncio.CancelledError
            if failure is not None:
                raise RuntimeError(failure["error"])
        backend._force_boiler_on = lambda: action("on")
        backend._force_boiler_off = lambda: action("off")
        original_asyncio, original_telemetry = module.asyncio, module.telemetry
        module.asyncio = SimpleNamespace(
            sleep=sleep, current_task=asyncio.current_task, CancelledError=asyncio.CancelledError,
        )
        module.telemetry = SimpleNamespace(KIND_EVENT=original_telemetry.KIND_EVENT, get_recorder=lambda: Capture())
        try:
            backend._schedule_boiler_check(scheduled["seconds"], scheduled["turn_on"])
            try:
                await backend._pending_boiler_task
            except RuntimeError as err:
                if failure is None or str(err) != failure["error"]:
                    raise
        finally:
            module.asyncio, module.telemetry = original_asyncio, original_telemetry
        if [row["data"] for row in emitted] != [row["data"] for row in rows]:
            raise ValueError("Relay delay task diverged during replay")
        results.append({"records": rows, "actions": actions, "reason": backend.status_reason})
    return results


async def replay_weather_compensation_from_records(records: Iterable[dict]) -> list[tuple[dict, float]]:
    """Re-run recorded feed-forward decisions with captured PID and thermal-model outputs."""
    import math
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat import zone_controller as module
    from custom_components.multizone_thermostat.const import KEY_WEATHER_CURVE

    def equal(left, right):
        if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
            return True
        if isinstance(left, dict) and isinstance(right, dict):
            return left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
        return left == right

    results = []
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "weather_compensation":
            continue
        required = ("pid_demand", "current_temp", "effective_target", "weather_sensor", "outdoor_temp",
                    "raw_curve", "raw_curve_captured", "manual_curve", "effective_curve",
                    "applied", "feed_forward", "demand")
        if "zone" not in rec or any(key not in data for key in required) or not data["raw_curve_captured"]:
            raise ValueError(f"Weather record {rec.get('seq')} lacks replay inputs")
        zone = rec["zone"]
        emitted = []
        class Capture:
            def record(self, kind, zone, **payload):
                if payload.get("event") == "weather_compensation":
                    emitted.append(payload)
        aggregator = SimpleNamespace(set_zone_demand=lambda *_: None)
        coordinator = SimpleNamespace(
            zones=[], demand_aggregator=aggregator, weather_sensor_id=data["weather_sensor"],
            zone_manager=SimpleNamespace(get_zone_mode=lambda _: "primary"),
            get_persistent_data=lambda key, default: data["raw_curve"] if key == KEY_WEATHER_CURVE else default,
            is_dhw_active=False,
        )
        controller = module.ZoneController(MagicMock(), coordinator)
        controller._pids[zone] = SimpleNamespace(
            calc=lambda *_: data["pid_demand"], last_calc=None, update_sensor_timestamp=lambda: None,
        )
        controller._thermal_models[zone] = SimpleNamespace(
            thermal_inertia=0.0, get_weather_multiplier=lambda **_: data["effective_curve"],
        )
        controller._autotuners[zone] = SimpleNamespace(state="completed", STATE_COMPLETED="completed")
        controller._outdoor_temp = lambda: data["outdoor_temp"]
        state = SimpleNamespace(
            state="heat",
            attributes={"current_temperature": data["current_temp"], "temperature": data["effective_target"]},
        )
        original = module.telemetry
        module.telemetry = SimpleNamespace(KIND_EVENT=original.KIND_EVENT, KIND_PID=original.KIND_PID,
                                           get_recorder=lambda: Capture())
        try:
            result = await controller.async_update_zone(zone, state, learn=False)
        finally:
            module.telemetry = original
        if len(emitted) != 1 or not equal(emitted[0], data):
            raise ValueError(f"Weather record {rec.get('seq')} diverged during replay")
        results.append((rec, result))
    return results


async def replay_hysteresis_from_records(records: Iterable[dict]) -> list[tuple[dict, float]]:
    """Re-run recorded learning-phase hysteresis using the captured previous demand."""
    import math
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat import zone_controller as module

    def equal(left, right):
        return left == right or (
            isinstance(left, float) and isinstance(right, float)
            and math.isnan(left) and math.isnan(right)
        )

    results = []
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "hysteresis_demand":
            continue
        required = ("input", "effective_target", "tolerance", "demand", "previous_demand", "autotune_state")
        if "zone" not in rec or any(key not in data for key in required):
            raise ValueError(f"Hysteresis record {rec.get('seq')} lacks replay inputs")
        emitted = []
        class Capture:
            def record(self, kind, zone, **payload):
                if payload.get("event") == "hysteresis_demand":
                    emitted.append(payload)
        coordinator = SimpleNamespace(
            zones=[], demand_aggregator=SimpleNamespace(
                get_zone_demand=lambda _: data["previous_demand"], set_zone_demand=lambda *_: None,
            ),
            zone_manager=SimpleNamespace(get_zone_mode=lambda _: "primary"), is_dhw_active=False,
        )
        controller = module.ZoneController(MagicMock(), coordinator)
        zone = rec["zone"]
        controller._pids[zone] = SimpleNamespace(update_sensor_timestamp=lambda: None)
        controller._thermal_models[zone] = SimpleNamespace(thermal_inertia=0.0)
        controller._autotuners[zone] = SimpleNamespace(
            state=data["autotune_state"], STATE_COMPLETED=object(),
        )
        state = SimpleNamespace(
            state="heat", attributes={"current_temperature": data["input"], "temperature": data["effective_target"]},
        )
        original = module.telemetry
        module.telemetry = SimpleNamespace(KIND_EVENT=original.KIND_EVENT, get_recorder=lambda: Capture())
        try:
            result = await controller.async_update_zone(zone, state, learn=False)
        finally:
            module.telemetry = original
        if len(emitted) != 1 or emitted[0].keys() != data.keys() or any(
            not equal(emitted[0][key], value) for key, value in data.items()
        ):
            raise ValueError(f"Hysteresis record {rec.get('seq')} diverged during replay")
        results.append((rec, result))
    return results


async def replay_zone_control_skips_from_records(records: Iterable[dict]) -> list[dict]:
    """Re-run recorded zone-control early exits with only their effective inputs."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat import zone_controller as module

    results = []
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "zone_control_skipped":
            continue
        reason = data.get("reason")
        if "zone" not in rec or reason not in (
            "missing_temperature", "invalid_temperature", "unregistered_zone",
            "critical_sensor_fault", "inactive_mode",
        ):
            raise ValueError(f"Zone skip record {rec.get('seq')} has unsupported inputs")
        if reason == "missing_temperature":
            if "current_present" not in data or "target_present" not in data:
                raise ValueError(f"Zone skip record {rec.get('seq')} lacks temperature presence")
            current, target = (19 if data["current_present"] else None), (20 if data["target_present"] else None)
        elif reason == "invalid_temperature":
            if not data.get("inputs_captured") or "current_temp" not in data or "target_temp" not in data:
                raise ValueError(f"Zone skip record {rec.get('seq')} lacks raw temperatures")
            current, target = data["current_temp"], data["target_temp"]
        elif reason in ("unregistered_zone", "critical_sensor_fault"):
            if "current_temp" not in data or "target_temp" not in data:
                raise ValueError(f"Zone skip record {rec.get('seq')} lacks temperatures")
            current, target = data["current_temp"], data["target_temp"]
        else:
            if "hvac_mode" not in data or "zone_mode" not in data:
                raise ValueError(f"Zone skip record {rec.get('seq')} lacks modes")
            current, target = 19, 20
        emitted, updates = [], []
        class Capture:
            def record(self, kind, zone, **payload):
                if payload.get("event") == "zone_control_skipped":
                    emitted.append(payload)
        coordinator = SimpleNamespace(
            zones=[], demand_aggregator=SimpleNamespace(set_zone_demand=lambda cid, val: updates.append((cid, val))),
            zone_manager=SimpleNamespace(get_zone_mode=lambda _: data.get("zone_mode", "primary")),
            plant_diagnostics=SimpleNamespace(has_critical_sensor_fault=lambda _: reason == "critical_sensor_fault"),
        )
        controller = module.ZoneController(MagicMock(), coordinator)
        zone = rec["zone"]
        if reason != "unregistered_zone":
            controller._pids[zone] = SimpleNamespace(update_sensor_timestamp=lambda: None)
            controller._autotuners[zone] = SimpleNamespace()
            controller._thermal_models[zone] = SimpleNamespace()
        state = SimpleNamespace(state=data.get("hvac_mode", "heat"),
                                attributes={"current_temperature": current, "temperature": target})
        original = module.telemetry
        module.telemetry = SimpleNamespace(KIND_EVENT=original.KIND_EVENT, get_recorder=lambda: Capture())
        try:
            result = await controller.async_update_zone(zone, state, learn=False)
        finally:
            module.telemetry = original
        if len(emitted) != 1 or emitted[0] != data:
            raise ValueError(f"Zone skip record {rec.get('seq')} diverged during replay")
        results.append({"record": rec, "result": result, "updates": updates})
    return results


async def replay_frost_scans_from_records(records: Iterable[dict]) -> list[dict]:
    """Replay non-overlapping frost scans, including recorded action failures and cancellations."""
    import asyncio
    from collections import deque
    import math
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat.const import (
        KEY_ANTI_FROST_ENABLED, KEY_FROST_PROTECTION_TEMP, make_zone_entity_id,
    )
    from custom_components.multizone_thermostat.safety.frost import FrostManager

    scans = []
    current = None
    for rec in records:
        data = rec.get("data", {})
        event = data.get("event")
        if rec.get("kind") != "event" or event not in ("frost_scan", "frost_check", "frost_action"):
            continue
        if event == "frost_scan" and data.get("phase") == "start":
            if current is not None:
                raise ValueError("Overlapping frost scans cannot be replayed")
            current = [rec]
        elif current is not None:
            current.append(rec)
            if event == "frost_scan" and data.get("phase") == "completed":
                scans.append(current)
                current = None
            elif event == "frost_action" and data.get("phase") in ("failed", "cancelled"):
                scans.append(current)
                current = None
        elif event == "frost_action" or (event == "frost_scan" and data.get("enabled") is True):
            raise ValueError("Frost scan lacks a start boundary")
        elif event == "frost_scan" and data.get("enabled") is False:
            scans.append([rec])
    if current is not None:
        raise ValueError("Incomplete frost scan")

    results = []
    for scan in scans:
        if len(scan) == 1:
            coordinator = SimpleNamespace(get_persistent_data=lambda *_: False)
            result = await FrostManager(MagicMock(), coordinator).async_handle_frost_emergencies()
            results.append({"records": scan, "result": result, "actions": []})
            continue
        names = scan[0]["data"].get("zones")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("Frost scan lacks zone inputs")
        terminal = scan[-1]["data"]
        interrupted = terminal.get("event") == "frost_action"
        if interrupted and (terminal.get("operation") not in ("set_demand", "set_mode", "set_target") or (
            terminal.get("phase") == "failed" and not isinstance(terminal.get("error"), str)
        )):
            raise ValueError("Frost scan has unsupported failure context")
        checks = replay_frost_checks_from_records(scan)
        if any(row["data"].get("result", False) != result for row, result in checks):
            raise ValueError("Frost scan has a divergent check result")
        check_results = deque((row["zone"], result) for row, result in checks)
        events = deque(scan[1:] if interrupted else scan[1:-1])
        actions = []

        def same_number(first, second):
            return first == second or (
                isinstance(first, float) and isinstance(second, float)
                and math.isnan(first) and math.isnan(second)
            )

        def take(event, zone, phase=None):
            if not events:
                raise ValueError(f"Frost scan lacks {event} {phase or ''}")
            row = events.popleft()
            data = row["data"]
            if data.get("event") != event or row["zone"] != zone or (
                phase is not None and data.get("phase") != phase
            ):
                raise ValueError(f"Frost scan has missing or divergent {event} {phase or ''}")
            return data

        def check(zone):
            take("frost_check", zone)
            if not check_results or check_results[0][0] != zone:
                raise ValueError("Frost scan has missing or divergent checks")
            return check_results.popleft()[1]

        def demand(zone, value):
            take("frost_action", zone, "triggered")
            actions.append(("demand", zone, value))
            inject(zone, "set_demand")

        def inject(zone, operation):
            if events and events[0]["data"].get("phase") in ("failed", "cancelled"):
                failure = take("frost_action", zone, events[0]["data"]["phase"])
                if failure.get("operation") != operation:
                    raise ValueError("Frost scan has divergent failure operation")
                if failure["phase"] == "cancelled":
                    raise asyncio.CancelledError
                raise RuntimeError(failure["error"])

        def get_state(zone):
            if not events or events[0]["data"].get("phase") not in ("mode_input", "target_input"):
                raise ValueError("Frost scan lacks action state inputs")
            phase = events[0]["data"]["phase"]
            data = take("frost_action", zone, phase)
            required = ("available", "mode") if phase == "mode_input" else ("available", "current_target")
            if any(key not in data for key in required):
                raise ValueError("Frost scan lacks action state inputs")
            if not data["available"]:
                return None
            return SimpleNamespace(state=data.get("mode"), attributes={"temperature": data.get("current_target")})

        def setting(key, default):
            if key == KEY_ANTI_FROST_ENABLED:
                return True
            if key == KEY_FROST_PROTECTION_TEMP:
                if not events or events[0]["data"].get("phase") != "target_input":
                    raise ValueError("Frost scan lacks action threshold")
                data = events[0]["data"]
                if "threshold" not in data or "frost_target" not in data:
                    raise ValueError("Frost scan lacks action threshold")
                if not same_number(data["threshold"] + 3.0, data["frost_target"]):
                    raise ValueError("Frost scan has divergent target")
                return data["threshold"]
            return default

        async def set_mode(zone, mode):
            actions.append(("mode", zone, mode))
            inject(zone, "set_mode")
            take("frost_action", zone, "heat_mode_requested")

        async def service(domain, service_name, data, blocking):
            actions.append(("service", domain, service_name, dict(data), blocking))
            inject(data["entity_id"], "set_target")
            recorded = take("frost_action", data["entity_id"], "target_requested")
            if not same_number(recorded.get("target"), data["temperature"]):
                raise ValueError("Frost scan has divergent target request")

        hass = MagicMock()
        hass.states.get = get_state
        hass.services.async_call = service
        coordinator = SimpleNamespace(
            zones=[{"name": name} for name in names], get_persistent_data=setting,
            set_zone_demand=demand, _async_set_hvac_mode=set_mode,
        )
        manager = FrostManager(hass, coordinator)
        manager.is_zone_in_frost_emergency = check
        result = None
        outcome = None
        try:
            result = await manager.async_handle_frost_emergencies()
        except asyncio.CancelledError:
            outcome = "cancelled"
        except RuntimeError as err:
            if not interrupted or terminal.get("phase") != "failed" or str(err) != terminal["error"]:
                raise
            outcome = "failed"
        if interrupted and outcome != terminal.get("phase"):
            raise ValueError("Frost scan failure was not reproduced")
        if not interrupted and outcome is not None:
            raise ValueError("Frost scan unexpectedly interrupted")
        if events or check_results or (not interrupted and terminal.get("result") != result):
            raise ValueError("Frost scan has unconsumed events or divergent result")
        expected_zones = [make_zone_entity_id(name) for name in names]
        checked_zones = [row["zone"] for row, _ in checks]
        if checked_zones != expected_zones[:len(checked_zones)] or (
            not interrupted and checked_zones != expected_zones
        ):
            raise ValueError("Frost scan checks do not match configured zones")
        results.append({"records": scan, "result": result, "actions": actions, "outcome": outcome})
    return results


def replay_opentherm_diagnostics_from_records(records: Iterable[dict]) -> list[tuple[dict, dict]]:
    """Re-evaluate captured OpenTherm direct attributes and companion sensor readings."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from custom_components.multizone_thermostat.plant_diagnostics import anomalies

    rows = []
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "sensor" or data.get("source") != "opentherm_diagnostics":
            continue
        observed = data.get("observed")
        if not observed or any(item.get("capture_error") for item in observed):
            raise ValueError(f"OpenTherm diagnostics record {rec.get('seq')} has incomplete inputs")
        primary = observed[0]
        if "attributes" not in primary:
            raise ValueError(f"OpenTherm diagnostics record {rec.get('seq')} lacks attributes")
        states = {}
        for item in observed:
            if item.get("state") is not None:
                states[item["entity"]] = SimpleNamespace(state=item["state"], attributes=item.get("attributes", {}))
        entries = [SimpleNamespace(entity_id=item["entity"], original_device_class=item.get("device_class"),
                                   device_class=None) for item in observed[1:]]
        hass = MagicMock()
        hass.states.get = states.get
        registry = SimpleNamespace(async_get=lambda _: SimpleNamespace(device_id="replay"))
        fake_registry = SimpleNamespace(async_get=lambda _: registry, async_entries_for_device=lambda *_: entries)
        original = anomalies.er
        anomalies.er = fake_registry
        try:
            result = anomalies.get_opentherm_diagnostics(SimpleNamespace(opentherm_entity=primary["entity"]), hass)
        finally:
            anomalies.er = original
        rows.append((rec, result))
    return rows


async def replay_anti_seize_from_records(records: Iterable[dict]) -> list[dict]:
    """Replay complete anti-seize cycles, including recorded operation failures and cancellation."""
    import asyncio
    from collections import deque
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat.const import (
        CONF_ANTI_SEIZE_BOILER, KEY_ANTI_SEIZE_DURATION, make_zone_entity_id,
    )
    from custom_components.multizone_thermostat.safety import anti_seize as module

    cycles = []
    current = None
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "anti_seize":
            continue
        phase = data.get("phase")
        if phase == "scheduled":
            continue
        if phase == "start":
            if current is not None:
                raise ValueError("Overlapping anti-seize cycles cannot be replayed")
            current = []
        if current is None:
            raise ValueError("Anti-seize records lack a start boundary")
        current.append(data)
        if phase in ("cancel_cleanup_error", "cleanup_error"):
            raise ValueError("Anti-seize cleanup failure lacks enough replay context")
        if phase in ("completed", "no_zones", "cancel_cleanup_completed", "cleanup_completed"):
            cycles.append(current)
            current = None
    if current is not None:
        raise ValueError("Incomplete anti-seize cycle")

    results = []
    for cycle in cycles:
        phases = {item["phase"]: item for item in cycle}
        selected = phases.get("zones_selected") or phases.get("no_zones")
        if selected is None or "zone_inputs" not in selected:
            raise ValueError("Anti-seize cycle lacks zone inputs")
        no_zones = "no_zones" in phases
        failure = phases.get("cancelled") or phases.get("error")
        if failure is not None and not isinstance(failure.get("stage"), dict):
            raise ValueError("Anti-seize failure lacks its operation context")
        if failure is not None:
            stage = failure["stage"]
            zoned_stages = {"zone_open_mode", "zone_open_demand", "zone_restore_demand", "zone_restore_mode"}
            other_stages = {"valve_wait", "boiler_on", "duration_wait", "boiler_off"}
            operation = stage.get("operation")
            if operation not in zoned_stages | other_stages or (
                operation in zoned_stages and not isinstance(stage.get("zone"), str)
            ) or (operation in other_stages and stage.get("zone") is not None):
                raise ValueError("Anti-seize failure has an unsupported operation stage")
            if "error" in phases and not isinstance(failure.get("error"), str):
                raise ValueError("Anti-seize failure lacks its error message")
            if ("cancelled" in phases) != ("cancel_cleanup_completed" in phases) or (
                "error" in phases
            ) != ("cleanup_completed" in phases):
                raise ValueError("Anti-seize failure has a divergent cleanup outcome")
        if not no_zones and failure is None and any(
            name not in phases for name in ("valve_wait", "boiler_decision", "duration")
        ):
            raise ValueError("Anti-seize cycle lacks timing or boiler inputs")
        zone_inputs = selected["zone_inputs"]
        zone_states = selected.get("saved_states", {})
        zone_demands = selected.get("saved_demands", {})
        hass = MagicMock()
        hass.states.get.side_effect = lambda cid: (
            SimpleNamespace(state=zone_states[cid]) if cid in zone_states else None
        )
        actions = []
        tasks = []
        mode_calls = {}
        demand_calls = {}
        failure_stage = failure["stage"] if failure is not None else None
        triggered = False
        input_errors = []
        off_statuses = deque(item for item in cycle if item["phase"] == "boiler_off_status")
        if any(item["phase"] == "boiler_off_switch" for item in cycle):
            raise ValueError("Anti-seize switch-only boiler replay is not supported")
        failure_after_status = failure is not None and any(
            item["phase"] == "boiler_off_status" for item in cycle[:cycle.index(failure)]
        )

        def inject(operation, zone=None):
            nonlocal triggered
            if triggered or failure_stage is None:
                return
            if failure_stage.get("operation") == operation and failure_stage.get("zone") == zone:
                triggered = True
                if "cancelled" in phases:
                    raise asyncio.CancelledError
                raise RuntimeError(failure["error"])

        async def set_mode(cid, mode):
            actions.append(("mode", cid, mode))
            calls = mode_calls.get(cid, 0)
            mode_calls[cid] = calls + 1
            inject("zone_open_mode" if calls == 0 else "zone_restore_mode", cid)

        def set_demand(cid, demand):
            actions.append(("demand", cid, demand))
            calls = demand_calls.get(cid, 0)
            demand_calls[cid] = calls + 1
            inject("zone_open_demand" if calls == 0 else "zone_restore_demand", cid)

        async def backend_update(demand, peak, frost):
            actions.append(("boiler", demand, peak, frost))
            if demand > 0:
                inject("boiler_on")
                return
            if not failure_after_status:
                inject("boiler_off")
            if not off_statuses:
                input_errors.append("missing boiler-off status")
                raise ValueError("Anti-seize cycle lacks boiler-off status")
            status = off_statuses.popleft()
            if "status_reason" not in status or "emergency_supported" not in status:
                input_errors.append("incomplete boiler-off status")
                raise ValueError("Anti-seize cycle lacks boiler-off status inputs")
            backend.status_reason = status["status_reason"]
            if status["emergency_supported"]:
                backend.async_emergency_off = emergency_off
            elif hasattr(backend, "async_emergency_off"):
                del backend.async_emergency_off

        async def emergency_off():
            actions.append(("emergency_off",))
            if failure_after_status:
                inject("boiler_off")

        sleeps = []
        valve_delay = phases.get("valve_wait", {}).get("seconds")

        async def sleep(seconds):
            sleeps.append(seconds)
            inject("valve_wait" if len(sleeps) == 1 and valve_delay and seconds == valve_delay
                   else "duration_wait")

        def get_setting(key, default):
            if key == CONF_ANTI_SEIZE_BOILER:
                if "boiler_decision" not in phases:
                    input_errors.append("missing boiler decision")
                    raise ValueError("Anti-seize cycle lacks the boiler decision")
                return phases["boiler_decision"]["enabled"]
            if key == KEY_ANTI_SEIZE_DURATION:
                if "duration" not in phases or "minutes" not in phases["duration"]:
                    input_errors.append("missing duration")
                    raise ValueError("Anti-seize cycle lacks the duration input")
                return phases["duration"]["minutes"]
            return default

        def create_task(coro):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        hass.async_create_task = create_task
        backend = SimpleNamespace(async_update=backend_update, status_reason="")
        coord = SimpleNamespace(
            zones=[{"name": zone["name"], "anti_seize_zone_enable": zone["enabled"]} for zone in zone_inputs],
            _valve_delay=valve_delay if valve_delay is not None else 0,
            get_zone_demand=lambda cid: zone_demands[cid], set_zone_demand=set_demand,
            _async_set_hvac_mode=set_mode, reset_last_active_time=lambda: None,
            get_persistent_data=get_setting,
            boiler_backend=backend,
        )
        expected_available = {make_zone_entity_id(zone["name"]) for zone in zone_inputs
                              if zone["enabled"] and zone["available"]}
        if expected_available != set(zone_states):
            raise ValueError("Anti-seize saved states do not match available zones")
        if expected_available != set(zone_demands):
            raise ValueError("Anti-seize saved demands do not match available zones")
        original = module.asyncio
        module.asyncio = SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)
        try:
            try:
                await module.AntiSeizeManager(hass, coord).async_execute()
            except asyncio.CancelledError:
                if "cancelled" not in phases:
                    raise
            if tasks:
                await asyncio.gather(*tasks)
        finally:
            module.asyncio = original
        if input_errors:
            raise ValueError(f"Anti-seize cycle has incomplete inputs: {input_errors}")
        if off_statuses:
            raise ValueError("Anti-seize cycle has unconsumed boiler-off status inputs")
        if failure is not None and not triggered:
            raise ValueError("Anti-seize failure stage was not reached during replay")
        results.append({"records": cycle, "actions": actions, "sleeps": sleeps})
    return results


async def replay_opentherm_targets_from_records(records: Iterable[dict]) -> list[tuple[dict, float]]:
    """Replay the OpenTherm temperature calculation, not stateful actuator commands."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.multizone_thermostat.boiler.opentherm_backend import OpenThermBoilerBackend

    rows = []
    for rec in records:
        if rec.get("kind") != "opentherm":
            continue
        data = rec["data"]
        if data.get("action") != "update":
            continue
        required = ("computed_target_temp", "weather_cooling_rates", "weather_curve", "outdoor_temp",
                    "min_temp", "max_temp", "demand", "peak_zone", "frost_emergency")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"OpenTherm record {rec.get('seq')} missing replay fields: {missing}")
        hass = MagicMock()
        climate = SimpleNamespace(state="heat", attributes={"temperature": None})
        outdoor = SimpleNamespace(state=str(data["outdoor_temp"]) if data["outdoor_temp"] is not None else "unknown")
        hass.states.get.side_effect = lambda entity: outdoor if entity == "sensor.replay_outdoor" else climate
        hass.services.async_call = AsyncMock()
        coordinator = SimpleNamespace(
            plant_diagnostics=None, weather_sensor_id="sensor.replay_outdoor",
            get_persistent_data=lambda *_: data["weather_curve"] if data["weather_curve"] is not None else 0,
            _thermal_models={str(i): SimpleNamespace(cooling_rate=rate)
                             for i, rate in enumerate(data["weather_cooling_rates"])},
        )
        backend = OpenThermBoilerBackend(hass, coordinator, "climate.replay_boiler",
                                        data["min_temp"], data["max_temp"])
        await backend.async_update(data["demand"], data["peak_zone"], data["frost_emergency"])
        calls = hass.services.async_call.await_args_list
        target_calls = [call for call in calls if call.args[:2] == ("climate", "set_temperature")]
        if len(target_calls) != 1:
            raise RuntimeError(f"OpenTherm replay did not issue a target for record {rec.get('seq')}")
        rows.append((rec, target_calls[0].args[2]["temperature"]))
    return rows


async def replay_opentherm_commands_from_records(records: Iterable[dict]) -> list[tuple[dict, dict]]:
    """Replay OpenTherm updates against recorded state reads, clocks and service outcomes."""
    import asyncio
    from collections import deque
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat.boiler import opentherm_backend as module

    results = []
    for rec in records:
        if rec.get("kind") != "opentherm":
            continue
        data = rec["data"]
        required = ("trace", "entity", "demand", "peak_zone", "frost_emergency", "min_temp",
                    "max_temp", "ch_switch", "pending_before", "reason_before")
        missing = [key for key in required if key not in data]
        if missing or not isinstance(data.get("trace"), list):
            raise ValueError(f"OpenTherm record {rec.get('seq')} missing replay fields: {missing}")
        tape = deque(data["trace"])
        commands = []
        divergence = []

        def take(kind):
            if not tape or tape[0].get("type") != kind:
                divergence.append(f"missing or divergent {kind} event")
                raise ValueError(f"OpenTherm record {rec.get('seq')} has incomplete or divergent {kind} trace")
            return tape.popleft()

        def read_state(entity):
            item = take("state")
            if item["entity"] != entity:
                divergence.append(f"unexpected state read {entity}")
                raise ValueError(f"OpenTherm record {rec.get('seq')} requested unexpected state {entity}")
            if not item["exists"]:
                return None
            return SimpleNamespace(state=item["state"], domain=item["domain"], attributes=item["attributes"])

        async def service(domain, action, payload, **kwargs):
            item = take("service")
            actual = {"domain": domain, "service": action, "data": payload, "kwargs": kwargs}
            if any(item.get(key) != value for key, value in actual.items()):
                divergence.append(actual)
                raise ValueError(f"OpenTherm record {rec.get('seq')} issued a different command: {actual}")
            commands.append(actual)
            if item["outcome"] == "error":
                raise RuntimeError(item["error"])
            if item["outcome"] == "cancelled":
                raise asyncio.CancelledError
            if item["outcome"] != "completed":
                divergence.append("incomplete service outcome")
                raise ValueError(f"OpenTherm record {rec.get('seq')} has incomplete service outcome")

        hass = MagicMock()
        hass.states.get = read_state
        hass.services.async_call = service
        flame = data.get("flame_active")
        diag = None if flame is None else SimpleNamespace(
            get_opentherm_diagnostics=lambda: {"flame_active": flame},
            _current_boiler_start=None, record_boiler_state=lambda _: None,
        )
        coordinator = SimpleNamespace(
            plant_diagnostics=diag, weather_sensor_id=data.get("weather_sensor_id"),
            get_persistent_data=lambda *_: data.get("weather_curve") or 0,
            _thermal_models={str(i): SimpleNamespace(cooling_rate=rate)
                             for i, rate in enumerate(data.get("weather_cooling_rates", []))},
            record_diagnostic_event=lambda *_: None,
        )
        backend = module.OpenThermBoilerBackend(hass, coordinator, data["entity"],
                                               data["min_temp"], data["max_temp"], data["ch_switch"])
        backend._pending_command = tuple(data["pending_before"]) if data["pending_before"] is not None else None
        backend._status_reason = data["reason_before"]
        old_time = module.time
        module.time = SimpleNamespace(monotonic=lambda: take("clock")["value"])
        cancelled = False
        try:
            try:
                await backend.async_update(data["demand"], data["peak_zone"], data["frost_emergency"])
            except asyncio.CancelledError:
                cancelled = True
        finally:
            module.time = old_time
        if divergence:
            raise ValueError(f"OpenTherm record {rec.get('seq')} issued a different command: {divergence}")
        if tape:
            raise ValueError(f"OpenTherm record {rec.get('seq')} has unconsumed replay events")
        results.append((rec, {
            "commands": commands, "reason": backend.status_reason,
            "pending_command": backend._pending_command, "cancelled": cancelled,
        }))
    return results


async def replay_plant_decisions_from_records(records: Iterable[dict]) -> list[tuple[dict, str, tuple]]:
    """Replay individual orchestrator decisions, not asynchronous hardware or override lifecycles."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.multizone_thermostat.boiler.plant_orchestrator import PlantOrchestrator
    from custom_components.multizone_thermostat.demand_aggregator import DemandAggregator

    aggregation = None
    rows = []
    for rec in records:
        if rec.get("kind") == "aggregation":
            aggregation = rec
            continue
        if rec.get("kind") != "plant":
            continue
        data = rec["data"]
        action = data["action"]
        backend = SimpleNamespace(async_update=AsyncMock(), async_emergency_off=AsyncMock())
        coordinator = SimpleNamespace(
            get_master_state=lambda: data.get("master_on", action != "master_off"),
            is_dhw_active=data.get("dhw_active", False),
            zones=[], _climate_entities={}, _climate_callbacks={},
            frost_manager=SimpleNamespace(async_handle_frost_emergencies=AsyncMock(
                return_value=data.get("frost_emergency", False))),
        )
        if action == "aggregate":
            if (aggregation is None or data.get("raw_demands") is None
                    or rec["seq"] != aggregation["seq"] + 1):
                raise ValueError(f"Plant record {rec.get('seq')} lacks aggregation or raw demand inputs")
            aggregation = aggregation["data"]
            if (aggregation["dhw_active"], aggregation["frost_emergency"]) != (
                data["dhw_active"], data["frost_emergency"]
            ):
                raise ValueError(f"Plant record {rec.get('seq')} has mismatched aggregation inputs")
            agg = DemandAggregator(strategy=aggregation["strategy"])
            for zone in aggregation["zones"]:
                agg.register_zone(zone["name"], climate_id=zone["id"],
                                  weight=zone["configured_weight"], mode=zone["mode"])
            for cid, demand in data["raw_demands"].items():
                agg.set_zone_demand(cid, demand)
            suppressed = data["suppressed"]
            for cid, reason in suppressed.items():
                if reason not in ("missing_temperature", "critical_sensor_fault"):
                    raise ValueError(f"Unsupported suppression reason: {reason}")
                coordinator._climate_entities[cid] = SimpleNamespace(
                    current_temperature=None if reason == "missing_temperature" else 20.0)
            coordinator.plant_diagnostics = SimpleNamespace(
                has_critical_sensor_fault=lambda cid: suppressed.get(cid) == "critical_sensor_fault")
            internal_faults = {zone["id"] for zone in aggregation["zones"] if zone["fault_suppressed"]}
            agg.coordinator = SimpleNamespace(plant_diagnostics=SimpleNamespace(
                has_critical_sensor_fault=lambda cid: cid in internal_faults))
            weights = _recorded_weight_getter(agg, aggregation, rec.get("seq"))
            aggregation = None
        else:
            agg = DemandAggregator()
        plant = PlantOrchestrator(MagicMock(), agg, backend, coordinator)
        if action == "anti_seize":
            coordinator.anti_seize_manager = SimpleNamespace(is_running=True)
            await plant.async_tick(None)
        elif action in ("aggregate", "master_off"):
            await plant._async_update_boiler()
        else:
            raise ValueError(f"Unsupported plant action: {action}")
        if action == "aggregate" and weights:
            raise ValueError(f"Plant record {rec.get('seq')} has unconsumed weight calls")
        if backend.async_update.await_count:
            rows.append((rec, "update", backend.async_update.await_args.args))
        elif backend.async_emergency_off.await_count:
            rows.append((rec, "emergency_off", ()))
        else:
            rows.append((rec, "none", ()))
    return rows


async def replay_linked_plant_pwm_from_records(
    records: Iterable[dict], *, allow_emergency_off: bool = False, allow_failed: bool = False,
) -> list[dict]:
    """Link plant decisions to boiler PWM calls and optionally completed emergency-off events."""
    import math

    def equal(left, right):
        return left == right or (
            isinstance(left, float) and isinstance(right, float)
            and math.isnan(left) and math.isnan(right)
        )

    records = list(records)
    def emergency_event(rec):
        return rec.get("kind") == "event" and rec.get("data", {}).get("event") == "relay_emergency_off"

    def supported_outcome(data, completed_key, cancelled_key, error_key):
        completed, cancelled, error = data.get(completed_key), data.get(cancelled_key), data.get(error_key)
        if completed is True:
            return cancelled is False and error is None
        return allow_failed and completed is False and (
            (cancelled is True and error is None) or (cancelled is False and isinstance(error, str))
        )

    sequences = [rec.get("seq") for rec in records
                 if rec.get("kind") in ("aggregation", "plant", "boiler") or emergency_event(rec)]
    if any(not isinstance(seq, int) or isinstance(seq, bool) for seq in sequences) or (
        len(sequences) != len(set(sequences))
    ):
        raise ValueError("Plant recording has missing or duplicate sequence numbers")
    if any(left >= right for left, right in zip(sequences, sequences[1:])):
        raise ValueError("Plant recording has reordered sequence numbers")
    decisions = await replay_plant_decisions_from_records(records)
    calculations = replay_stateful_pwm_from_records(records, "boiler", "boiler")
    decision_by_seq = {record["seq"]: (action, args) for record, action, args in decisions}
    pwm_by_seq = {record["seq"]: state for record, state in calculations}
    pending = None
    results = []
    for rec in records:
        if rec.get("kind") == "plant":
            action, args = decision_by_seq[rec["seq"]]
            if pending is not None:
                raise ValueError("Plant recording has overlapping or incomplete boiler updates")
            if action == "emergency_off" and allow_emergency_off:
                pending = (rec, action, args)
                continue
            if action != "update":
                raise ValueError("Plant recording contains an unsupported non-update lifecycle")
            reported = rec["data"]
            if any(key not in reported for key in ("demand", "peak_zone", "frost_emergency")) or (
                len(args) != 3 or not all(equal(a, b) for a, b in zip(
                    args, (reported["demand"], reported["peak_zone"], reported["frost_emergency"])
                ))
            ):
                raise ValueError("Plant decision diverges from its recorded output")
            pending = (rec, action, args)
        elif rec.get("kind") == "boiler":
            if pending is None:
                raise ValueError("Boiler record lacks a preceding plant decision")
            if pending[1] != "update":
                raise ValueError("Boiler record does not match the pending plant action")
            data = rec["data"]
            if rec["seq"] not in pwm_by_seq:
                raise ValueError("Boiler record lacks a replayable PWM calculation")
            if not supported_outcome(data, "update_completed", "update_cancelled", "update_error"):
                raise ValueError("Boiler update did not complete or lacks its outcome")
            actual = (data.get("demand"), data.get("peak_zone"), data.get("frost_emergency"))
            if len(pending[2]) != 3 or not all(equal(a, b) for a, b in zip(actual, pending[2])):
                raise ValueError("Plant decision and boiler inputs diverge")
            results.append({"plant": pending[0], "boiler": rec, "pwm_state": pwm_by_seq[rec["seq"]]})
            pending = None
        elif emergency_event(rec):
            if not allow_emergency_off:
                raise ValueError("Plant recording contains an unsupported non-update lifecycle")
            if pending is None or pending[1] != "emergency_off":
                raise ValueError("Emergency-off event lacks a matching plant decision")
            if not supported_outcome(rec["data"], "completed", "cancelled", "error"):
                raise ValueError("Emergency-off operation did not complete or lacks its outcome")
            results.append({"plant": pending[0], "emergency": rec, "pwm_state": None})
            pending = None
    if pending is not None:
        raise ValueError("Plant recording ends with an incomplete boiler update")
    return results


class _RelayReplayTape:
    def __init__(self, record, trace):
        self.record = record
        self.events = deque(trace)
        self.commands = []

    def take(self, kind):
        if not self.events or self.events[0].get("type") != kind:
            raise ValueError(f"Relay record {self.record.get('seq')} has incomplete or divergent {kind} trace")
        return self.events.popleft()

    def read_state(self, entity):
        item = self.take("state")
        if item.get("entity") != entity:
            raise ValueError(f"Relay record {self.record.get('seq')} requested unexpected state {entity}")
        if not item["exists"]:
            return None
        return SimpleNamespace(state=item["state"], attributes=item["attributes"])

    def zone_value(self, kind, zone):
        item = self.take(kind)
        if item.get("zone") != zone:
            raise ValueError(f"Relay record {self.record.get('seq')} requested unexpected zone {zone}")
        return item["value"]

    async def service(self, domain, action, payload, **kwargs):
        item = self.take("service")
        if (domain != "switch" or action != item.get("service") or payload != item.get("data")
                or kwargs.get("blocking") != item.get("blocking")):
            raise ValueError(f"Relay record {self.record.get('seq')} issued a different or incomplete command")
        self.commands.append((domain, action, payload.copy()))
        if item.get("outcome") == "cancelled":
            raise asyncio.CancelledError
        if item.get("outcome") == "error" and isinstance(item.get("error"), str):
            raise RuntimeError(item["error"])
        if item.get("outcome") != "completed":
            raise ValueError(f"Relay record {self.record.get('seq')} has an incomplete service outcome")


async def replay_relay_delayed_commands_from_records(
    records: Iterable[dict], *, _allow_wait_cancel_reason_change: bool = False,
) -> list[dict]:
    """Replay complete delay cycles and their actual relay command implementations."""
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat.boiler import relay_backend as module

    records = list(records)
    sequences = [rec.get("seq") for rec in records if rec.get("kind") == "event"
                 and rec.get("data", {}).get("event") in (
                     "relay_delay", "relay_delay_check", "relay_delay_command",
                 )]
    if (any(not isinstance(seq, int) or isinstance(seq, bool) for seq in sequences)
            or any(left >= right for left, right in zip(sequences, sequences[1:]))):
        raise ValueError("Relay delay recording has missing, duplicate or reordered sequence numbers")
    cycles = await replay_relay_delay_cycles_from_records(
        records, _allow_wait_cancel_reason_change=_allow_wait_cancel_reason_change,
    )
    commands = {}
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "relay_delay_command":
            continue
        key = (data.get("epoch"), data.get("step"))
        if key in commands or not isinstance(key[0], str) or not isinstance(key[1], int):
            raise ValueError("Relay delay command has an invalid or duplicate task identity")
        commands[key] = rec
    results = []
    for cycle in cycles:
        rows = cycle["records"]
        scheduled = rows[0]["data"]
        key = (scheduled["epoch"], scheduled["step"])
        record = commands.pop(key, None)
        if not cycle["actions"]:
            if record is not None:
                raise ValueError("Relay delay task has an unexpected command record")
            results.append({"cycle": cycle, "command": None, "commands": []})
            continue
        if len(cycle["actions"]) != 1 or record is None:
            raise ValueError("Relay delay task lacks its command record")
        data = record["data"]
        before, after, trace = data.get("before"), data.get("after"), data.get("trace")
        fields = ("switch", "last_change", "pending_command", "reason")
        if (not isinstance(before, dict) or any(key not in before for key in fields)
                or not isinstance(after, dict) or not isinstance(trace, list)
                or not isinstance(data.get("turn_on"), bool)):
            raise ValueError(f"Relay delay command {record.get('seq')} lacks replay inputs")
        action = "on" if data["turn_on"] else "off"
        starts = [row for row in rows if row["data"].get("phase") == f"{action}_request_started"]
        if (cycle["actions"] != [action] or len(starts) != 1
                or not starts[0]["seq"] < record["seq"] < rows[-1]["seq"]):
            raise ValueError("Relay delay command has a divergent lifecycle")
        tape = _RelayReplayTape(record, trace)
        hass = MagicMock()
        hass.states.get = tape.read_state
        hass.services.async_call = tape.service
        coord = SimpleNamespace(record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None)
        backend = module.RelayBoilerBackend(hass, coord, before["switch"], 0, 0, 0)
        backend._last_boiler_change = before["last_change"]
        backend._pending_command = tuple(before["pending_command"]) if before["pending_command"] is not None else None
        backend._status_reason = before["reason"]
        old_time = module.time
        module.time = SimpleNamespace(monotonic=lambda: tape.take("clock")["value"])
        outcome, error = "completed", None
        try:
            try:
                if data["turn_on"]:
                    await backend._force_boiler_on()
                else:
                    await backend._force_boiler_off()
            except asyncio.CancelledError:
                outcome = "cancelled"
            except RuntimeError as err:
                outcome, error = "error", str(err)
        finally:
            module.time = old_time
        recorded_after = after.copy()
        if recorded_after.get("pending_command") is not None:
            recorded_after["pending_command"] = tuple(recorded_after["pending_command"])
        actual_after = {"last_change": backend._last_boiler_change,
                        "pending_command": backend._pending_command, "reason": backend.status_reason}
        phases = [row["data"].get("phase") for row in rows]
        lifecycle_outcome = ("completed" if f"{action}_request_completed" in phases else
                             "cancelled" if "cancelled" in phases else "error" if "error" in phases else None)
        expected = ("completed" if data.get("completed") is True and data.get("cancelled") is False
                    and data.get("error") is None else
                    "cancelled" if data.get("completed") is False and data.get("cancelled") is True
                    and data.get("error") is None else
                    "error" if data.get("completed") is False and data.get("cancelled") is False
                    and isinstance(data.get("error"), str) else None)
        if (tape.events or actual_after != recorded_after or outcome != expected
                or outcome != lifecycle_outcome or error != data.get("error")):
            raise ValueError(f"Relay delay command {record.get('seq')} diverged during replay")
        results.append({"cycle": cycle, "command": record, "commands": tape.commands,
                        "outcome": outcome, "error": error})
    if commands:
        raise ValueError("Relay delay command lacks a matching task")
    return results


async def replay_linked_relay_plant_from_records(records: Iterable[dict]) -> list[dict]:
    """Replay sequential plant decisions through one real relay backend.

    The first recorded backend state seeds replay. Delayed tasks, overlapping updates and
    lifecycle actions other than updates and master-off operations are unsupported.
    """
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat import pwm_engine
    from custom_components.multizone_thermostat.boiler import relay_backend as module

    linked = await replay_linked_plant_pwm_from_records(records, allow_emergency_off=True, allow_failed=True)
    if not linked:
        return []
    backend = None
    pwm_seeded = False
    diagnostic_seeded = False
    results = []
    for row in linked:
        emergency = "emergency" in row
        record = row["emergency"] if emergency else row["boiler"]
        data = record["data"]
        before = data.get("before" if emergency else "replay_before")
        after = data.get("after" if emergency else "replay_after")
        trace = data.get("trace" if emergency else "replay_trace")
        required = ("switch", "last_change", "pending_command", "last_known_on", "trv_wait_start",
                    "delay_pending")
        required += (("reason",) if emergency else
                     ("min_cycle_on", "min_cycle_off", "valve_delay", "dhw_active", "diagnostic_on", "zones"))
        if not isinstance(before, dict) or any(key not in before for key in required) or not isinstance(trace, list):
            raise ValueError(f"Relay record {record.get('seq')} lacks replay inputs")
        if not isinstance(after, dict) or (not emergency and (
                not isinstance(before["dhw_active"], bool) or not isinstance(before["diagnostic_on"], bool)
                or not isinstance(before["zones"], list))):
            raise ValueError(f"Relay record {record.get('seq')} has incomplete replay inputs")
        if before["delay_pending"] or (not emergency and before["valve_delay"] != 0):
            raise ValueError("Relay replay does not support delayed tasks")
        tape = _RelayReplayTape(record, trace)

        if backend is None:
            hass = MagicMock()
            diag = SimpleNamespace(_current_boiler_start=None)
            diag.record_boiler_state = lambda on: setattr(diag, "_current_boiler_start", 1 if on else None)
            coord = SimpleNamespace(
                zones=[], is_dhw_active=False, plant_diagnostics=diag,
                record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
            )
            backend = module.RelayBoilerBackend(hass, coord, before["switch"],
                                                before.get("min_cycle_on", 0), before.get("min_cycle_off", 0), 0)
            backend._last_boiler_change = before["last_change"]
            backend._pending_command = tuple(before["pending_command"]) if before["pending_command"] is not None else None
            backend._last_known_boiler_on = before["last_known_on"]
            backend._trv_wait_start = before["trv_wait_start"]
            backend._status_reason = before["reason"] if emergency else data["reason_before"]
        if not emergency and not pwm_seeded:
            backend._boiler_pwm.cycle_start_time = data["cycle_start_before"]
            backend._boiler_pwm._prev_demand = data["prev_demand_before"]
            pwm_seeded = True
        if not emergency and not diagnostic_seeded:
            backend.coordinator.plant_diagnostics._current_boiler_start = 1 if before["diagnostic_on"] else None
            diagnostic_seeded = True
        expected = (
            backend.boiler_switch, backend._last_boiler_change, backend._pending_command,
            backend._last_known_boiler_on, backend._trv_wait_start, backend._status_reason,
        )
        recorded = (
            before["switch"], before["last_change"],
            tuple(before["pending_command"]) if before["pending_command"] is not None else None,
            before["last_known_on"], before["trv_wait_start"],
            before["reason"] if emergency else data["reason_before"],
        )
        if expected != recorded or (not emergency and (
                (backend.coordinator.plant_diagnostics._current_boiler_start is not None)
                != before["diagnostic_on"])):
            raise ValueError(f"Relay record {record.get('seq')} has divergent previous backend state")
        if not emergency:
            backend._min_cycle_on = before["min_cycle_on"]
            backend._min_cycle_off = before["min_cycle_off"]
            backend._boiler_pwm.set_params(data["calculation_interval"], data["calculation_min_on"],
                                           data["calculation_min_off"])
            backend.coordinator.zones = before["zones"]
            backend.coordinator.is_dhw_active = before["dhw_active"]
        backend.coordinator.get_zone_mode = lambda zone: tape.zone_value("mode", zone)
        backend.coordinator.get_zone_demand = lambda zone: tape.zone_value("demand", zone)
        backend.hass.states.get = tape.read_state
        backend.hass.services.async_call = tape.service
        old_time, old_pwm_time = module.time, pwm_engine.time
        module.time = SimpleNamespace(monotonic=lambda: tape.take("clock")["value"])
        if not emergency:
            pwm_engine.time = SimpleNamespace(monotonic=lambda: data["calculation_time"])
        outcome = "completed"
        error = None
        try:
            try:
                if emergency:
                    await backend._async_emergency_off_impl()
                else:
                    await backend._async_update_impl(data["demand"], data["peak_zone"], data["frost_emergency"], {})
            except asyncio.CancelledError:
                outcome = "cancelled"
            except RuntimeError as err:
                outcome, error = "error", str(err)
        finally:
            module.time, pwm_engine.time = old_time, old_pwm_time
        if tape.events:
            raise ValueError(f"Relay record {record.get('seq')} has unconsumed replay events")
        pending = backend._pending_command[0] if backend._pending_command else None
        actual_after = {
            "last_change": backend._last_boiler_change,
            "pending_command": backend._pending_command,
            "last_known_on": backend._last_known_boiler_on,
            "trv_wait_start": backend._trv_wait_start,
            "delay_pending": backend._pending_boiler_task is not None,
        }
        recorded_after = after.copy()
        if recorded_after.get("pending_command") is not None:
            recorded_after["pending_command"] = tuple(recorded_after["pending_command"])
        if emergency:
            actual_after["reason"] = backend.status_reason
        expected_outcome = (
            "completed" if data["completed" if emergency else "update_completed"] else
            "cancelled" if data["cancelled" if emergency else "update_cancelled"] else "error"
        )
        if ((not emergency and (backend.status_reason != data["reason"] or pending != data["pending_command"]))
                or actual_after != recorded_after or outcome != expected_outcome
                or error != data["error" if emergency else "update_error"]):
            raise ValueError(f"Relay record {record.get('seq')} diverged during backend replay")
        results.append({"plant": row["plant"], "emergency" if emergency else "boiler": record,
                        "pwm_state": row["pwm_state"], "reason": backend.status_reason, "commands": tape.commands,
                        "outcome": outcome, "error": error})
    return results


async def replay_single_delayed_plant_from_records(records: Iterable[dict]) -> dict:
    """Replay one non-overlapping plant update and its actual delayed relay task."""
    import math
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat import pwm_engine
    from custom_components.multizone_thermostat.boiler import relay_backend as module

    records = list(records)
    linked = await replay_linked_plant_pwm_from_records(records)
    cycles = await replay_relay_delayed_commands_from_records(records)
    if len(linked) != 1 or len(cycles) != 1:
        raise ValueError("Delayed plant replay requires one update and one completed delay task")
    row, cycle = linked[0], cycles[0]
    plant, record = row["plant"], row["boiler"]
    data = record["data"]
    before, after, trace = data.get("replay_before"), data.get("replay_after"), data.get("replay_trace")
    required = ("switch", "min_cycle_on", "min_cycle_off", "valve_delay", "last_change",
                "pending_command", "last_known_on", "trv_wait_start", "delay_pending",
                "dhw_active", "diagnostic_on", "zones")
    if (not isinstance(before, dict) or any(key not in before for key in required)
            or not isinstance(after, dict) or not isinstance(trace, list)
            or not isinstance(before["zones"], list) or not isinstance(before["dhw_active"], bool)
            or not isinstance(before["diagnostic_on"], bool)):
        raise ValueError("Delayed plant update lacks replay inputs")
    if (not isinstance(before["valve_delay"], (int, float)) or isinstance(before["valve_delay"], bool)
            or not math.isfinite(before["valve_delay"]) or before["valve_delay"] <= 0
            or before["delay_pending"]):
        raise ValueError("Delayed plant replay requires a fresh positive delay")
    delay_rows = cycle["cycle"]["records"]
    scheduled, finished = delay_rows[0], delay_rows[-1]
    if (not plant["seq"] < scheduled["seq"] < record["seq"] < finished["seq"]
            or scheduled["data"]["seconds"] != before["valve_delay"]
            or scheduled["data"]["turn_on"] is not True):
        raise ValueError("Delayed plant recording has a divergent task boundary")
    waits = [rec for rec in delay_rows if rec["data"].get("phase") == "waiting"]
    expiries = [rec for rec in delay_rows if rec["data"].get("phase") == "expired"]
    if (len(waits) != 1 or len(expiries) != 1
            or not isinstance(waits[0].get("mono"), (int, float))
            or not isinstance(expiries[0].get("mono"), (int, float))
            or isinstance(waits[0]["mono"], bool) or isinstance(expiries[0]["mono"], bool)
            or not math.isfinite(waits[0]["mono"]) or not math.isfinite(expiries[0]["mono"])
            or expiries[0]["mono"] - waits[0]["mono"] + 1e-6 < before["valve_delay"]):
        raise ValueError("Delayed plant recording has an incomplete or premature delay")
    checks = [rec for rec in delay_rows if rec["data"].get("event") == "relay_delay_check"]
    if len(checks) != 1:
        raise ValueError("Delayed plant task lacks a unique start recheck")
    check = checks[0]["data"]
    path = None
    if "switch_path" in check:
        paths = [rec for rec in records if rec.get("seq") == checks[0]["seq"] - 1
                 and rec.get("kind") == "event" and rec.get("data", {}).get("event") == "relay_switch_path"]
        if len(paths) != 1:
            raise ValueError("Delayed plant task lacks its recorded switch-path decision")
        path = replay_relay_switch_paths_from_records(paths)[0][1]
        if path != check["switch_path"]:
            raise ValueError("Delayed plant task has divergent switch-path inputs")
    command_record = cycle["command"]
    command_data = command_record["data"] if command_record is not None else None
    command_tape = _RelayReplayTape(command_record, command_data["trace"]) if command_data else None
    update_tape = _RelayReplayTape(record, trace)
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    hass.states.get = update_tape.read_state
    hass.services.async_call = update_tape.service
    diag = SimpleNamespace(_current_boiler_start=1 if before["diagnostic_on"] else None)
    diag.record_boiler_state = lambda on: setattr(diag, "_current_boiler_start", 1 if on else None)
    coord = SimpleNamespace(
        zones=before["zones"], is_dhw_active=before["dhw_active"], plant_diagnostics=diag,
        get_zone_mode=lambda cid: update_tape.zone_value("mode", cid),
        get_zone_demand=lambda cid: update_tape.zone_value("demand", cid),
        get_master_state=lambda: check["master_on"],
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    backend = module.RelayBoilerBackend(hass, coord, before["switch"], before["min_cycle_on"],
                                        before["min_cycle_off"], before["valve_delay"])
    backend._last_boiler_change = before["last_change"]
    backend._pending_command = tuple(before["pending_command"]) if before["pending_command"] is not None else None
    backend._last_known_boiler_on = before["last_known_on"]
    backend._trv_wait_start = before["trv_wait_start"]
    backend._status_reason = data["reason_before"]
    backend._boiler_pwm.cycle_start_time = data["cycle_start_before"]
    backend._boiler_pwm._prev_demand = data["prev_demand_before"]
    backend._boiler_pwm.set_params(data["calculation_interval"], data["calculation_min_on"],
                                   data["calculation_min_off"])
    backend._delay_epoch = scheduled["data"]["epoch"]
    backend._delay_step = scheduled["data"]["step"]
    original_command = backend._trace_delayed_command

    async def command(turn_on, epoch, step):
        if command_data is None or turn_on != command_data["turn_on"]:
            raise ValueError("Delayed plant task issued an unexpected command")
        actual = {"switch": backend.boiler_switch, "last_change": backend._last_boiler_change,
                  "pending_command": backend._pending_command, "reason": backend._status_reason}
        expected = command_data["before"].copy()
        if expected["pending_command"] is not None:
            expected["pending_command"] = tuple(expected["pending_command"])
        if actual != expected:
            raise ValueError("Delayed plant task has divergent command state")
        await original_command(turn_on, epoch, step)

    async def sleep(seconds):
        if seconds != scheduled["data"]["seconds"]:
            raise ValueError("Delayed plant task requested an unexpected delay")

    backend._trace_delayed_command = command
    old_time, old_pwm_time, old_asyncio = module.time, pwm_engine.time, module.asyncio
    module.time = SimpleNamespace(monotonic=lambda: update_tape.take("clock")["value"])
    pwm_engine.time = SimpleNamespace(monotonic=lambda: data["calculation_time"])
    module.asyncio = SimpleNamespace(
        sleep=sleep, current_task=asyncio.current_task, CancelledError=asyncio.CancelledError,
    )
    task = None
    try:
        await backend._async_update_impl(data["demand"], data["peak_zone"], data["frost_emergency"], {})
        task = backend._pending_boiler_task
        expected_after = after.copy()
        if expected_after.get("pending_command") is not None:
            expected_after["pending_command"] = tuple(expected_after["pending_command"])
        actual_after = {"last_change": backend._last_boiler_change, "pending_command": backend._pending_command,
                        "last_known_on": backend._last_known_boiler_on, "trv_wait_start": backend._trv_wait_start,
                        "delay_pending": task is not None}
        if (update_tape.events or update_tape.commands or task is None or actual_after != expected_after
                or backend.status_reason != data["reason"]):
            raise ValueError("Delayed plant update diverged during backend replay")
        if backend._last_frost_emergency != check["frost"]:
            raise ValueError("Delayed plant task has divergent frost state")
        if "demand" in check and backend._last_demand != check["demand"]:
            raise ValueError("Delayed plant task has divergent demand")
        if "trv_path" in check and check["switch_path"] is False:
            if getattr(backend, "_trv_path_ready", False) != check["trv_path"]:
                raise ValueError("Delayed plant task has divergent TRV path state")
        coord.is_dhw_active = check.get("dhw_active", False)
        if "switch_path" in check:
            backend._switch_valve_path_open = lambda: path
        if command_tape is not None:
            hass.states.get = command_tape.read_state
            hass.services.async_call = command_tape.service
            module.time = SimpleNamespace(monotonic=lambda: command_tape.take("clock")["value"])
        try:
            await task
        except RuntimeError as err:
            if cycle.get("outcome") != "error" or str(err) != cycle.get("error"):
                raise
        if (backend._pending_boiler_task is not None or backend.status_reason != cycle["cycle"]["reason"]
                or (command_tape is not None and command_tape.events)):
            raise ValueError("Delayed plant task diverged during backend replay")
        if command_data is not None:
            actual = {"last_change": backend._last_boiler_change, "pending_command": backend._pending_command,
                      "reason": backend.status_reason}
            expected = command_data["after"].copy()
            if expected["pending_command"] is not None:
                expected["pending_command"] = tuple(expected["pending_command"])
            if actual != expected or command_tape.commands != cycle["commands"]:
                raise ValueError("Delayed plant command diverged during backend replay")
    finally:
        tasks = {pending for pending in (task, backend._pending_boiler_task) if pending is not None}
        for pending in tasks:
            if not pending.done():
                pending.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        module.time, pwm_engine.time, module.asyncio = old_time, old_pwm_time, old_asyncio
    return {
        "plant": plant, "boiler": record, "cycle": cycle,
        "final_state": {
            "last_change": backend._last_boiler_change, "pending_command": backend._pending_command,
            "last_known_on": backend._last_known_boiler_on, "trv_wait_start": backend._trv_wait_start,
            "delay_pending": backend._pending_boiler_task is not None, "reason": backend.status_reason,
            "diagnostic_on": backend.coordinator.plant_diagnostics._current_boiler_start is not None,
        },
    }


async def replay_sequential_delayed_plant_from_records(records: Iterable[dict]) -> list[dict]:
    """Replay non-overlapping delayed plant cycles with checked backend and PWM continuity."""
    records = list(records)
    decisions = await replay_linked_plant_pwm_from_records(records)
    cycles = await replay_relay_delayed_commands_from_records(records)
    if not decisions:
        raise ValueError("Delayed plant recording contains no updates")
    if len(decisions) != len(cycles):
        raise ValueError("Delayed plant recording has unmatched updates or tasks")
    results = []
    previous = None
    backend_epoch = None
    pwm_epoch = None
    for index, (decision, cycle) in enumerate(zip(decisions, cycles)):
        plant, boiler = decision["plant"], decision["boiler"]
        scheduled, finished = cycle["cycle"]["records"][0], cycle["cycle"]["records"][-1]
        current_backend_epoch = scheduled["data"]["epoch"]
        current_pwm_epoch = boiler["data"]["pwm_epoch"]
        if backend_epoch is None:
            backend_epoch, pwm_epoch = current_backend_epoch, current_pwm_epoch
        elif current_backend_epoch != backend_epoch or current_pwm_epoch != pwm_epoch:
            raise ValueError("Delayed plant recording crosses a backend or PWM epoch")
        start = next((i for i, rec in enumerate(records)
                      if rec.get("kind") == "aggregation" and rec.get("seq") == plant["seq"] - 1), None)
        end = next((i for i, rec in enumerate(records) if rec is finished), None)
        if (start is None or end is None or not plant["seq"] < scheduled["seq"] < boiler["seq"] < finished["seq"]
                or (index + 1 < len(decisions) and finished["seq"] >= decisions[index + 1]["plant"]["seq"] - 1)):
            raise ValueError("Delayed plant recording has overlapping or incomplete cycles")
        before = boiler["data"].get("replay_before")
        if not isinstance(before, dict):
            raise ValueError("Delayed plant update lacks previous backend state")
        if previous is not None:
            recorded = {
                "last_change": before["last_change"],
                "pending_command": tuple(before["pending_command"]) if before["pending_command"] is not None else None,
                "last_known_on": before["last_known_on"], "trv_wait_start": before["trv_wait_start"],
                "delay_pending": before["delay_pending"], "reason": boiler["data"]["reason_before"],
                "diagnostic_on": before["diagnostic_on"],
            }
            if recorded != previous:
                raise ValueError("Delayed plant recording has divergent previous backend state")
        replayed = await replay_single_delayed_plant_from_records(records[start:end + 1])
        previous = replayed["final_state"]
        results.append(replayed)
    return results


async def replay_waiting_delayed_plant_from_records(records: Iterable[dict]) -> dict:
    """Replay one pending delayed task with sequential plant updates while it waits."""
    import math
    from unittest.mock import MagicMock

    from custom_components.multizone_thermostat import pwm_engine
    from custom_components.multizone_thermostat.boiler import relay_backend as module

    records = list(records)
    decisions = await replay_linked_plant_pwm_from_records(records)
    cycles = await replay_relay_delayed_commands_from_records(records, _allow_wait_cancel_reason_change=True)
    if not decisions or len(cycles) != 1:
        raise ValueError("Waiting plant replay requires updates and one completed delay task")
    cycle = cycles[0]
    delay_rows = cycle["cycle"]["records"]
    scheduled, finished = delay_rows[0], delay_rows[-1]
    waits = [rec for rec in delay_rows if rec["data"].get("phase") == "waiting"]
    expiries = [rec for rec in delay_rows if rec["data"].get("phase") == "expired"]
    cancellations = [rec for rec in delay_rows if rec["data"].get("phase") == "cancelled"]
    checks = [rec for rec in delay_rows if rec["data"].get("event") == "relay_delay_check"]
    cancelled_wait = len(cancellations) == 1 and not expiries and not checks
    if len(waits) != 1 or not (cancelled_wait or (len(expiries) == 1 and len(checks) == 1)):
        raise ValueError("Waiting plant recording has an incomplete delay lifecycle")
    waiting = waits[0]
    terminal = cancellations[0] if cancelled_wait else expiries[0]
    check_record = None if cancelled_wait else checks[0]
    first = decisions[0]
    before = first["boiler"]["data"].get("replay_before")
    if not isinstance(before, dict) or not isinstance(before.get("valve_delay"), (int, float)):
        raise ValueError("Waiting plant update lacks replay inputs")
    delay = before["valve_delay"]
    if (isinstance(delay, bool) or not math.isfinite(delay) or delay <= 0
            or before.get("delay_pending") is not False
            or scheduled["data"].get("seconds") != delay or scheduled["data"].get("turn_on") is not True
            or not first["plant"]["seq"] < scheduled["seq"] < first["boiler"]["seq"] < waiting["seq"]
            or not waiting["seq"] < terminal["seq"] < finished["seq"]):
        raise ValueError("Waiting plant recording has a divergent task boundary")
    if (not isinstance(waiting.get("mono"), (int, float)) or isinstance(waiting["mono"], bool)
            or not isinstance(terminal.get("mono"), (int, float)) or isinstance(terminal["mono"], bool)
            or not math.isfinite(waiting["mono"]) or not math.isfinite(terminal["mono"])
            or terminal["mono"] < waiting["mono"]
            or (not cancelled_wait and terminal["mono"] - waiting["mono"] + 1e-6 < delay)):
        raise ValueError("Waiting plant recording has an incomplete or premature delay")
    for row in decisions:
        if (row["boiler"]["seq"] >= terminal["seq"]
                or row["plant"]["seq"] < waiting["seq"] < row["boiler"]["seq"]):
            raise ValueError("Waiting plant recording has overlapping updates or task execution")
    check = check_record["data"] if check_record is not None else None
    path = None
    if check is not None and "switch_path" in check:
        paths = [rec for rec in records if rec.get("seq") == check_record["seq"] - 1
                 and rec.get("kind") == "event" and rec.get("data", {}).get("event") == "relay_switch_path"]
        if len(paths) != 1:
            raise ValueError("Waiting plant task lacks its recorded switch-path decision")
        path = replay_relay_switch_paths_from_records(paths)[0][1]
        if path != check["switch_path"]:
            raise ValueError("Waiting plant task has divergent switch-path inputs")
    command_record = cycle["command"]
    command_data = command_record["data"] if command_record is not None else None
    command_tape = _RelayReplayTape(command_record, command_data["trace"]) if command_data else None
    required = ("switch", "min_cycle_on", "min_cycle_off", "valve_delay", "last_change", "pending_command",
                "last_known_on", "trv_wait_start", "delay_pending", "dhw_active", "diagnostic_on", "zones")
    if any(key not in before for key in required):
        raise ValueError("Waiting plant update lacks previous backend state")
    hass = MagicMock()
    hass.async_create_task = asyncio.create_task
    diag = SimpleNamespace(_current_boiler_start=1 if before["diagnostic_on"] else None)
    diag.record_boiler_state = lambda on: setattr(diag, "_current_boiler_start", 1 if on else None)
    coord = SimpleNamespace(
        zones=[], is_dhw_active=False, plant_diagnostics=diag,
        get_master_state=lambda: check["master_on"],
        record_diagnostic_event=lambda *_: None, reset_last_active_time=lambda: None,
    )
    backend = module.RelayBoilerBackend(hass, coord, before["switch"], before["min_cycle_on"],
                                        before["min_cycle_off"], delay)
    backend._last_boiler_change = before["last_change"]
    backend._pending_command = tuple(before["pending_command"]) if before["pending_command"] is not None else None
    backend._last_known_boiler_on = before["last_known_on"]
    backend._trv_wait_start = before["trv_wait_start"]
    backend._status_reason = first["boiler"]["data"]["reason_before"]
    backend._boiler_pwm.cycle_start_time = first["boiler"]["data"]["cycle_start_before"]
    backend._boiler_pwm._prev_demand = first["boiler"]["data"]["prev_demand_before"]
    backend._delay_epoch = scheduled["data"]["epoch"]
    backend._delay_step = scheduled["data"]["step"]
    original_command = backend._trace_delayed_command
    waiter = None

    async def command(turn_on, epoch, step):
        if command_data is None or turn_on != command_data["turn_on"]:
            raise ValueError("Waiting plant task issued an unexpected command")
        actual = {"switch": backend.boiler_switch, "last_change": backend._last_boiler_change,
                  "pending_command": backend._pending_command, "reason": backend._status_reason}
        expected = command_data["before"].copy()
        if expected["pending_command"] is not None:
            expected["pending_command"] = tuple(expected["pending_command"])
        if actual != expected:
            raise ValueError("Waiting plant task has divergent command state")
        await original_command(turn_on, epoch, step)

    async def sleep(seconds):
        nonlocal waiter
        if seconds != delay or waiter is not None:
            raise ValueError("Waiting plant task requested an unexpected delay")
        waiter = asyncio.get_running_loop().create_future()
        await waiter

    backend._trace_delayed_command = command
    old_time, old_pwm_time, old_asyncio = module.time, pwm_engine.time, module.asyncio
    module.asyncio = SimpleNamespace(
        sleep=sleep, current_task=asyncio.current_task, CancelledError=asyncio.CancelledError,
    )
    task = None
    cancelled_by_update = False

    def final_state():
        return {
            "last_change": backend._last_boiler_change, "pending_command": backend._pending_command,
            "last_known_on": backend._last_known_boiler_on, "trv_wait_start": backend._trv_wait_start,
            "delay_pending": backend._pending_boiler_task is not None, "reason": backend.status_reason,
            "diagnostic_on": diag._current_boiler_start is not None,
        }

    try:
        for index, row in enumerate(decisions):
            record, data = row["boiler"], row["boiler"]["data"]
            if data["pwm_epoch"] != first["boiler"]["data"]["pwm_epoch"]:
                raise ValueError("Waiting plant recording crosses a PWM epoch")
            state, after, trace = data.get("replay_before"), data.get("replay_after"), data.get("replay_trace")
            if (not isinstance(state, dict) or any(key not in state for key in required)
                    or not isinstance(state.get("zones"), list) or not isinstance(after, dict)
                    or not isinstance(trace, list)):
                raise ValueError("Waiting plant update lacks replay inputs")
            if index and waiting["seq"] < row["plant"]["seq"] and waiter is None:
                await asyncio.sleep(0)
                if waiter is None:
                    raise ValueError("Waiting plant task did not enter its recorded wait")
            actual_before = (backend.boiler_switch, backend._last_boiler_change, backend._pending_command,
                             backend._last_known_boiler_on, backend._trv_wait_start,
                             backend._pending_boiler_task is not None, backend.status_reason,
                             diag._current_boiler_start is not None, backend._boiler_pwm.cycle_start_time,
                             getattr(backend._boiler_pwm, "_prev_demand", 0.0))
            expected_before = (state["switch"], state["last_change"],
                               tuple(state["pending_command"]) if state["pending_command"] is not None else None,
                               state["last_known_on"], state["trv_wait_start"], state["delay_pending"],
                               data["reason_before"], state["diagnostic_on"],
                               data["cycle_start_before"], data["prev_demand_before"])
            if actual_before != expected_before or state["valve_delay"] != delay:
                raise ValueError("Waiting plant update has divergent previous backend state")
            tape = _RelayReplayTape(record, trace)
            hass.states.get, hass.services.async_call = tape.read_state, tape.service
            coord.zones, coord.is_dhw_active = state["zones"], state["dhw_active"]
            coord.get_zone_mode = lambda cid: tape.zone_value("mode", cid)
            coord.get_zone_demand = lambda cid: tape.zone_value("demand", cid)
            backend._min_cycle_on, backend._min_cycle_off = state["min_cycle_on"], state["min_cycle_off"]
            backend._boiler_pwm.set_params(data["calculation_interval"], data["calculation_min_on"],
                                           data["calculation_min_off"])
            module.time = SimpleNamespace(monotonic=lambda: tape.take("clock")["value"])
            pwm_engine.time = SimpleNamespace(monotonic=lambda: data["calculation_time"])
            await backend._async_update_impl(data["demand"], data["peak_zone"], data["frost_emergency"], {})
            if task is None:
                task = backend._pending_boiler_task
            expected_after = after.copy()
            if expected_after.get("pending_command") is not None:
                expected_after["pending_command"] = tuple(expected_after["pending_command"])
            actual_after = {"last_change": backend._last_boiler_change, "pending_command": backend._pending_command,
                            "last_known_on": backend._last_known_boiler_on, "trv_wait_start": backend._trv_wait_start,
                            "delay_pending": backend._pending_boiler_task is not None}
            update_cancelled = task is not None and backend._pending_boiler_task is None and task.cancelling()
            if update_cancelled:
                if (not cancelled_wait or cancelled_by_update or index != len(decisions) - 1
                        or waiter is None or waiting["seq"] >= row["plant"]["seq"]):
                    raise ValueError("Waiting plant recording has an unsupported task cancellation")
                cancelled_by_update = True
            elif task is None or backend._pending_boiler_task is not task or task.done():
                raise ValueError("Waiting plant update diverged or cancelled the delayed task")
            if (tape.events or tape.commands or actual_after != expected_after
                    or backend.status_reason != data["reason"]):
                raise ValueError("Waiting plant update diverged during backend replay")
        if cancelled_wait:
            if not cancelled_by_update:
                if waiter is None:
                    await asyncio.sleep(0)
                if waiter is None or waiter.done() or task.done():
                    raise ValueError("Waiting plant task did not reach its recorded wait")
                task.cancel()
            if waiter is None or not waiter.cancelled():
                raise ValueError("Waiting plant task did not receive cancellation")
            await task
            if backend._pending_boiler_task is not None or backend.status_reason != cycle["cycle"]["reason"]:
                raise ValueError("Waiting plant cancellation diverged during backend replay")
            return {"updates": decisions, "cycle": cycle,
                    "cancellation_source": "update" if cancelled_by_update else "unknown",
                    "final_state": final_state()}
        if waiter is None:
            await asyncio.sleep(0)
        if waiter is None or waiter.done():
            raise ValueError("Waiting plant task did not reach its recorded wait")
        if backend._last_frost_emergency != check["frost"]:
            raise ValueError("Waiting plant task has divergent frost state")
        if "demand" in check and backend._last_demand != check["demand"]:
            raise ValueError("Waiting plant task has divergent demand")
        if "trv_path" in check and check["switch_path"] is False:
            if getattr(backend, "_trv_path_ready", False) != check["trv_path"]:
                raise ValueError("Waiting plant task has divergent TRV path state")
        coord.is_dhw_active = check.get("dhw_active", False)
        if "switch_path" in check:
            backend._switch_valve_path_open = lambda: path
        if command_tape is not None:
            hass.states.get, hass.services.async_call = command_tape.read_state, command_tape.service
            module.time = SimpleNamespace(monotonic=lambda: command_tape.take("clock")["value"])
        waiter.set_result(None)
        try:
            await task
        except RuntimeError as err:
            if cycle.get("outcome") != "error" or str(err) != cycle.get("error"):
                raise
        if (backend._pending_boiler_task is not None or backend.status_reason != cycle["cycle"]["reason"]
                or (command_tape is not None and command_tape.events)):
            raise ValueError("Waiting plant task diverged during backend replay")
        if command_data is not None:
            expected = command_data["after"].copy()
            if expected["pending_command"] is not None:
                expected["pending_command"] = tuple(expected["pending_command"])
            actual = {"last_change": backend._last_boiler_change, "pending_command": backend._pending_command,
                      "reason": backend.status_reason}
            if actual != expected or command_tape.commands != cycle["commands"]:
                raise ValueError("Waiting plant command diverged during backend replay")
    finally:
        tasks = {pending for pending in (task, backend._pending_boiler_task) if pending is not None}
        for pending in tasks:
            if not pending.done():
                pending.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        module.time, pwm_engine.time, module.asyncio = old_time, old_pwm_time, old_asyncio
    return {"updates": decisions, "cycle": cycle, "final_state": final_state()}


async def replay_sequential_waiting_plant_from_records(records: Iterable[dict]) -> list[dict]:
    """Replay non-overlapping waiting cycles, checking backend and PWM continuity."""
    records = list(records)
    decisions = await replay_linked_plant_pwm_from_records(records)
    cycles = await replay_relay_delayed_commands_from_records(records, _allow_wait_cancel_reason_change=True)
    if not decisions or not cycles:
        raise ValueError("Waiting plant recording requires updates and delay tasks")
    results = []
    previous = None
    backend_epoch = None
    pwm_epoch = None
    next_decision = 0
    for cycle in cycles:
        scheduled, finished = cycle["cycle"]["records"][0], cycle["cycle"]["records"][-1]
        start_index = next_decision
        while next_decision < len(decisions) and decisions[next_decision]["boiler"]["seq"] < finished["seq"]:
            next_decision += 1
        group = decisions[start_index:next_decision]
        if not group or not group[0]["plant"]["seq"] < scheduled["seq"] < group[0]["boiler"]["seq"]:
            raise ValueError("Waiting plant recording has unmatched or overlapping cycles")
        if results and group[0]["plant"]["seq"] - 1 <= results[-1]["cycle"]["cycle"]["records"][-1]["seq"]:
            raise ValueError("Waiting plant recording has overlapping cycles")
        current_backend_epoch = scheduled["data"]["epoch"]
        current_pwm_epoch = group[0]["boiler"]["data"]["pwm_epoch"]
        if backend_epoch is None:
            backend_epoch, pwm_epoch = current_backend_epoch, current_pwm_epoch
        elif current_backend_epoch != backend_epoch or current_pwm_epoch != pwm_epoch:
            raise ValueError("Waiting plant recording crosses a backend or PWM epoch")
        before = group[0]["boiler"]["data"].get("replay_before")
        if not isinstance(before, dict):
            raise ValueError("Waiting plant update lacks previous backend state")
        if previous is not None:
            try:
                recorded = {
                    "last_change": before["last_change"],
                    "pending_command": tuple(before["pending_command"]) if before["pending_command"] is not None else None,
                    "last_known_on": before["last_known_on"], "trv_wait_start": before["trv_wait_start"],
                    "delay_pending": before["delay_pending"], "reason": group[0]["boiler"]["data"]["reason_before"],
                    "diagnostic_on": before["diagnostic_on"],
                }
            except KeyError as err:
                raise ValueError("Waiting plant update lacks previous backend state") from err
            if recorded != previous:
                raise ValueError("Waiting plant recording has divergent previous backend state")
        start = next((i for i, rec in enumerate(records)
                      if rec.get("kind") == "aggregation" and rec.get("seq") == group[0]["plant"]["seq"] - 1), None)
        end = next((i for i, rec in enumerate(records) if rec is finished), None)
        if start is None or end is None or start >= end:
            raise ValueError("Waiting plant recording has incomplete cycle boundaries")
        replayed = await replay_waiting_delayed_plant_from_records(records[start:end + 1])
        previous = replayed["final_state"]
        results.append(replayed)
    if next_decision != len(decisions):
        raise ValueError("Waiting plant recording has unmatched updates or tasks")
    return results


def replay_smart_return_plans_from_records(records: Iterable[dict]) -> list[tuple[dict, dict]]:
    """Recompute recorded smart-return plans with the effective raw numerical inputs."""
    from collections import deque
    from copy import deepcopy
    from types import SimpleNamespace

    from custom_components.multizone_thermostat.schedule_manager import ScheduleProfileManager

    rows = []
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "smart_return_plan":
            continue
        required = ("now", "arrival", "arrival_is_datetime", "preset", "zones", "inputs", "plan")
        if (any(key not in data for key in required) or not isinstance(data["arrival_is_datetime"], bool)
                or not isinstance(data["inputs"], list) or not isinstance(data["plan"], dict)):
            raise ValueError(f"Smart-return record {rec.get('seq')} lacks replay inputs")
        tape = deque()
        zones = []
        for sample in data["inputs"]:
            if (not isinstance(sample, dict) or not isinstance(sample.get("name"), str)
                    or not isinstance(sample.get("id"), str) or not isinstance(sample.get("selected"), bool)):
                raise ValueError(f"Smart-return record {rec.get('seq')} has invalid zone inputs")
            zones.append({"name": sample["name"], "climate_id": sample["id"]})
            if sample["selected"]:
                for key in ("temperature", "target", "heating_rate"):
                    if key not in sample:
                        raise ValueError(f"Smart-return record {rec.get('seq')} lacks {key} for {sample['id']}")
                    tape.append((sample["id"], key, sample[key]))

        def take(cid, kind):
            if not tape or tape[0][:2] != (cid, kind):
                raise ValueError(f"Smart-return record {rec.get('seq')} has divergent {kind} input for {cid}")
            return tape.popleft()[2]

        def target(preset, cid):
            if preset != data["preset"]:
                raise ValueError(f"Smart-return record {rec.get('seq')} has divergent preset")
            return take(cid, "target")

        manager = ScheduleProfileManager(SimpleNamespace(zones=zones))
        manager._get_zone_current_temp = lambda cid: take(cid, "temperature")
        manager._resolve_preset_temp = target
        manager._get_zone_heating_rate = lambda cid: take(cid, "heating_rate")
        arrival = data["arrival"]
        if data["arrival_is_datetime"] and isinstance(arrival, str):
            arrival = datetime.fromisoformat(arrival)
        plan = manager.calculate_smart_return_plan(
            arrival, data["preset"], deepcopy(data["zones"]), datetime.fromisoformat(data["now"]),
        )
        if tape or plan != data["plan"]:
            raise ValueError(f"Smart-return record {rec.get('seq')} diverged during replay")
        rows.append((rec, plan))
    return rows


async def replay_schedule_evaluations_from_records(records: Iterable[dict]) -> list[tuple[dict, dict]]:
    """Replay schedule evaluations from effective inputs, including recorded smart-return plans."""
    from copy import deepcopy
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.multizone_thermostat.schedule_manager import ScheduleProfileManager

    rows = []
    fields = {
        "enabled": "scheduler_enabled", "active_profile": "active_profile",
        "expires_at": "profile_expires_at", "fallback_profile": "fallback_profile",
        "manual_overrides": "manual_overrides", "smart_return": "smart_return",
        "schedule_overrides": "schedule_overrides", "smart_start_info": "smart_start_info",
    }
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "schedule_evaluation":
            continue
        before, after, zones = data.get("before"), data.get("after"), data.get("zones")
        if (not isinstance(before, dict) or not isinstance(after, dict) or not isinstance(zones, list)
                or any(key not in before or key not in after for key in fields)
                or "now" not in data or "profile_expired" not in data or "smart_return_completed" not in data):
            raise ValueError(f"Schedule record {rec.get('seq')} lacks replay inputs")
        smart_return = before["smart_return"]
        if smart_return is not None and not isinstance(smart_return, dict):
            raise ValueError(f"Schedule record {rec.get('seq')} has invalid smart return")
        if smart_return is not None and (
                not isinstance(data.get("smart_return_actions"), list)
                or not isinstance(data.get("completion_zones"), list)
                or (not data["smart_return_completed"] and not isinstance(data.get("smart_return_plan"), dict))):
            raise ValueError(f"Schedule record {rec.get('seq')} lacks smart-return inputs")
        observations = {}
        for sample in zones:
            if (not isinstance(sample, dict) or not isinstance(sample.get("id"), str)
                    or not isinstance(sample.get("initially_off"), bool) or sample["id"] in observations):
                raise ValueError(f"Schedule record {rec.get('seq')} has invalid zone observations")
            observations[sample["id"]] = sample
        used = []
        actions = []
        completion_zones = data.get("completion_zones", [])
        if any(not isinstance(zone, dict) or not isinstance(zone.get("id"), str) for zone in completion_zones):
            raise ValueError(f"Schedule record {rec.get('seq')} has invalid completion zones")
        zone_ids = [zone["id"] for zone in completion_zones] if data["smart_return_completed"] else list(observations)
        if data["smart_return_completed"] and observations and zone_ids != list(observations):
            raise ValueError(f"Schedule record {rec.get('seq')} has divergent zone ordering")
        modes = [(zone["id"], zone["mode"]) for zone in completion_zones if "mode" in zone]
        modes.extend((zone["id"], zone["mode"]) for zone in zones if "mode" in zone)

        def mode(cid):
            if not modes or modes[0][0] != cid:
                raise ValueError(f"Schedule record {rec.get('seq')} has incomplete zone mode inputs")
            return modes.pop(0)[1]

        def value(cid, key):
            if cid not in observations or key not in observations[cid]:
                raise ValueError(f"Schedule record {rec.get('seq')} lacks {key} for {cid}")
            used.append((cid, key))
            return deepcopy(observations[cid][key])

        def upcoming(cid, _now, horizon_hours):
            if horizon_hours != 12:
                raise ValueError(f"Schedule record {rec.get('seq')} requested an unexpected horizon")
            blocks = value(cid, "upcoming")
            for block in blocks:
                if isinstance(block.get("start_dt"), str):
                    block["start_dt"] = datetime.fromisoformat(block["start_dt"])
            return blocks

        hass = MagicMock()
        hass.states.get.side_effect = lambda cid: SimpleNamespace(
            state="off" if observations[cid]["initially_off"] else "heat",
        )
        coord = SimpleNamespace(
            hass=hass, zones=[{"climate_id": cid} for cid in zone_ids],
            _climate_entities={}, _climate_callbacks={}, record_diagnostic_event=lambda *_: None,
            get_zone_mode=mode,
        )
        recorded_actions = data.get("smart_return_actions", [])
        if any(action.get("action") == "set_preset" for action in recorded_actions):
            async def set_preset(preset):
                actions.append({"action": "set_preset", "preset": preset})
            coord.zone_manager = SimpleNamespace(async_set_global_preset=set_preset)
        manager = ScheduleProfileManager(coord)
        for key, attr in fields.items():
            setattr(manager, attr, deepcopy(before[key]))
        manager.async_save_to_storage = AsyncMock()
        manager._notify_climates = MagicMock()
        manager.evaluate_zone_slot = lambda cid, _now: value(cid, "slot")
        manager.get_upcoming_blocks = upcoming
        manager._get_zone_current_temp = lambda cid: value(cid, "temperature")
        manager._get_zone_heating_rate = lambda cid: value(cid, "heating_rate")
        async def wake_master():
            actions.append({"action": "wake_master"})
        async def wake_zone(cid, *_args):
            actions.append({"action": "wake_zone", "id": cid})
        manager._async_wake_master = wake_master
        manager._async_wake_zone = wake_zone
        plan_calls = []
        def smart_return_plan(arrival, preset, selected_zones, now):
            if (smart_return is None or arrival != smart_return["arrival_time"]
                    or preset != smart_return.get("preset", "comfort")
                    or selected_zones != smart_return.get("zones")
                    or now != datetime.fromisoformat(data["now"])):
                raise ValueError(f"Schedule record {rec.get('seq')} has divergent smart-return inputs")
            plan_calls.append(True)
            return deepcopy(data["smart_return_plan"])
        manager.calculate_smart_return_plan = smart_return_plan
        await manager.async_evaluate_schedule(datetime.fromisoformat(data["now"]))
        expected_reads = {(cid, key) for cid, sample in observations.items()
                          for key in ("slot", "upcoming", "temperature", "heating_rate") if key in sample}
        actual_after = {key: getattr(manager, attr) for key, attr in fields.items()}
        if (set(used) != expected_reads or actual_after != after
                or manager.async_save_to_storage.await_count != (
                    int(data["profile_expired"]) + int(data["smart_return_completed"])
                ) or actions != recorded_actions or modes
                or bool(plan_calls) != (smart_return is not None and not data["smart_return_completed"])):
            raise ValueError(f"Schedule record {rec.get('seq')} diverged during replay")
        rows.append((rec, actual_after))
    return rows


async def replay_linked_smart_return_evaluations_from_records(records: Iterable[dict]) -> list[dict]:
    """Replay plan calculations and match them to evaluations by their captured identifier."""
    records = list(records)
    sequences = [rec.get("seq") for rec in records if rec.get("kind") == "event"
                 and rec.get("data", {}).get("event") in ("smart_return_plan", "schedule_evaluation")]
    if (any(not isinstance(seq, int) or isinstance(seq, bool) for seq in sequences)
            or any(left >= right for left, right in zip(sequences, sequences[1:]))):
        raise ValueError("Schedule recording has missing, duplicate or reordered sequence numbers")
    plans = replay_smart_return_plans_from_records(records)
    evaluations = await replay_schedule_evaluations_from_records(records)
    by_id = {id(record): plan for record, plan in plans}
    evaluation_by_id = {id(record): after for record, after in evaluations}
    plan_by_evaluation = {}
    for index, rec in enumerate(records):
        if rec.get("kind") == "event" and rec.get("data", {}).get("event") == "smart_return_plan":
            evaluation_id = rec["data"].get("evaluation_id")
            if evaluation_id is not None:
                plan_by_evaluation.setdefault(evaluation_id, []).append((index, rec))
    seen = set()
    results = []
    for index, rec in enumerate(records):
        if rec.get("kind") != "event":
            continue
        data = rec.get("data", {})
        if data.get("event") == "schedule_evaluation":
            smart_return = data["before"]["smart_return"]
            matching = []
            if smart_return is not None and not data["smart_return_completed"]:
                evaluation_id = data.get("evaluation_id")
                if not isinstance(evaluation_id, str) or not evaluation_id or evaluation_id in seen:
                    raise ValueError(f"Schedule record {rec.get('seq')} has an invalid evaluation boundary")
                seen.add(evaluation_id)
                candidates = plan_by_evaluation.get(evaluation_id, [])
                for position, candidate in candidates:
                    plan_data = candidate["data"]
                    if (position < index and plan_data["now"] == data["now"]
                            and plan_data["arrival"] == smart_return["arrival_time"]
                            and plan_data["preset"] == smart_return.get("preset", "comfort")
                            and plan_data["zones"] == smart_return.get("zones")
                            and by_id[id(candidate)] == data.get("smart_return_plan")):
                        matching.append(candidate)
                if len(candidates) != 1:
                    matching = []
                if len(matching) != 1:
                    raise ValueError(f"Schedule record {rec.get('seq')} lacks a unique matching smart-return plan")
            results.append({"evaluation": rec, "after": evaluation_by_id[id(rec)],
                            "plan": matching[0] if matching else None})
    return results


async def replay_manual_schedule_overrides_from_records(records: Iterable[dict]) -> list[dict]:
    """Replay recorded manual override mutations, without their subsequent evaluations."""
    from copy import deepcopy
    from unittest.mock import AsyncMock

    from custom_components.multizone_thermostat import schedule_manager as module

    rows = []
    next_steps = {}
    sequences = []
    for rec in records:
        data = rec.get("data", {})
        if rec.get("kind") != "event" or data.get("event") != "schedule_manual_override":
            continue
        seq, epoch, step = rec.get("seq"), data.get("evaluation_epoch"), data.get("override_step")
        if (not isinstance(seq, int) or isinstance(seq, bool) or (sequences and seq <= sequences[-1])
                or not isinstance(epoch, str) or not epoch
                or not isinstance(step, int) or isinstance(step, bool) or step < 0
                or (epoch in next_steps and step != next_steps[epoch])):
            raise ValueError("Manual override recording has an invalid or missing boundary")
        sequences.append(seq)
        next_steps[epoch] = step + 1
        before, after = data.get("before"), data.get("after")
        if (not isinstance(before, dict) or not isinstance(after, dict)
                or any(not isinstance(state.get(key), dict) for state in (before, after)
                       for key in ("manual_overrides", "schedule_overrides"))):
            raise ValueError("Manual override recording lacks state")
        action, cid = data.get("action"), data.get("climate_id")
        if action not in ("set", "clear") or (cid is not None and not isinstance(cid, str)):
            raise ValueError("Manual override recording has invalid action")
        manager = module.ScheduleProfileManager(SimpleNamespace(hass=SimpleNamespace()))
        manager.manual_overrides = deepcopy(before["manual_overrides"])
        manager.schedule_overrides = deepcopy(before["schedule_overrides"])
        manager.async_evaluate_schedule = AsyncMock()
        manager._notify_climates = lambda: None
        original_telemetry = module.telemetry
        module.telemetry = SimpleNamespace(get_recorder=lambda: SimpleNamespace(enabled=False))
        try:
            if action == "set":
                inputs = data.get("inputs")
                if (not isinstance(cid, str) or not isinstance(inputs, dict)
                        or any(key not in inputs for key in ("target_temp", "slot_id", "until", "now"))
                        or not isinstance(inputs["until"], str) or not isinstance(inputs["now"], str)
                        or not isinstance(inputs["target_temp"], (int, float))
                        or isinstance(inputs["target_temp"], bool)):
                    raise ValueError("Manual override recording lacks set inputs")
                manager.evaluate_zone_slot = lambda *_: {"slot_id": inputs["slot_id"], "until": inputs["until"]}
                manager.record_manual_override(cid, inputs["target_temp"], datetime.fromisoformat(inputs["now"]))
            else:
                await manager.async_clear_manual_override(cid)
        finally:
            module.telemetry = original_telemetry
        actual = {"manual_overrides": manager.manual_overrides, "schedule_overrides": manager.schedule_overrides}
        if actual != after:
            raise ValueError("Manual override recording diverged during replay")
        rows.append({"record": rec, "after": actual})
    return rows


async def replay_stateful_schedule_evaluations_from_records(records: Iterable[dict]) -> list[dict]:
    """Replay non-overlapping schedule evaluations with continuous recorded state."""
    records = list(records)
    lifecycle = [rec for rec in records if (
        rec.get("kind") == "event" and rec.get("data", {}).get("event") in (
            "schedule_evaluation_start", "schedule_evaluation", "schedule_manual_override",
        )
    ) or (
        rec.get("kind") == "config" and rec.get("data", {}).get("source") in (
            "schedule_startup", "schedule_saved",
        )
    )]
    sequences = [rec.get("seq") for rec in lifecycle]
    if (any(not isinstance(seq, int) or isinstance(seq, bool) for seq in sequences)
            or any(left >= right for left, right in zip(sequences, sequences[1:]))):
        raise ValueError("Schedule lifecycle has missing, duplicate or reordered sequence numbers")
    linked = await replay_linked_smart_return_evaluations_from_records(records)
    by_record = {id(row["evaluation"]): row for row in linked}
    overrides = await replay_manual_schedule_overrides_from_records(records)
    override_by_record = {id(row["record"]): row for row in overrides}
    initial_overrides = {}
    pending = None
    previous = None
    seen = set()
    seen_epochs = set()
    epoch = None
    next_step = None
    checkpoints = {}
    checkpoint = None
    result = []
    for rec in lifecycle:
        data = rec["data"]
        if rec["kind"] == "config":
            recorded_epoch, snapshot = data.get("evaluation_epoch"), data.get("schedule")
            fields = ("enabled", "active_profile", "expires_at", "fallback_profile", "manual_overrides",
                      "smart_return", "schedule_overrides", "smart_start_info")
            if (not isinstance(recorded_epoch, str) or not recorded_epoch or not isinstance(snapshot, dict)
                    or any(key not in snapshot for key in fields)):
                raise ValueError(f"Schedule record {rec.get('seq')} lacks checkpoint state")
            if pending is not None:
                if recorded_epoch != epoch:
                    raise ValueError("Schedule recording has overlapping manager epochs")
                continue
            state = {key: snapshot[key] for key in fields}
            initial_overrides.pop(recorded_epoch, None)
            if recorded_epoch == epoch:
                previous, checkpoint = state, rec
            else:
                checkpoints[recorded_epoch] = (state, rec)
            continue
        if data.get("event") == "schedule_manual_override":
            if pending is not None:
                raise ValueError("Schedule recording has overlapping manual override and evaluation")
            recorded_epoch = data["evaluation_epoch"]
            if recorded_epoch != epoch and recorded_epoch in seen_epochs:
                raise ValueError("Schedule recording has a reordered or overlapping epoch")
            if recorded_epoch == epoch and previous is not None:
                before = {key: previous[key] for key in ("manual_overrides", "schedule_overrides")}
                if before != data["before"]:
                    raise ValueError("Manual override has divergent previous schedule state")
                previous = {**previous, **override_by_record[id(rec)]["after"]}
            elif recorded_epoch in checkpoints:
                state, saved = checkpoints[recorded_epoch]
                if {key: state[key] for key in ("manual_overrides", "schedule_overrides")} != data["before"]:
                    raise ValueError("Manual override has divergent previous schedule state")
                checkpoints[recorded_epoch] = ({**state, **override_by_record[id(rec)]["after"]}, saved)
            else:
                prior = initial_overrides.get(recorded_epoch)
                if prior is not None and prior != data["before"]:
                    raise ValueError("Manual override has divergent previous schedule state")
                initial_overrides[recorded_epoch] = override_by_record[id(rec)]["after"]
            continue
        evaluation_id = data.get("evaluation_id")
        if not isinstance(evaluation_id, str) or not evaluation_id:
            raise ValueError(f"Schedule record {rec.get('seq')} lacks an evaluation boundary")
        if data["event"] == "schedule_evaluation_start":
            if pending is not None:
                raise ValueError("Schedule recording has overlapping or incomplete evaluations")
            recorded_epoch, step = data.get("evaluation_epoch"), data.get("evaluation_step")
            if (not isinstance(recorded_epoch, str) or not recorded_epoch
                    or not isinstance(step, int) or isinstance(step, bool) or step < 0):
                raise ValueError(f"Schedule record {rec.get('seq')} lacks an epoch or step")
            if recorded_epoch != epoch:
                if recorded_epoch in seen_epochs:
                    raise ValueError("Schedule recording has a reordered or overlapping epoch")
                epoch, next_step = recorded_epoch, step
                previous, checkpoint = checkpoints.pop(epoch, (None, None))
                seen_epochs.add(epoch)
            if step != next_step:
                raise ValueError("Schedule recording has a missing or duplicate evaluation step")
            if evaluation_id in seen:
                raise ValueError("Schedule recording has a duplicate evaluation boundary")
            if not isinstance(data.get("before"), dict):
                raise ValueError(f"Schedule record {rec.get('seq')} lacks previous state")
            initial = initial_overrides.pop(epoch, None)
            if initial is not None and any(data["before"].get(key) != value for key, value in initial.items()):
                raise ValueError("Schedule evaluation has divergent previous manual override state")
            if previous is not None and data["before"] != previous:
                raise ValueError(f"Schedule record {rec.get('seq')} has divergent previous state")
            seen.add(evaluation_id)
            pending = rec
        else:
            if (pending is None or pending["data"]["evaluation_id"] != evaluation_id
                    or data.get("evaluation_epoch") != epoch or data.get("evaluation_step") != next_step
                    or pending["data"].get("now") != data.get("now")
                    or pending["data"]["before"] != data.get("before")):
                raise ValueError(f"Schedule record {rec.get('seq')} lacks a matching evaluation start")
            row = by_record[id(rec)]
            previous = row["after"]
            result.append({"start": pending, "checkpoint": checkpoint, **row})
            pending = None
            checkpoint = None
            next_step += 1
    if pending is not None:
        raise ValueError("Schedule recording ends with an incomplete evaluation")
    return result


@dataclass
class PidStep:
    t: float
    temp: float
    set_point: float
    output: float
    updated: bool
    p: float
    i: float
    d: float
    error: float


def replay_pid(samples: Iterable[tuple[float, float]], set_point, *, module=None, **pid_kwargs) -> list[PidStep]:
    """Feed (t, temp) to the real PID with explicit timestamps (sampling_period=0), tracing every term.

    `set_point` may be a number or a function of t. `module` selects the PID implementation
    (default: production; pass `load_reference_module("pid_v0")` for the frozen original).
    """
    module = module or load_prod_module("pid")
    pid = module.PID(**{"kp": 0.0, "ki": 0.0, "kd": 0.0, "out_min": 0.0, "out_max": 100.0, **pid_kwargs})
    steps: list[PidStep] = []
    last_t = None
    for t, temp in samples:
        sp = set_point(t) if callable(set_point) else set_point
        out, updated = pid.calc(temp, sp, input_time=t, last_input_time=last_t)
        last_t = t
        steps.append(PidStep(t, temp, sp, out, updated, pid.proportional, pid.integral, pid.derivative, pid.error))
    return steps


@dataclass
class PidReplayRow:
    """One replayed `pid` record: what production logged vs what the replayed PID produced."""
    seq: int
    mono: float
    logged: dict
    out: float
    updated: bool
    p: float
    i: float
    d: float
    error: float
    dt: float


def replay_pid_from_records(records: Iterable[dict], zone: str, *, module=None) -> list[PidReplayRow]:
    """Re-run logged `pid` records through a PID (production by default) and return logged-vs-replayed rows.

    Exact only from a clean-state boundary: replay starts at the first record whose
    `last_input_time` is None (fresh controller or `clear_samples`), because integral and derivative
    history before that point are not in the log. A later `last_input_time is None` record resets the
    replayed PID exactly as MultizonePID.clear_samples does. Dead-sensor records bypass the PID in
    production and are skipped. Gains, limits and mode are taken from each record, so autotune/option
    changes replay faithfully.
    """
    module = module or load_prod_module("pid")
    pid = None
    rows: list[PidReplayRow] = []
    for rec in records:
        if rec.get("kind") != "pid" or rec.get("zone") != zone:
            continue
        d = rec["data"]
        if d.get("dead_sensor") or "kp" not in d:
            continue
        if d.get("last_input_time") is None:
            if pid is None:
                pid = module.PID(d["kp"], d["ki"], d["kd"], out_min=d["out_min"], out_max=d["out_max"],
                                 sampling_period=0,
                                 derivative_window=d.get("d_window", module.PID.DEFAULT_DERIVATIVE_WINDOW),
                                 derivative_limit=d.get("d_limit", module.PID.MAX_DERIVATIVE))
            else:
                pid.clear_samples()
                pid.integral = 0.0
        if pid is None:
            continue
        pid.set_pid_param(kp=d["kp"], ki=d["ki"], kd=d["kd"])
        pid.out_min, pid.out_max = d["out_min"], d["out_max"]
        pid.mode = d.get("mode", "AUTO")
        pid.set_derivative_params(window=d.get("d_window"), limit=d.get("d_limit"))
        out, updated = pid.calc(d["input"], d["set_point"], input_time=d["mono"], last_input_time=d["last_input_time"])
        rows.append(PidReplayRow(rec["seq"], rec["mono"], d, out, updated, pid.proportional, pid.integral,
                                 pid.derivative, pid.error, pid.dt))
    return rows
