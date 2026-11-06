"""Rate-estimator accuracy against synthetic rooms with known ground truth.

Complements tests/test_thermal_harness.py (real recorded data, plausibility bounds only): here the
true rate is known exactly, so the assertions are quantitative.
"""
import pytest

from mzt_replay.drivers import replay_estimator
from mzt_replay.loader import load_prod_module
from mzt_replay.synthetic import simulate_room

re_mod = load_prod_module("rate_estimators")

HOUR = 3600.0
TOLERANCE = 1.5  # estimate must be within x1.5 / /1.5 of truth


def within(estimate, truth, factor=TOLERANCE):
    return truth / factor <= estimate <= truth * factor


def net_rate(run, t0, t1):
    pts = [s for s in run.samples if t0 <= s.t <= t1]
    return (pts[-1].true_temp - pts[0].true_temp) / ((pts[-1].t - pts[0].t) / HOUR)


@pytest.fixture
def legacy():
    return re_mod.LegacyEmaEstimator("synthetic")


# ---- correct today --------------------------------------------------------------------------------

def test_no_data_means_unknown_rates(legacy):
    assert legacy.heating_rate == 0.0 and legacy.cooling_rate == 0.0


def test_rates_are_never_negative_and_other_rate_untouched_by_pure_cooling(legacy):
    run = simulate_room([(8 * HOUR, False)], flicker_prob=0.1, noise_sd=0.05, seed=2)
    trace = replay_estimator(legacy, run.samples)
    assert all(h == 0.0 and c >= 0.0 for _, h, c in trace)


def test_replay_is_deterministic():
    run = simulate_room([(2 * HOUR, True), (6 * HOUR, False)], flicker_prob=0.05, noise_sd=0.03, seed=9)
    a, b = re_mod.LegacyEmaEstimator("a"), re_mod.LegacyEmaEstimator("b")
    assert replay_estimator(a, run.samples) == replay_estimator(b, run.samples)


def test_legacy_cooling_is_correct_on_a_noise_free_quantised_sensor(legacy):
    """Characterisation: the legacy algorithm is only right when the sensor is perfectly clean."""
    run = simulate_room([(12 * HOUR, False)], start_temp=21.0, tau_h=40.0, seed=3)
    replay_estimator(legacy, run.samples)
    assert within(legacy.cooling_rate, run.true_mean_rate(0, 12 * HOUR))


# ---- known defects --------------------------------------------------------------------------------

@pytest.mark.known_defect("EST-001", "LegacyEma overestimates cooling ~10-20x once the quantised sensor has any noise/flicker")
@pytest.mark.parametrize("flicker,noise", [(0.0, 0.03), (0.05, 0.03), (0.15, 0.05)])
def test_legacy_cooling_rate_matches_truth(legacy, flicker, noise):
    run = simulate_room([(12 * HOUR, False)], start_temp=21.0, tau_h=40.0, flicker_prob=flicker, noise_sd=noise, seed=3)
    replay_estimator(legacy, run.samples)
    truth = run.true_mean_rate(0, 12 * HOUR)
    assert within(legacy.cooling_rate, truth), f"cooling {legacy.cooling_rate:.2f} vs truth {truth:.2f} degC/h"


@pytest.mark.known_defect("EST-002", "LegacyEma cooling estimate is not stable: it never settles near any value")
def test_legacy_cooling_estimate_is_stable_late_in_the_run(legacy):
    run = simulate_room([(24 * HOUR, False)], start_temp=21.0, tau_h=40.0, flicker_prob=0.05, noise_sd=0.03, seed=4)
    trace = replay_estimator(legacy, run.samples)
    late = [c for t, _, c in trace if t > 12 * HOUR]
    assert max(late) <= 1.5 * min(late), f"late estimates span {min(late):.2f}..{max(late):.2f}"


@pytest.mark.known_defect("EST-003", "LegacyEma heating rate ignores sensor lag/quantisation and is far from the true net rate")
def test_legacy_heating_rate_matches_truth(legacy):
    run = simulate_room([(3 * HOUR, True)], start_temp=17.0, heat_rate_c_per_h=2.0, tau_h=40.0,
                        flicker_prob=0.05, noise_sd=0.03, seed=5)
    replay_estimator(legacy, run.samples)
    truth = net_rate(run, 0, 3 * HOUR)
    assert within(legacy.heating_rate, truth), f"heating {legacy.heating_rate:.2f} vs truth {truth:.2f} degC/h"


@pytest.mark.known_defect("EST-004", "after PWM-style short heat bursts the cooling estimate is polluted by baseline resets")
def test_legacy_cooling_survives_pwm_style_heating_bursts(legacy):
    schedule = [(2 * HOUR, True)] + [(300.0, False), (300.0, True)] * 6 + [(10 * HOUR, False)]
    run = simulate_room(schedule, start_temp=19.0, tau_h=40.0, flicker_prob=0.05, noise_sd=0.03, seed=6)
    replay_estimator(legacy, run.samples)
    t_end = sum(d for d, _ in schedule)
    truth = run.true_mean_rate(t_end - 10 * HOUR, t_end)
    assert within(legacy.cooling_rate, truth), f"cooling {legacy.cooling_rate:.2f} vs truth {truth:.2f} degC/h"
