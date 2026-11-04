"""Runs the thermal rate harness on the recorded sample data and on a synthetic flickering series."""
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from thermal_harness.estimators import ESTIMATORS
from thermal_harness.harness import (
    Series, actuator_heating, debounce, heating_flags, load_actuators, load_series,
    reference_cooling, replay,
)

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
# The *WithData file repeats the LivingRoom temperature plus actuator entities; it is tested
# separately with the real heating signals.
SAMPLES = sorted(p for p in SAMPLE_DIR.glob("*.csv") if "WithData" not in p.name)
WITH_DATA = SAMPLE_DIR / "LivingRoomWithData.csv"
# The living room zone valve is wired to the entity named after the kitchen's (relays swapped).
HEATING_ENTITIES = ["switch.mqtt_io_kitchen_ufh_valve", "switch.boiler_call_for_heat"]

# Estimators known not to meet the accuracy bar yet; remove from here once fixed.
KNOWN_INACCURATE = {"legacy", "trend"}


def _estimator_params():
    for name in ESTIMATORS:
        marks = []
        if name in KNOWN_INACCURATE:
            marks.append(pytest.mark.xfail(reason="known flicker/baseline-reset bug", strict=False))
        yield pytest.param(name, marks=marks, id=name)


def _flicker_series(rate=0.4, hours=10.0, seed=1) -> Series:
    """Slow linear decline, 0.1 °C quantised, with ±0.1 flicker and irregular sample times."""
    rng = np.random.default_rng(seed)
    times = np.cumsum(rng.uniform(10, 90, int(hours * 3600 / 50)))
    times = times[times < hours * 3600] + 1.8e9
    true = 21.5 - rate * (times - times[0]) / 3600
    noisy = true + rng.choice([-0.1, 0.0, 0.1], size=len(times))
    return Series("synthetic", "sensor.synthetic", times, np.round(noisy, 1))


@pytest.mark.parametrize("name", list(_estimator_params()))
def test_flickering_decline_matches_true_slope(name):
    series = _flicker_series()
    r = replay(ESTIMATORS[name], name, series, np.zeros(len(series.times), dtype=bool))
    assert 0.2 <= r.cooling_final <= 0.8
    assert r.cooling_max <= 1.0


@pytest.mark.parametrize("name", list(_estimator_params()))
@pytest.mark.parametrize("csv_path", SAMPLES, ids=[p.stem for p in SAMPLES])
def test_sample_data_cooling_is_plausible(name, csv_path, capsys):
    series = load_series(csv_path)
    heating = heating_flags(series, None)
    r = replay(ESTIMATORS[name], name, series, heating)
    ref = reference_cooling(series, heating)
    with capsys.disabled():
        print(f"\n{series.name:<13}{name:<8} cool med {r.cooling_median:5.2f} p90 {r.cooling_p90:5.2f} "
              f"max {r.cooling_max:5.2f} | reference {np.median(ref) if ref else 0:.2f} °C/h")
    assert r.cooling_p90 <= 1.0, "learned cooling rate is implausibly high"
    if ref:
        assert r.cooling_median <= 3 * max(ref) + 0.2


def test_harness_loads_samples():
    if not SAMPLES:
        pytest.skip("no sample_data")
    for path in SAMPLES:
        series = load_series(path)
        assert len(series.times) > 100
        assert np.all(np.diff(series.times) >= 0)
        assert 5 < series.temps.mean() < 35


def test_debounce_merges_pwm_and_drops_blips():
    times = np.arange(0.0, 3600.0, 10.0)
    flags = np.zeros(len(times), dtype=bool)
    flags[10:20] = True   # 100 s heating
    flags[22:60] = True   # 20 s gap, merged with the run above
    flags[100:102] = True  # 20 s blip, dropped
    out = debounce(times, flags, gap_s=60, min_s=300)
    assert out[10:60].all() and not out[:10].any() and not out[60:].any()


@pytest.mark.skipif(not WITH_DATA.exists(), reason="no LivingRoomWithData.csv")
@pytest.mark.parametrize("name", list(_estimator_params()))
def test_living_room_with_real_valve_and_boiler_signals(name, capsys):
    full = load_series(WITH_DATA)
    series, heating = actuator_heating(full, load_actuators(WITH_DATA, HEATING_ENTITIES))
    assert series.times[0] > full.times[0], "samples before actuator history must be dropped"
    assert 0.05 < heating.mean() < 0.5
    ref = reference_cooling(series, heating)
    assert ref, "expected idle stretches to build a reference"
    r = replay(ESTIMATORS[name], name, series, heating)
    with capsys.disabled():
        print(f"\nreal-signals {name:<12} cool med {r.cooling_median:5.2f} p90 {r.cooling_p90:5.2f} "
              f"| reference {np.median(ref):.2f} heat med {r.heating_median:.2f} C/h")
    assert r.cooling_p90 <= 1.0
    assert r.cooling_median <= 3 * max(ref) + 0.2
    if r.heating_median:
        assert 0.2 <= r.heating_median <= 4.0


@pytest.mark.parametrize("name", ["rt_kalman", "rt_rls", "b_perperiod"])
def test_harness_estimators_plug_into_real_thermal_observer(name):
    """The production ThermalObserver accepts any harness estimator and reports its rates."""
    from custom_components.multizone_thermostat.thermal_model import ThermalObserver

    series = _flicker_series()
    obs = ThermalObserver("synthetic", estimator=ESTIMATORS[name]("synthetic"))
    for ts, temp in zip(series.times, series.temps):
        obs.update(float(temp), False, datetime.fromtimestamp(float(ts), timezone.utc))
    assert 0.2 <= obs.cooling_rate <= 0.8
    state = obs.dump_state()
    assert state["estimator"] == obs.estimator_name
