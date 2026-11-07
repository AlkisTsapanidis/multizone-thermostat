"""PID: production log -> replay exactness, and differential tests against the frozen original.

The frozen copy (tests/mzt_replay/reference/pid_v0.py) is the pre-fix behaviour. Any refactor of pid.py
must keep `test_differential_*` green; a deliberate fix may only change results outside the domains
those tests cover (NaN input, outage-sized dt, noise-driven derivative), which is what the
known_defect tests in test_replay_pid.py describe.
"""
import math
import random

import pytest

from custom_components.multizone_thermostat import pid_wrapper
from mzt_replay.clock import ManualClock, patched_time
from mzt_replay.drivers import replay_pid, replay_pid_from_records
from mzt_replay.loader import load_prod_module, load_reference_module

telemetry = load_prod_module("telemetry")
REF = load_reference_module("pid_v0")
PROD = load_prod_module("pid")
ZONE = "climate.test"


def run_production(tmp_path, seed, steps=400):
    """Drive the real MultizonePID like the zone controller does, logging every calc."""
    rng = random.Random(seed)
    clock = ManualClock(start=5000.0)
    recorder = telemetry.JsonlRecorder(str(tmp_path), wall_clock=clock.time, mono_clock=clock.monotonic)
    with patched_time(pid_wrapper, clock):
        mp = pid_wrapper.MultizonePID(kp=rng.uniform(5, 40), ki=rng.uniform(0, 0.05), kd=rng.uniform(0, 400),
                                      sensor_timeout=1800.0)
        temp, sp = 19.0, 21.0
        for _ in range(steps):
            roll = rng.random()
            if roll < 0.05:
                clock.advance(rng.uniform(1900, 4000))            # long outage -> dead sensor path
            elif roll < 0.10:
                clock.advance(rng.uniform(300, 1500))             # shorter outage, sensor still alive
                mp.update_sensor_timestamp()
            else:
                clock.advance(rng.choice([10, 30, 60, 60, 60, 120]))
                mp.update_sensor_timestamp()
            if roll > 0.97:
                mp.clear_samples()
            if roll > 0.95:
                sp = rng.choice([17.0, 19.5, 21.0, 22.5])
            if 0.5 < roll < 0.53:
                mp.set_pid_param(kp=rng.uniform(5, 40), ki=rng.uniform(0, 0.05), kd=rng.uniform(0, 400))
            temp += rng.gauss(0.02, 0.15)
            reading = math.nan if 0.4 < roll < 0.42 else round(temp, 1)
            demand = mp.calc(reading, sp)
            recorder.record("pid", ZONE, **mp.last_calc)
    recorder.flush()
    return sorted(str(p) for p in tmp_path.glob("*.jsonl"))


@pytest.mark.parametrize("seed", range(8))
def test_production_pid_log_replays_exactly(tmp_path, seed):
    paths = run_production(tmp_path, seed)
    records = list(telemetry.read_records(paths))
    rows = replay_pid_from_records(records, ZONE)
    assert len(rows) > 200, "scenario should produce plenty of live (non dead-sensor) records"
    for row in rows:
        d = row.logged
        assert row.out == d["demand"], f"seq {row.seq}: demand {row.out} != logged {d['demand']}"
        assert row.p == d["p"] and row.i == d["i"] and row.d == d["d"], f"seq {row.seq}: terms diverged"
        assert row.updated == d["updated"]


def test_nan_inputs_are_logged_and_replayed_as_nan(tmp_path):
    clock = ManualClock(start=100.0)
    recorder = telemetry.JsonlRecorder(str(tmp_path), wall_clock=clock.time, mono_clock=clock.monotonic)
    with patched_time(pid_wrapper, clock):
        mp = pid_wrapper.MultizonePID(kp=10, ki=0.01, kd=0)
        for temp in (19.0, 19.2, math.nan, 19.4):
            clock.advance(60)
            mp.update_sensor_timestamp()
            demand = mp.calc(temp, 21.0)
            recorder.record("pid", ZONE, **mp.last_calc)
    recorder.flush()
    recs = list(telemetry.read_records(sorted(str(p) for p in tmp_path.glob("*.jsonl"))))
    inputs = [r["data"]["input"] for r in recs]
    assert math.isnan(inputs[2]) and not math.isnan(inputs[3])
    assert [r.out for r in replay_pid_from_records(recs, ZONE)] == [r["data"]["demand"] for r in recs]


def test_replay_skips_records_until_a_clean_state_boundary(tmp_path):
    """A log that starts mid-run cannot be replayed exactly; we must not pretend otherwise."""
    paths = run_production(tmp_path, seed=3, steps=60)
    recs = [r for r in telemetry.read_records(paths) if not r["data"].get("dead_sensor")]
    mid = recs[5:]
    first_clean = next(i for i, r in enumerate(mid) if r["data"]["last_input_time"] is None)
    rows = replay_pid_from_records(mid, ZONE)
    assert rows[0].seq == mid[first_clean]["seq"]


