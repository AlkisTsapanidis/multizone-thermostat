"""PWM engine contract tests, driven by deterministic replay.

Contract under test (from PWMEngine's own docstring): the engine "handles min_cycle_on and
min_cycle_off dilatation", i.e. a relay driven by it must never be switched ON for less than min_on
or OFF for less than min_off. These are the relay-clicking symptoms seen on the real installation.

If the maintainers decide demand==0 / demand>=99 are *meant* to bypass min_on/min_off (e.g. as a
safety stop), amend PWM-001/PWM-002 explicitly rather than deleting them.
"""
import math
import random

import pytest

from mzt_replay.drivers import constant_rate_ticks, replay_pwm, run_lengths

TICK = 10.0
HOURS3 = 3 * 3600


def fuzz_demand(seed: int, hours: float = 6.0):
    """Random demand with sticky levels plus frequent exact 0 / 100 / near-99 excursions."""
    rng = random.Random(seed)
    level, out = 20.0, []
    for i in range(1, int(hours * 3600 / TICK)):
        if rng.random() < 0.08:
            r = rng.random()
            level = 0.0 if r < 0.2 else 100.0 if r < 0.3 else 99.5 if r < 0.4 else rng.uniform(0, 100)
        out.append((i * TICK, level))
    return out


# ---- behaviour that is correct today (guards against regressions) ---------------------------------

def test_zero_demand_never_switches_on():
    ticks = replay_pwm(constant_rate_ticks(lambda t: 0.0, HOURS3), min_on=300, min_off=300)
    assert not any(t.state for t in ticks)


def test_full_demand_stays_on():
    ticks = replay_pwm(constant_rate_ticks(lambda t: 100.0, HOURS3), min_on=300, min_off=300)
    assert all(t.state for t in ticks)


@pytest.mark.parametrize("demand", [20.0, 35.0, 50.0, 65.0, 80.0])
def test_steady_mid_demand_duty_matches_demand(demand):
    ticks = replay_pwm(constant_rate_ticks(lambda t: demand, 24 * 3600), pwm_interval=900, min_on=0, min_off=0)
    duty = sum(t.state for t in ticks) / len(ticks)
    assert duty == pytest.approx(demand / 100.0, abs=0.02)


def test_smooth_wobble_respects_min_times():
    demand = constant_rate_ticks(lambda t: 11 + 3 * math.sin(t / 200.0), HOURS3)
    rl = run_lengths(replay_pwm(demand, min_on=300, min_off=300))
    assert min(rl.on) >= 300 and min(rl.off) >= 300


def test_stepwise_demand_respects_min_times():
    demand = constant_rate_ticks(lambda t: 50.0 if (t // 600) % 2 == 0 else 5.0, HOURS3)
    rl = run_lengths(replay_pwm(demand, min_on=300, min_off=300))
    assert min(rl.on) >= 300 and min(rl.off) >= 300


def test_tiny_demand_duty_is_bounded_by_min_on_inflation():
    """Characterisation: 1% demand cannot be honoured with a 300s min_on; the engine settles on a
    1h cycle (8.3% duty). Pinned so any change to this trade-off is a deliberate decision."""
    ticks = replay_pwm(constant_rate_ticks(lambda t: 1.0, 12 * 3600), pwm_interval=900, min_on=300, min_off=300)
    duty = sum(t.state for t in ticks) / len(ticks)
    assert duty == pytest.approx(300 / 3600, abs=0.01)


# ---- known defects: relay-clicking ----------------------------------------------------------------

@pytest.mark.known_defect("PWM-001", "demand crossing 0 or >=99 bypasses min_off: relay re-closes after seconds")
@pytest.mark.parametrize("seed", range(5))
def test_fuzz_relay_is_never_off_for_less_than_min_off(seed):
    rl = run_lengths(replay_pwm(fuzz_demand(seed), min_on=300, min_off=300))
    assert not rl.off or min(rl.off) >= 300, f"shortest OFF run {min(rl.off)}s < 300s"


@pytest.mark.known_defect("PWM-002", "demand crossing 0 or >=99 bypasses min_on: relay opens after seconds")
@pytest.mark.parametrize("seed", range(5))
def test_fuzz_relay_is_never_on_for_less_than_min_on(seed):
    rl = run_lengths(replay_pwm(fuzz_demand(seed), min_on=300, min_off=300))
    assert not rl.on or min(rl.on) >= 300, f"shortest ON run {min(rl.on)}s < 300s"


@pytest.mark.known_defect("PWM-003", "demand flutter between 0 and a few % clicks the relay every 30s")
def test_demand_flutter_around_zero_does_not_click_relay():
    demand = constant_rate_ticks(lambda t: 2.0 if (t // 30) % 2 == 0 else 0.0, HOURS3)
    transitions = run_lengths(replay_pwm(demand, min_on=300, min_off=300)).transitions
    assert transitions <= HOURS3 / 600, f"{transitions} relay transitions in 3h"


@pytest.mark.known_defect("PWM-004", "demand wobbling across 99% flips ON/OFF immediately")
def test_demand_wobble_across_99_does_not_click_relay():
    demand = constant_rate_ticks(lambda t: 98.0 if (t // 30) % 2 == 0 else 99.5, HOURS3)
    transitions = run_lengths(replay_pwm(demand, min_on=300, min_off=300)).transitions
    assert transitions <= HOURS3 / 600, f"{transitions} relay transitions in 3h"
