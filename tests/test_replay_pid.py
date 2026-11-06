"""PID contract tests via deterministic replay (explicit timestamps, sampling_period=0)."""
import math
import random

import pytest

from mzt_replay.drivers import replay_pid
from mzt_replay.loader import load_prod_module

pid_mod = load_prod_module("pid")


def minute_samples(temps, step=60.0):
    return [(i * step, t) for i, t in enumerate(temps)]


# ---- correct today --------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(5))
def test_output_always_within_limits(seed):
    rng = random.Random(seed)
    temps = [18 + rng.uniform(-3, 6) for _ in range(500)]
    steps = replay_pid(minute_samples(temps), 20.0, kp=40, ki=0.05, kd=5000, out_min=0, out_max=100)
    assert all(0.0 <= s.output <= 100.0 for s in steps)


@pytest.mark.known_defect("PID-005", "NaN input returns _last_output (the output BEFORE the previous step), not the held current output")
def test_nan_input_holds_current_output_and_reports_not_updated():
    pid = pid_mod.PID(kp=10, ki=0, kd=0, out_min=0, out_max=100)
    out, _ = pid.calc(19.0, 20.0, input_time=0.0, last_input_time=None)
    held, updated = pid.calc(math.nan, 20.0, input_time=60.0, last_input_time=0.0)
    assert updated is False and held == out


def test_derivative_is_bounded_by_limit():
    temps = [20 + 0.05 * i for i in range(60)]
    steps = replay_pid(minute_samples(temps), 20.0, kp=0, ki=0, kd=1e9, derivative_limit=15.0)
    assert max(abs(s.d) for s in steps) <= 15.0 + 1e-9


def test_integral_is_clamped_to_output_range():
    steps = replay_pid(minute_samples([10.0] * 2000), 20.0, kp=0, ki=1.0, kd=0, out_min=0, out_max=100)
    assert max(s.i for s in steps) <= 100.0


def test_pure_proportional_response():
    (step,) = replay_pid([(0.0, 19.0)], 20.0, kp=25, ki=0, kd=0)
    assert step.p == pytest.approx(25.0) and step.output == pytest.approx(25.0)


def test_derivative_opposes_a_rising_temperature():
    temps = [18 + 0.02 * i for i in range(60)]
    steps = replay_pid(minute_samples(temps), 20.0, kp=0, ki=0, kd=5000)
    assert steps[-1].d < 0


def test_characterisation_integral_is_discarded_on_large_setpoint_step():
    """Pinned: a >=1.5 degC setpoint change zeroes the integral, throwing away the learned
    steady-state heating power (e.g. every morning/night schedule transition). See PID-003."""
    sp = lambda t: 20.0 if t < 3600 else 22.0
    steps = replay_pid(minute_samples([19.0] * 120), sp, kp=0, ki=0.01, kd=0, out_min=0, out_max=100)
    before = steps[59].i
    after = steps[61].i
    assert before > 10.0 and after < before / 2


# ---- known defects --------------------------------------------------------------------------------

@pytest.mark.known_defect("PID-001", "0.1 degC sensor flicker around a constant temperature produces derivative output")
def test_quantisation_flicker_at_constant_temperature_gives_no_derivative():
    rng = random.Random(7)
    temps = [20.0 + rng.choice((0.0, 0.1)) for _ in range(300)]
    steps = replay_pid(minute_samples(temps), 20.0, kp=0, ki=0, kd=60000)
    assert max(abs(s.d) for s in steps) <= 2.0


@pytest.mark.known_defect("PID-002", "integral slams by dt of the whole sensor outage when readings resume")
def test_sensor_outage_does_not_slam_the_integral():
    regular = [(i * 60.0, 19.0) for i in range(30)]
    resume = [(29 * 60.0 + 4 * 3600.0, 19.0), (29 * 60.0 + 4 * 3600.0 + 60.0, 19.0)]
    steps = replay_pid(regular + resume, 20.0, kp=0, ki=0.005, kd=0, out_min=0, out_max=100)
    normal_step = steps[20].i - steps[19].i
    jump = steps[30].i - steps[29].i
    assert jump <= 3 * normal_step, f"integral jumped {jump:.1f} vs normal step {normal_step:.2f}"