def test_logged_records_carry_everything_needed_to_replay():
    clock = ManualClock(start=0.0)
    with patched_time(pid_wrapper, clock):
        mp = pid_wrapper.MultizonePID(kp=10, ki=0.01, kd=5)
        clock.advance(60)
        mp.calc(19.0, 21.0)
    needed = {"input", "set_point", "mono", "last_input_time", "demand", "p", "i", "d", "kp", "ki", "kd",
              "out_min", "out_max", "mode", "d_window", "d_limit", "dt", "error", "updated"}
    assert needed <= mp.last_calc.keys()


# ---------------- differential tests: production vs frozen original ----------------

def _steps(module, samples, sp, **kw):
    return [(s.output, s.p, s.i, s.d, s.updated) for s in replay_pid(samples, sp, module=module, **kw)]


@pytest.mark.parametrize("seed", range(10))
def test_differential_proportional_integral_noisy_no_derivative(seed):
    """Kd=0 and sane dt: the only region where noise, NaN and outage fixes must not change anything."""
    rng = random.Random(seed)
    t, temp, samples = 0.0, 19.0, []
    for _ in range(600):
        t += rng.choice([10, 30, 60, 60, 120])
        temp += rng.gauss(0.0, 0.2)
        samples.append((t, round(temp, 1)))
    sp = lambda tt: 21.0 if (tt // 4000) % 2 == 0 else 18.5  # noqa: E731 - crosses the >=1.5 reset rule
    kw = dict(kp=rng.uniform(5, 40), ki=rng.uniform(0.001, 0.05), kd=0.0)
    assert _steps(REF, samples, sp, **kw) == _steps(PROD, samples, sp, **kw)


@pytest.mark.parametrize("seed", range(10))
def test_differential_smooth_signal_with_derivative(seed):
    """Smooth (noise-free) temperature: derivative-slope behaviour must be identical."""
    rng = random.Random(100 + seed)
    kw = dict(kp=rng.uniform(5, 40), ki=rng.uniform(0.001, 0.05), kd=rng.uniform(50, 500),
              derivative_window=rng.choice([300, 900, 1800]), derivative_limit=rng.choice([5.0, 15.0, 40.0]))
    samples = [(60.0 * i, 18.0 + 0.004 * 60 * i / 60 + 1.5 * math.sin(i / 40)) for i in range(500)]
    assert _steps(REF, samples, 21.0, **kw) == _steps(PROD, samples, 21.0, **kw)


@pytest.mark.parametrize("mode", ["AUTO", "OFF"])
def test_differential_off_mode_bang_bang(mode):
    ref = REF.PID(10, 0.01, 0, out_min=0, out_max=100)
    new = PROD.PID(10, 0.01, 0, out_min=0, out_max=100)
    ref.mode = new.mode = mode
    rng, last = random.Random(7), None
    t, temp = 0.0, 20.0
    for _ in range(300):
        t += 60
        temp += rng.gauss(0, 0.4)
        a = ref.calc(temp, 20.0, input_time=t, last_input_time=last)
        b = new.calc(temp, 20.0, input_time=t, last_input_time=last)
        last = t
        assert a == b


def test_differential_harness_detects_a_behaviour_change(monkeypatch, tmp_path):
    """Guard against vacuous differential/replay tests: a tiny perturbation must be caught."""
    samples = [(60.0 * i, 18.0 + 1.5 * math.sin(i / 40)) for i in range(300)]
    kw = dict(kp=20, ki=0.01, kd=200)
    baseline = _steps(PROD, samples, 21.0, **kw)
    orig = PROD.PID._temperature_slope
    monkeypatch.setattr(PROD.PID, "_temperature_slope",
                        lambda self, now: (lambda s: None if s is None else s * 1.001)(orig(self, now)))
    assert _steps(PROD, samples, 21.0, **kw) != baseline
    # a log recorded before the change no longer replays exactly after it
    monkeypatch.setattr(PROD.PID, "_temperature_slope", orig)
    records = list(telemetry.read_records(run_production(tmp_path, seed=1)))
    monkeypatch.setattr(PROD.PID, "_temperature_slope",
                        lambda self, now: (lambda s: None if s is None else s * 1.001)(orig(self, now)))
    rows = replay_pid_from_records(records, ZONE)
    assert any(r.d != r.logged["d"] for r in rows)


def test_differential_set_pid_param_and_clear_samples_midstream():
    ref = REF.PID(10, 0.01, 0, out_min=0, out_max=100)
    new = PROD.PID(10, 0.01, 0, out_min=0, out_max=100)
    last = None
    for i in range(1, 200):
        t = 60.0 * i
        if i == 50:
            ref.set_pid_param(kp=25, ki=0.02, kd=100)
            new.set_pid_param(kp=25, ki=0.02, kd=100)
        if i == 120:
            for p in (ref, new):
                p.clear_samples()
                p.integral = 0.0
            last = None
        temp = 19 + 0.01 * i
        assert ref.calc(temp, 21.0, input_time=t, last_input_time=last) == \
            new.calc(temp, 21.0, input_time=t, last_input_time=last)
        assert (ref.integral, ref.derivative, ref.proportional) == (new.integral, new.derivative, new.proportional)
        last = t
