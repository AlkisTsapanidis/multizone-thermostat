"""Advanced Room Thermal Model (SAT) module."""
import logging
from datetime import datetime, timezone

from .rate_estimators import (
    DEFAULT_RATE_ESTIMATOR,
    LegacyEmaEstimator,
    RateEstimator,
    create_estimator,
    dump_estimator_state,
    load_estimator_state,
    update_ema,
)

_LOGGER = logging.getLogger(__name__)

class ThermalObserver:
    """Observes room temperature to learn thermal capacity, dispersion, and inertia.

    Heating/cooling rates come from a pluggable `RateEstimator` (see rate_estimators.py); inertia
    tracking and the weather multiplier live here.
    """

    def __init__(
        self,
        zone_id: str,
        temp_delta_threshold: float = 0.1,
        estimator: "str | RateEstimator | None" = None,
    ):
        """Initialize the thermal observer.

        `estimator` is a registered estimator name, a ready-made estimator instance, or None for
        the default. `temp_delta_threshold` only applies to the legacy estimator.
        """
        self.zone_id = zone_id
        self.temp_delta_threshold = temp_delta_threshold

        if isinstance(estimator, str) or estimator is None:
            self.estimator_name = estimator or DEFAULT_RATE_ESTIMATOR
            if self.estimator_name == "legacy":
                self.estimator: RateEstimator = LegacyEmaEstimator(zone_id, temp_delta_threshold)
            else:
                self.estimator = create_estimator(self.estimator_name, zone_id)
        else:
            self.estimator = estimator
            self.estimator_name = getattr(estimator, "name", type(estimator).__name__)

        # State tracking
        self._seen_sample = False
        self._is_heating = False

        # Inertia tracking
        self._temp_at_shutdown = None
        self._shutdown_time = None
        self._max_temp_after_shutdown = None

        self.thermal_inertia = 0.0  # °C overshoot

    @property
    def heating_rate(self) -> float:
        """Learned heating rate in °C/hour (0.0 while unknown)."""
        return self.estimator.heating_rate

    @heating_rate.setter
    def heating_rate(self, value: float) -> None:
        self.estimator.heating_rate = value

    @property
    def cooling_rate(self) -> float:
        """Learned cooling rate in °C/hour (0.0 while unknown)."""
        return self.estimator.cooling_rate

    @cooling_rate.setter
    def cooling_rate(self, value: float) -> None:
        self.estimator.cooling_rate = value

    def update(self, current_temp: float, is_heating: bool, timestamp: datetime | None = None):
        """Update the observer with the latest temperature and state.

        `timestamp` is the time the sample was taken (timezone-aware); defaults to now. Passing it
        explicitly makes recorded history replayable.
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        now = timestamp.timestamp()

        self.estimator.update(timestamp, current_temp, is_heating)

        if not self._seen_sample:
            self._seen_sample = True
            self._is_heating = is_heating
            return

        # Track Thermal Inertia when transitioning from HEATING to OFF
        if self._is_heating and not is_heating:
            self._temp_at_shutdown = current_temp
            self._shutdown_time = now
            self._max_temp_after_shutdown = current_temp
            _LOGGER.debug("[%s] Heating stopped. Tracking inertia starting from %.2f°C", self.zone_id, current_temp)
            
        # Update max temp after shutdown (only valid for 60 minutes after shutdown)
        if not is_heating and self._shutdown_time is not None:
            if now - self._shutdown_time <= 3600:
                if current_temp > self._max_temp_after_shutdown:
                    self._max_temp_after_shutdown = current_temp
                elif current_temp <= self._max_temp_after_shutdown - 0.1:
                    # Peak reached (temperature started dropping)
                    inertia = max(0.0, self._max_temp_after_shutdown - self._temp_at_shutdown)
                    self._update_ema('thermal_inertia', inertia, alpha=0.1)
                    _LOGGER.debug("[%s] Inertia peak reached: +%.2f°C", self.zone_id, inertia)
                    self._shutdown_time = None
            else:
                inertia = max(0.0, self._max_temp_after_shutdown - self._temp_at_shutdown)
                self._update_ema('thermal_inertia', inertia, alpha=0.1)
                _LOGGER.debug("[%s] Inertia window closed: +%.2f°C", self.zone_id, inertia)
                self._shutdown_time = None  # Stop tracking inertia after 1 hour

        self._is_heating = is_heating

    def _update_ema(self, attribute: str, new_value: float, alpha: float = 0.1):
        """Update Exponential Moving Average."""
        setattr(self, attribute, update_ema(getattr(self, attribute), new_value, alpha))

    def dump_state(self) -> dict:
        """Dump the learned metrics for storage."""
        state = {
            "heating_rate": self.heating_rate,
            "cooling_rate": self.cooling_rate,
            "thermal_inertia": self.thermal_inertia,
            "estimator": self.estimator_name,
        }
        extra = dump_estimator_state(self.estimator)
        if extra:
            state["estimator_state"] = extra
        return state

    def load_state(self, state: dict):
        """Load learned metrics from storage.

        Rates are only restored when they were produced by the same estimator: values learned by a
        different algorithm are not comparable. Data saved before estimators existed is "legacy".
        """
        self.thermal_inertia = state.get("thermal_inertia", 0.0)
        if state.get("estimator", "legacy") != self.estimator_name:
            _LOGGER.info("[%s] Stored rates came from estimator %r, now using %r; relearning rates",
                         self.zone_id, state.get("estimator", "legacy"), self.estimator_name)
            return
        self.heating_rate = state.get("heating_rate", 0.0)
        self.cooling_rate = state.get("cooling_rate", 0.0)
        load_estimator_state(self.estimator, state.get("estimator_state") or {})

    def get_weather_multiplier(self, manual_base_curve: float = 0.0) -> float:
        """
        Calculate the weather compensation multiplier (Ke) based on thermal metrics.
        If manual_base_curve is provided, it acts as a starting point.
        Otherwise, it calculates an adaptive curve based on cooling/heating ratio.
        """
        if self.heating_rate > 0.1 and self.cooling_rate > 0.01:
            # Dispersion ratio: higher ratio = more dispersion = higher multiplier
            adaptive_curve = (self.cooling_rate / self.heating_rate)
            
            # If a manual base curve was set, we blend it or use it as a base
            if manual_base_curve > 0.0:
                # We let the adaptive curve 'bend' the manual one by +/- 50%
                # Clamping the adaptive part between 0.5 and 1.5
                bend_factor = max(0.5, min(1.5, adaptive_curve))
                return manual_base_curve * bend_factor
            
            return adaptive_curve
            
        # Fallback if model hasn't learned enough yet
        return manual_base_curve if manual_base_curve > 0.0 else 0.5
