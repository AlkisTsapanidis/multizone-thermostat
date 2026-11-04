"""Estimator adapters. Add new numerical code here and register it in ESTIMATORS.

Every estimator follows the integration's contract (custom_components/multizone_thermostat/
rate_estimators.py): update(ts: datetime, temp, is_heating), heating_rate, cooling_rate. Anything
registered here can therefore be plugged into the real ThermalObserver unchanged.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

_RATE_ESTIMATORS_PATH = (Path(__file__).resolve().parents[2]
                         / "custom_components" / "multizone_thermostat" / "rate_estimators.py")


def _load_rate_estimators():
    # Load by path: the package __init__ imports Home Assistant, which the harness does not need.
    # rate_estimators.py has no intra-package imports, so this runs the real production class.
    spec = importlib.util.spec_from_file_location("_prod_rate_estimators", _RATE_ESTIMATORS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LegacyEstimator = _load_rate_estimators().LegacyEmaEstimator


def extract_true_thermal_rates(timestamps, values):
    """
    timestamps: list/array of epoch seconds from sensor.last_changed
    values: list/array of raw quantised temperatures (e.g., 18.1, 18.1, 18.2)
    """
    import numpy as np
    from derivative import dxdt

    t = np.array(timestamps, dtype=float)
    x = np.array(values, dtype=float)

    if len(t) < 10:
        return 0.0, 0.0  # Not enough history to process safely

    # Total Variation Regularisation (Trend Filtering)
    # alpha penalises wiggles. For 0.1°C quantisation, 1e-2 to 5e-2 completely
    # flattens out 1-minute flickers while letting 30-minute ramps slide through.
    try:
        raw_derivatives = dxdt(x, t, kind="trend_filtered", order=0, alpha=3e-2)
    except Exception:
        # Robust fallback: Spline smoothing with an aggressive smoothing factor
        # to ensure we don't crash if the optimiser fails to converge on erratic data
        raw_derivatives = dxdt(x, t, kind="spline", s=len(x) * 0.5, order=2, d=1)

    # Convert from °C/second to °C/hour
    rates_per_hour = raw_derivatives * 3600

    # DO NOT use absolute max/min. Use robust percentiles to strip out
    # any edge artifacts or remaining telemetry glitches.
    true_max_heating = np.percentile(rates_per_hour, 99.0)
    true_max_cooling = np.percentile(rates_per_hour, 1.0)  # Will be negative

    # Bound them logically
    heating_rate = max(0.0, true_max_heating)
    cooling_rate = abs(min(0.0, true_max_cooling))

    return heating_rate, cooling_rate


class TrendFilteredEstimator:
    """Streaming wrapper around the batch extract_true_thermal_rates().

    Keeps a trailing window of raw samples and re-runs the batch function at most once per
    `refit_s` of sensor time (a refit per sample would be needlessly slow). The function ignores
    is_heating, so the flag is unused.
    """

    window_s = 12 * 3600
    refit_s = 6 * 3600

    def __init__(self, zone_id: str):
        self.zone_id = zone_id
        self._t: list[float] = []
        self._x: list[float] = []
        self._last_fit: float | None = None
        self.heating_rate = 0.0
        self.cooling_rate = 0.0

    def update(self, ts: datetime, temp: float, is_heating: bool) -> None:
        now = ts.timestamp()
        self._t.append(now)
        self._x.append(temp)
        cutoff = now - self.window_s
        drop = 0
        while self._t[drop] < cutoff:
            drop += 1
        if drop:
            del self._t[:drop], self._x[:drop]
        if self._last_fit is None or now - self._last_fit >= self.refit_s:
            self._last_fit = now
            self.heating_rate, self.cooling_rate = extract_true_thermal_rates(self._t, self._x)



from .solvers import (  # noqa: E402
    GaussianProcessEstimator, HampelTheilSenEstimator, KalmanEstimator, PerPeriodLSEstimator,
    PhysicalKalmanEstimator, RLSEstimator, RTSSmootherEstimator, SavGolEstimator,
    SmoothingSplineEstimator, TheilSenWindowEstimator, TrendIdleEstimator,
)

# Real-time (per-sample) solvers are marked rt_, batch (windowed, periodic) solvers are marked b_.
ESTIMATORS = {
    "legacy": LegacyEstimator,
    "trend": TrendFilteredEstimator,
    "trend_idle": TrendIdleEstimator,
    "rt_kalman": KalmanEstimator,
    "rt_physical": PhysicalKalmanEstimator,
    "rt_rls": RLSEstimator,
    "rt_theilsen": TheilSenWindowEstimator,
    "b_savgol": SavGolEstimator,
    "b_spline": SmoothingSplineEstimator,
    "b_perperiod": PerPeriodLSEstimator,
    "b_hampel_ts": HampelTheilSenEstimator,
    "b_gp": GaussianProcessEstimator,
    "b_rts": RTSSmootherEstimator,
}
