# Controller fixes: changes made

Implements the P0/P1 items from [ControllerFixes.md](./ControllerFixes.md). All changes are uncommitted.

## Source changes (`custom_components/multizone_thermostat`)

### `sync_manager.py`
- `async_sync_switches`: switches in `unknown` are now commanded (previously skipped). Only `unavailable` is skipped, with a WARNING naming the entity.
- `async_pwm_tick`: sync is now level-triggered. It reconciles desired vs actual switch state on every 10 s tick, not only on a PWM edge, so skipped or lost commands are retried. Still returns `True` only when the PWM state changed.
- `async_sync_proportional_valves`: same `unknown` / `unavailable` handling as above.

### `boiler/relay_backend.py`
- `_force_boiler_on`: works from `unknown`; skips (WARNING) only when the entity is missing or `unavailable`.
- `_force_boiler_off` and `async_emergency_off`: OFF is always attempted unless the switch is known to be `off`, including when `unknown` or `unavailable`. Skipped only if the entity does not exist (WARNING).
- New `_switch_valve_path_open()`: for demanding primary zones that have switches and no TRVs, returns `True` if any valve switch is `on`, `False` if all are closed/unknown, `None` if there are no such zones.
- Ignition hold: the boiler no longer ignites while every demanding switch-valve zone has its valves closed (`HOLD_VALVES_CLOSED`). Frost emergency and anti-seize are exempt.
- Zones with switches but no TRVs are no longer treated as "heat path open by default". Zones with neither remain open by default.
- `_conditions_still_valid` (post valve-delay re-check) also fails when switch valves are closed and no TRV path was ready.
- Added import of `CONF_ZONE_SWITCHES`.

### `boiler/plant_orchestrator.py`
- `async_tick` logs with `_LOGGER.exception` so tracebacks are kept.

### `pid.py`
- The derivative is now the negated least-squares slope of the measured temperature over a sliding window, multiplied by Kd, instead of a two-sample difference. A single quantised step (for example 18.3 to 18.2) is spread across the whole window instead of producing a spike.
- A slope needs at least 3 samples spanning at least a third of the window; otherwise D is 0 (for example straight after a restart or `clear_samples`).
- The result is clamped to ±`derivative_limit` (default 15%). `PID.MAX_DERIVATIVE = 15.0` is the default limit.
- New constructor arguments and properties `derivative_window` (seconds, default 900; 0 disables D) and `derivative_limit` (percent, default 15; 0 disables D), plus `set_derivative_params(window=None, limit=None)`. Non-numeric values are ignored and negatives become 0.
- `clear_samples` also clears the slope history.

### `pid_wrapper.py`
- `MultizonePID` accepts `derivative_window` / `derivative_limit` and exposes `set_derivative_params`.

### `const.py`, `number.py`, `coordinator.py`, `zone_controller.py` (configurable derivative)
- New `CONF_DERIVATIVE_WINDOW` (`derivative_window_min`, default 15 min) and `CONF_DERIVATIVE_LIMIT` (`derivative_limit_pct`, default 15%).
- Two new number entities on the Master Control device, "PID D Window" (0-60 min) and "PID D Limit" (0-100%), applied live to every zone PID via `coordinator.set_derivative_window` / `set_derivative_limit`. Set either to 0 to disable the derivative term.
- `ZoneController` reads the configured values when it builds the PIDs, falling back to the defaults if they are not numbers.
- Test support: `RestoreNumber` added to the dummy HA `number` component so `number.py` can be imported in tests.

### `autotune.py`
- New `MAX_KD_TO_KP = 60.0`; autotuned `Kd` is capped at `60 × Kp` per cycle. Already-stored Kd values are not rewritten; the PID clamp makes them harmless.

### `coordinator.py`
- New `_async_periodic_zone_refresh`, run every 60 s: recomputes demand for every available zone and updates the boiler, without feeding the autotuner or thermal model (`learn=False`), so learned rates are not skewed by timer samples with unchanged temperatures. A failure in one zone is logged and does not stop the others.

### `zone_controller.py`
- `async_update_zone` takes `learn: bool = True`. With `learn=False` it computes and stores demand only and returns before the autotuner and thermal-model updates.
- `_async_on_climate_state_changed` now delegates to the new shared `_async_refresh_zone`.
- Added `STATE_UNAVAILABLE` / `STATE_UNKNOWN` imports.

### `climate.py`
- `hvac_action`: for zones with switches and no TRVs, reports `HEATING` only when a valve switch is actually `on`, otherwise `IDLE`. TRV zones are unchanged.

## Tests

### New: `tests/test_controller_fixes.py` (22 tests)
- Valve switches: `unknown` commanded; `unavailable` skipped then resynced on next tick with unchanged PWM state; master OFF closes `unknown` switch; `unknown` proportional valve commanded.
- Boiler relay: OFF sent when `unknown` / `unavailable`; ON sent when `unknown`; hold while valves closed or `unknown`; fires when a valve is open; frost emergency never blocked; post-delay re-check; relay-only zone not blocked.
- PID: Kd=60000 with a -0.1 °C step at error -3 °C gives 0 output; derivative is 0 without enough history; flickering sensor with huge Kd stays at 0; autotune Kd cap.
- `hvac_action`: idle with closed valve, heating with open valve, heating for TRV zone.
- Periodic refresh: recomputes available zones only; survives a zone failure.

### Modified: `tests/test_boiler_subsystem.py`
- `test_relay_backend_skips_when_unavailable_fix_a` previously asserted that no command is ever sent to an `unknown` / `unavailable` boiler, which was the bug. It now asserts: ON is skipped when `unavailable`, OFF is still sent, and ON and OFF are sent when `unknown`.

### New: `tests/test_pid_derivative_window.py` (19 tests)
- Slope maths, minimum history, window trimming and reset, clamping, disabling via window 0 or limit 0, invalid parameters, wrapper passthrough, coordinator setters updating every zone PID, `ZoneController` applying configured values, and number entity routing.

## Test status
- Every test file was run individually after the derivative work and passed, including `tests/test_zone_controller.py` (its own runner). `test_thermal_harness.py` was not re-run.
- Test files must be run individually: `tests/test_zone_controller.py` installs a mock `homeassistant` finder that breaks other files in the same pytest session.
- `tests/test_phase5_e2e_simulations.py` fails at import (`homeassistant.data_entry_flow` missing from the dummy HA package). It fails identically without these changes.

## Not addressed
- Zone valve PWM and boiler PWM are still not phase-aligned, so a valve can close after the boiler has started.
- Plant-anomaly sensor and preset flapping (items 4 and 5 in the original document) are untouched. The false overshoot is expected to clear once ghost demand is gone, but this is unconfirmed.
- `relay_backend.py` still contains a `MagicMock` check in production code.
- No minimum on-time or rising-edge debounce was added to the zone PWM engine.
