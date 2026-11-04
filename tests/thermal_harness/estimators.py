"""Estimator adapters. Add new numerical code here and register it in ESTIMATORS.

Every estimator follows the integration's contract (custom_components/multizone_thermostat/
rate_estimators.py): update(ts: datetime, temp, is_heating), heating_rate, cooling_rate. Anything
registered here can therefore be plugged into the real ThermalObserver unchanged.
"""
from __future__ import annotations

import importlib.util
import sys
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


def _load_solvers():
    spec = importlib.util.spec_from_file_location(
        "_prod_rate_solvers", _RATE_ESTIMATORS_PATH.with_name("rate_solvers.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_solvers = _load_solvers()
extract_true_thermal_rates = _solvers.extract_true_thermal_rates
TrendFilteredEstimator = _solvers.TrendFilteredEstimator
GaussianProcessEstimator = _solvers.GaussianProcessEstimator
HampelTheilSenEstimator = _solvers.HampelTheilSenEstimator
KalmanEstimator = _solvers.KalmanEstimator
PerPeriodLSEstimator = _solvers.PerPeriodLSEstimator
PhysicalKalmanEstimator = _solvers.PhysicalKalmanEstimator
RLSEstimator = _solvers.RLSEstimator
RTSSmootherEstimator = _solvers.RTSSmootherEstimator
SavGolEstimator = _solvers.SavGolEstimator
SmoothingSplineEstimator = _solvers.SmoothingSplineEstimator
TheilSenWindowEstimator = _solvers.TheilSenWindowEstimator
TrendIdleEstimator = _solvers.TrendIdleEstimator

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
