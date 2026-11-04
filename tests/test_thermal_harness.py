"""Runs the thermal rate harness on the recorded sample data and on a synthetic flickering series."""
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from thermal_harness.estimators import ESTIMATORS
from thermal_harness.harness import (
    Series, heating_flags, load_series, reference_cooling, replay,
)

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
SAMPLES = sorted(SAMPLE_DIR.glob("*.csv"))

# Estimators known not to meet the accuracy bar yet; remove from here once fixed.
KNOWN_INACCURATE = {"legacy"}


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
