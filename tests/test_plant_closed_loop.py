"""Closed-loop plant tests: real valve PWM + real boiler relay logic on a manual clock."""
import pytest

from mzt_replay.plant_sim import run_plant

ONE = {"A": "switch.va"}


@pytest.mark.parametrize("demand", [40, 60])
def test_mid_demand_never_fires_into_closed_valves(demand):
    r = run_plant(lambda t, z: demand, ONE, hours=3)
    assert r.boiler_into_closed_s == 0
    assert r.boiler_on_s > 0


def test_full_demand_is_continuous_with_no_clicking():
    r = run_plant(lambda t, z: 100, ONE, hours=3)
    assert r.boiler_transitions == 1
    assert r.valve_transitions["A"] == 1


def test_zero_demand_never_fires_boiler():
    r = run_plant(lambda t, z: 0, ONE, hours=2)
    assert r.boiler_on_s == 0 and r.boiler_commands == 0


@pytest.mark.known_defect("PLANT-001", "boiler min-on (5 min) exceeds the valve open time at low demand, so the boiler fires into a closed valve")
@pytest.mark.parametrize("demand", [10, 25])
def test_low_demand_boiler_does_not_fire_into_closed_valves(demand):
    r = run_plant(lambda t, z: demand, ONE, hours=6)
    assert r.into_closed_fraction < 0.10


@pytest.mark.known_defect("PLANT-002", "boiler PWM and valve PWM are unsynchronised: boiler stays on after valve closes at high demand")
def test_high_demand_boiler_does_not_fire_into_closed_valves():
    r = run_plant(lambda t, z: 80, ONE, hours=6)
    assert r.into_closed_fraction < 0.10


@pytest.mark.known_defect("PLANT-003", "slow valve actuation (90 s) leaves the boiler firing while the valve is still closed/closing")
def test_slow_valve_actuation_does_not_fire_into_closed_valve():
    r = run_plant(lambda t, z: 40, ONE, hours=6, valve_latency=90)
    assert r.into_closed_fraction < 0.05
