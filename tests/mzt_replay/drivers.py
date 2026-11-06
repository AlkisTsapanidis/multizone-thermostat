"""Drivers that replay inputs through production numerical code and emit telemetry."""
from __future__ import annotations

from dataclasses import dataclass
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
                                prev_demand_before=prev_demand)
    return ticks


def replay_pwm_from_records(records: Iterable[dict], zone: str | None = None,
                            kinds: tuple[str, ...] = ("pwm", "boiler")) -> list[tuple[dict, bool]]:
    """Re-run recorded PWM decisions through the current PWMEngine.

    Handles "pwm" and "boiler" records (both carry demand/state/min_on/min_off/cycle_start_before).
    `zone` selects one engine's records when a log holds several. Returns (record, replayed_state)
    pairs; the caller compares with record["data"]["state"]. Engine state before the first record is
    injected from the log, so replay is exact from there on.
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
            clock.now = rec["mono"]
            out.append((rec, engine.calculate(d["demand"])))
    return out


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


def replay_pid(samples: Iterable[tuple[float, float]], set_point, **pid_kwargs) -> list[PidStep]:
    """Feed (t, temp) to the real PID with explicit timestamps (sampling_period=0), tracing every term.

    `set_point` may be a number or a function of t.
    """
    module = load_prod_module("pid")
    pid = module.PID(**{"kp": 0.0, "ki": 0.0, "kd": 0.0, "out_min": 0.0, "out_max": 100.0, **pid_kwargs})
    steps: list[PidStep] = []
    last_t = None
    for t, temp in samples:
        sp = set_point(t) if callable(set_point) else set_point
        out, updated = pid.calc(temp, sp, input_time=t, last_input_time=last_t)
        last_t = t
        steps.append(PidStep(t, temp, sp, out, updated, pid.proportional, pid.integral, pid.derivative, pid.error))
    return steps
