"""Tests for the pluggable rate-estimator architecture behind ThermalObserver."""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.multizone_thermostat import rate_estimators
from custom_components.multizone_thermostat.rate_estimators import (
    DEFAULT_RATE_ESTIMATOR,
    LegacyEmaEstimator,
    RateEstimator,
    available_estimators,
    create_estimator,
    register_estimator,
)
from custom_components.multizone_thermostat.thermal_model import ThermalObserver

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class StubEstimator:
    """Minimal third-party estimator: constant rates, records what it is fed."""

    name = "stub"

    def __init__(self, zone_id="z"):
        self.zone_id = zone_id
        self.heating_rate = 0.0
        self.cooling_rate = 0.0
        self.samples = []
        self.extra = {"n": 0}

    def update(self, ts, temp, is_heating):
        self.samples.append((ts, temp, is_heating))
        self.extra["n"] = len(self.samples)
        self.heating_rate, self.cooling_rate = 1.5, 0.25

    def dump_state(self):
        return dict(self.extra)

    def load_state(self, state):
        self.extra = dict(state)


@pytest.fixture
def stub_registered():
    register_estimator("stub", StubEstimator)
    yield
    rate_estimators._REGISTRY.pop("stub", None)


def test_default_is_legacy_and_satisfies_protocol():
    obs = ThermalObserver("z")
    assert obs.estimator_name == DEFAULT_RATE_ESTIMATOR == "legacy"
    assert isinstance(obs.estimator, LegacyEmaEstimator)
    assert isinstance(obs.estimator, RateEstimator)
    assert "legacy" in available_estimators()


def test_legacy_behaviour_is_unchanged_with_explicit_timestamps():
    obs = ThermalObserver("z")
    obs.update(20.0, False, at(0))
    obs.update(19.9, False, at(10))  # -0.1 °C in 10 min = 0.6 °C/h
    assert obs.cooling_rate == pytest.approx(0.6)
    obs.update(20.0, True, at(20))  # state flip resets the baseline
    obs.update(20.1, True, at(26))  # +0.1 °C in 6 min = 1.0 °C/h
    assert obs.heating_rate == pytest.approx(1.0)


def test_legacy_ignores_sub_36s_steps():
    obs = ThermalObserver("z")
    obs.update(20.0, False, at(0))
    obs.update(19.9, False, at(0.5))
    assert obs.cooling_rate == 0.0


def test_estimator_instance_is_plugged_in_and_fed_timestamps():
    stub = StubEstimator()
    obs = ThermalObserver("z", estimator=stub)
    obs.update(20.0, False, at(0))
    obs.update(20.1, True, at(1))
    assert stub.samples == [(at(0), 20.0, False), (at(1), 20.1, True)]
    assert (obs.heating_rate, obs.cooling_rate) == (1.5, 0.25)
    assert obs.estimator_name == "stub"
    assert obs.get_weather_multiplier() == pytest.approx(0.25 / 1.5)


def test_rate_setters_write_through_to_estimator():
    stub = StubEstimator()
    obs = ThermalObserver("z", estimator=stub)
    obs.heating_rate = 2.0
    obs.cooling_rate = 0.5
    assert (stub.heating_rate, stub.cooling_rate) == (2.0, 0.5)


def test_register_and_select_by_name(stub_registered):
    obs = ThermalObserver("zone1", estimator="stub")
    assert isinstance(obs.estimator, StubEstimator)
    assert obs.estimator.zone_id == "zone1"
    assert "stub" in available_estimators()


def test_unknown_name_falls_back_to_default():
    est = create_estimator("does_not_exist", "z")
    assert isinstance(est, LegacyEmaEstimator)
    assert isinstance(ThermalObserver("z", estimator="does_not_exist").estimator, LegacyEmaEstimator)


def test_inertia_tracking_uses_sample_timestamps():
    obs = ThermalObserver("z")
    obs.update(20.0, True, at(0))
    obs.update(20.5, True, at(5))
    obs.update(20.5, False, at(6))   # heating stops at 20.5
    obs.update(20.8, False, at(12))  # overshoot to 20.8
    obs.update(20.7, False, at(18))  # peak passed (>= 0.1 drop)
    assert obs.thermal_inertia == pytest.approx(0.3)


def test_dump_state_is_backward_compatible_and_tagged():
    obs = ThermalObserver("z")
    obs.heating_rate, obs.cooling_rate, obs.thermal_inertia = 1.2, 0.3, 0.4
    state = obs.dump_state()
    assert state["heating_rate"] == 1.2 and state["cooling_rate"] == 0.3
    assert state["thermal_inertia"] == 0.4 and state["estimator"] == "legacy"
    assert "estimator_state" not in state  # legacy has nothing extra to persist


def test_load_state_from_pre_estimator_storage_restores_legacy():
    obs = ThermalObserver("z")
    obs.load_state({"heating_rate": 1.5, "cooling_rate": 0.3, "thermal_inertia": 0.4})
    assert (obs.heating_rate, obs.cooling_rate, obs.thermal_inertia) == (1.5, 0.3, 0.4)


def test_load_state_from_other_estimator_discards_rates_but_keeps_inertia(stub_registered):
    legacy_state = ThermalObserver("z")
    legacy_state.heating_rate, legacy_state.cooling_rate, legacy_state.thermal_inertia = 3.0, 5.0, 0.4
    saved = legacy_state.dump_state()

    switched = ThermalObserver("z", estimator="stub")
    switched.load_state(saved)
    assert (switched.heating_rate, switched.cooling_rate) == (0.0, 0.0)
    assert switched.thermal_inertia == 0.4


def test_estimator_private_state_round_trips(stub_registered):
    a = ThermalObserver("z", estimator="stub")
    a.update(20.0, False, at(0))
    a.update(20.0, False, at(1))
    saved = a.dump_state()
    assert saved["estimator"] == "stub" and saved["estimator_state"] == {"n": 2}

    b = ThermalObserver("z", estimator="stub")
    b.load_state(saved)
    assert b.estimator.extra == {"n": 2}
    assert (b.heating_rate, b.cooling_rate) == (1.5, 0.25)


def test_estimator_without_persistence_hooks_still_works():
    class Bare:
        heating_rate = 0.0
        cooling_rate = 0.0

        def update(self, ts, temp, is_heating):
            pass

    obs = ThermalObserver("z", estimator=Bare())
    obs.heating_rate = 1.0
    state = obs.dump_state()
    assert "estimator_state" not in state
    obs.load_state(state)
    obs.update(20.0, False)  # default (now) timestamp works
