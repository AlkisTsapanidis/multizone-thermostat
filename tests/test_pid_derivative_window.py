"""Least-squares windowed derivative for the PID, and its configuration."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.multizone_thermostat.pid import PID
from custom_components.multizone_thermostat.pid_wrapper import MultizonePID


def _feed(pid, temps, step=60.0, start=1000.0, setpoint=15.0):
    """Feed samples `step` seconds apart; return the last output."""
    t = start
    last_t = None
    out = 0.0
    for temp in temps:
        out, _ = pid.calc(temp, setpoint, input_time=t, last_input_time=last_t)
        last_t = t
        t += step
    return out


def _pid(kd=30000.0, **kw):
    return PID(100.0, 0.0, kd, out_min=0.0, out_max=100.0, sampling_period=0, **kw)


# ----------------------------------------------------------------- slope maths

def test_derivative_equals_minus_kd_times_least_squares_slope():
    pid = _pid(kd=30000.0, derivative_limit=100.0)
    # +0.01 C/min for 15 samples = 1/6 mC/s
    _feed(pid, [18.0 + 0.01 * i for i in range(15)])
    slope = 0.01 / 60.0
    assert pid.derivative == pytest.approx(-30000.0 * slope, rel=1e-6)


def test_falling_temperature_gives_positive_derivative():
    pid = _pid(kd=30000.0, derivative_limit=100.0)
    _feed(pid, [18.0 - 0.01 * i for i in range(15)])
    assert pid.derivative > 0


def test_quantised_flicker_gives_near_zero_derivative():
    pid = _pid(kd=100000.0)
    # 18.3 / 18.2 alternating once a minute for 15 minutes
    out = _feed(pid, [18.3, 18.2] * 8)
    # Least squares leaves a small edge residual (~2%) versus +/-100% for a raw difference
    assert abs(pid.derivative) < 3.0
    assert out == 0.0  # error is -3.3 C: must not demand heat


def test_single_step_inside_window_is_smoothed():
    pid = _pid(kd=60000.0)
    out = _feed(pid, [18.3] * 14 + [18.2])
    assert out == 0.0
    assert abs(pid.derivative) < PID.MAX_DERIVATIVE


# --------------------------------------------------------- history requirements

def test_no_derivative_with_fewer_than_three_samples():
    pid = _pid()
    _feed(pid, [18.0, 17.0], step=600.0)
    assert pid.derivative == 0.0


def test_no_derivative_when_span_too_short():
    pid = _pid()  # window 900 s -> needs 300 s of history
    _feed(pid, [18.0, 17.9, 17.8, 17.7], step=30.0)
    assert pid.derivative == 0.0


def test_old_samples_leave_the_window():
    pid = _pid(derivative_window=600.0)
    _feed(pid, [18.0 + 0.05 * i for i in range(10)], step=60.0)  # rising
    assert pid.derivative != 0.0
    # then 15 minutes flat: the rise must be forgotten
    _feed(pid, [18.45] * 16, step=60.0, start=1000.0 + 10 * 60.0)
    assert pid.derivative == pytest.approx(0.0, abs=1e-9)


def test_clear_samples_drops_history():
    pid = _pid(derivative_limit=100.0)
    _feed(pid, [18.0 + 0.01 * i for i in range(15)])
    assert pid.derivative != 0.0
    pid.clear_samples()
    pid.calc(18.2, 15.0, input_time=5000.0, last_input_time=None)
    assert pid.derivative == 0.0


# ----------------------------------------------------------------- configuration

def test_defaults_are_15_minutes_and_15_percent():
    pid = _pid()
    assert pid.derivative_window == 900.0
    assert pid.derivative_limit == 15.0


def test_limit_clamps_derivative():
    pid = _pid(kd=1e7, derivative_limit=10.0)
    _feed(pid, [18.0 + 0.1 * i for i in range(15)])
    assert pid.derivative == -10.0


def test_window_zero_disables_derivative():
    pid = _pid(derivative_window=0.0, derivative_limit=100.0)
    _feed(pid, [18.0 + 0.1 * i for i in range(15)])
    assert pid.derivative == 0.0


def test_limit_zero_disables_derivative():
    pid = _pid(derivative_limit=0.0)
    _feed(pid, [18.0 + 0.1 * i for i in range(15)])
    assert pid.derivative == 0.0


def test_runtime_reconfiguration_takes_effect():
    pid = _pid(kd=1e7, derivative_window=0.0)
    _feed(pid, [18.0 + 0.1 * i for i in range(15)])
    assert pid.derivative == 0.0
    pid.set_derivative_params(window=900.0, limit=5.0)
    _feed(pid, [20.0 + 0.1 * i for i in range(15)], start=5000.0)
    assert pid.derivative == -5.0


def test_invalid_values_are_sanitised():
    pid = _pid()
    pid.set_derivative_params(window=-5, limit=-1)
    assert pid.derivative_window == 0.0
    assert pid.derivative_limit == 0.0
    pid.set_derivative_params(window="x", limit=None)  # ignored
    assert pid.derivative_window == 0.0


def test_pid_wrapper_passes_configuration_through():
    w = MultizonePID(100.0, 0.0, 1e7, derivative_window=300.0, derivative_limit=7.0)
    assert w._pid.derivative_window == 300.0
    assert w._pid.derivative_limit == 7.0
    w.set_derivative_params(window=600.0, limit=9.0)
    assert w._pid.derivative_window == 600.0
    assert w._pid.derivative_limit == 9.0


# --------------------------------------------------- coordinator / zone controller

def _coordinator_with_pids(n=2):
    from custom_components.multizone_thermostat.coordinator import MultizoneCoordinator

    coord = object.__new__(MultizoneCoordinator)
    coord.zone_controller = MagicMock()
    coord.zone_controller.pids = {f"climate.z{i}": MultizonePID(100.0, 0.0, 1e7) for i in range(n)}
    return coord


def test_coordinator_setters_update_every_zone_pid():
    coord = _coordinator_with_pids()
    coord.set_derivative_window(5)      # minutes
    coord.set_derivative_limit(8)       # percent
    for p in coord.zone_controller.pids.values():
        assert p._pid.derivative_window == 300.0
        assert p._pid.derivative_limit == 8.0
    assert coord._derivative_window_min == 5
    assert coord._derivative_limit_pct == 8


def test_zone_controller_applies_configured_values():
    from custom_components.multizone_thermostat.zone_controller import ZoneController

    coordinator = MagicMock()
    coordinator.zones = [{"name": "Kitchen"}]
    coordinator._derivative_window_min = 5
    coordinator._derivative_limit_pct = 10
    zc = ZoneController(MagicMock(), coordinator)
    pid = next(iter(zc.pids.values()))._pid
    assert pid.derivative_window == 300.0
    assert pid.derivative_limit == 10.0


def test_zone_controller_falls_back_to_defaults_for_non_numeric_config():
    from custom_components.multizone_thermostat.zone_controller import ZoneController

    coordinator = MagicMock()  # attributes are MagicMocks, not numbers
    coordinator.zones = [{"name": "Kitchen"}]
    zc = ZoneController(MagicMock(), coordinator)
    pid = next(iter(zc.pids.values()))._pid
    assert pid.derivative_window == 900.0
    assert pid.derivative_limit == 15.0


def test_number_entities_route_to_coordinator():
    from custom_components.multizone_thermostat.number import MultizoneProtectionNumber
    from custom_components.multizone_thermostat.const import (
        CONF_DERIVATIVE_LIMIT,
        CONF_DERIVATIVE_WINDOW,
    )

    coordinator = MagicMock()
    ent = object.__new__(MultizoneProtectionNumber)
    ent._coordinator = coordinator

    ent._key = CONF_DERIVATIVE_WINDOW
    ent._update_coordinator(7)
    coordinator.set_derivative_window.assert_called_once_with(7)

    ent._key = CONF_DERIVATIVE_LIMIT
    ent._update_coordinator(12)
    coordinator.set_derivative_limit.assert_called_once_with(12)
