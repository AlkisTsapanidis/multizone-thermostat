"""Pluggable heating/cooling rate estimators for the thermal model.

An estimator learns how fast a room heats (while the zone is calling for heat) and cools (while
idle) from the stream of temperature samples. `ThermalObserver` owns one estimator per zone and
exposes its results; the rest of the integration never talks to an estimator directly.

Estimator contract (duck-typed, see `RateEstimator`):

    update(ts: datetime, temp: float, is_heating: bool) -> None
    heating_rate: float    °C/h, >= 0, 0.0 while unknown (writable so stored values can be restored)
    cooling_rate: float    °C/h, >= 0, 0.0 while unknown (writable)
    dump_state() -> dict   optional, extra JSON-serialisable state to persist
    load_state(dict)       optional

`ts` is a timezone-aware datetime and is the only clock an estimator may use, so recorded history
can be replayed through it deterministically (see tests/thermal_harness).

Register additional estimators with `register_estimator(name, factory)`; select one per
installation with the `rate_estimator` option (default: `DEFAULT_RATE_ESTIMATOR`).
"""
from __future__ import annotations

import importlib.util
import logging
from datetime import datetime
from typing import Any, Callable, Protocol, runtime_checkable

_LOGGER = logging.getLogger(__name__)

DEFAULT_RATE_ESTIMATOR = "legacy"


@runtime_checkable
class RateEstimator(Protocol):
    """Interface every rate estimator implements."""

    heating_rate: float
    cooling_rate: float

    def update(self, ts: datetime, temp: float, is_heating: bool) -> None:
        """Feed one temperature sample taken at `ts` while the zone was (not) heating."""


def update_ema(current: float, new_value: float, alpha: float = 0.1) -> float:
    """Exponential moving average that seeds itself with the first value."""
    if current == 0.0:
        return new_value
    return alpha * new_value + (1.0 - alpha) * current


class LegacyEmaEstimator:
    """Original algorithm: EMA of two-point rates between consecutive >= threshold temperature steps.

    Known to overestimate by an order of magnitude on quantised, flickering sensors (see
    CoolingRateImprovements.md); kept as the default until a replacement is validated.
    """

    def __init__(self, zone_id: str, temp_delta_threshold: float = 0.1):
        self.zone_id = zone_id
        self.temp_delta_threshold = temp_delta_threshold
        self.heating_rate = 0.0  # °C / hour
        self.cooling_rate = 0.0  # °C / hour
        self._last_temp: float | None = None
        self._last_temp_time: float | None = None
        self._is_heating = False

    def update(self, ts: datetime, temp: float, is_heating: bool) -> None:
        now = ts.timestamp()

        if self._last_temp is None:
            self._last_temp = temp
            self._last_temp_time = now
            self._is_heating = is_heating
            return

        # Reset the baseline when switching between heating and cooling so durations are not mixed
        if self._is_heating != is_heating:
            self._last_temp = temp
            self._last_temp_time = now
            self._is_heating = is_heating
            return

        delta_temp = temp - self._last_temp
        if abs(delta_temp) >= self.temp_delta_threshold:
            elapsed_hours = (now - self._last_temp_time) / 3600.0

            if elapsed_hours > 0.01:  # Ignore super rapid spikes (< 36 seconds)
                rate = abs(delta_temp) / elapsed_hours
                if is_heating and delta_temp > 0:
                    self.heating_rate = update_ema(self.heating_rate, rate)
                    _LOGGER.debug("[%s] Heating rate updated: %.2f °C/hr", self.zone_id, self.heating_rate)
                elif not is_heating and delta_temp < 0:
                    self.cooling_rate = update_ema(self.cooling_rate, rate)
                    _LOGGER.debug("[%s] Cooling rate updated: %.2f °C/hr", self.zone_id, self.cooling_rate)

            self._last_temp = temp
            self._last_temp_time = now

        self._is_heating = is_heating


EstimatorFactory = Callable[[str], RateEstimator]

_REGISTRY: dict[str, EstimatorFactory] = {"legacy": LegacyEmaEstimator}


def register_estimator(name: str, factory: EstimatorFactory) -> None:
    """Make an estimator selectable by name. `factory(zone_id)` must return a RateEstimator."""
    _REGISTRY[name] = factory


def _solver_factory(name: str, requires: tuple[str, ...]) -> EstimatorFactory:
    """Lazy factory: imports rate_solvers on first use and falls back if a dependency is missing."""

    def factory(zone_id: str) -> RateEstimator:
        missing = [m for m in requires if importlib.util.find_spec(m) is None]
        if missing:
            _LOGGER.warning("Rate estimator %r needs %s, which is not installed; using %r for %s",
                            name, ", ".join(missing), DEFAULT_RATE_ESTIMATOR, zone_id)
            return _REGISTRY[DEFAULT_RATE_ESTIMATOR](zone_id)
        from . import rate_solvers
        return rate_solvers.SOLVERS[name][0](zone_id)

    return factory


# Kept in sync with rate_solvers.SOLVERS (checked by tests); listed here so that importing this
# module never pulls in numpy.
_SOLVER_REQUIRES = {
    "trend": ("numpy", "derivative"),
    "trend_idle": ("numpy", "derivative"),
    "rt_kalman": ("numpy",),
    "rt_physical": ("numpy",),
    "rt_rls": ("numpy",),
    "rt_theilsen": ("numpy",),
    "b_savgol": ("numpy", "scipy"),
    "b_spline": ("numpy", "scipy"),
    "b_perperiod": ("numpy",),
    "b_hampel_ts": ("numpy", "scipy"),
    "b_gp": ("numpy",),
    "b_rts": ("numpy",),
}
for _name, _requires in _SOLVER_REQUIRES.items():
    register_estimator(_name, _solver_factory(_name, _requires))


def available_estimators() -> list[str]:
    return sorted(_REGISTRY)


def create_estimator(name: str | None, zone_id: str) -> RateEstimator:
    """Instantiate the named estimator, falling back to the default if the name is unknown."""
    key = name or DEFAULT_RATE_ESTIMATOR
    factory = _REGISTRY.get(key)
    if factory is None:
        _LOGGER.warning("Unknown rate estimator %r for %s; using %r (available: %s)",
                        key, zone_id, DEFAULT_RATE_ESTIMATOR, ", ".join(available_estimators()))
        factory = _REGISTRY[DEFAULT_RATE_ESTIMATOR]
        key = DEFAULT_RATE_ESTIMATOR
    estimator = factory(zone_id)
    if not hasattr(estimator, "update"):
        raise TypeError(f"Rate estimator {key!r} has no update() method")
    return estimator


def dump_estimator_state(estimator: Any) -> dict:
    dump = getattr(estimator, "dump_state", None)
    return dict(dump()) if callable(dump) else {}


def load_estimator_state(estimator: Any, state: dict) -> None:
    load = getattr(estimator, "load_state", None)
    if callable(load):
        load(state)
