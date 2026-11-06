# Telemetry, Replay and the Known-Defect Registry

No numerical control code should change until the behaviour it replaces can be **observed**,
**replayed** and **pinned by a test**. This document covers the three tools that make that possible.

## 1. Telemetry (production)

`custom_components/multizone_thermostat/telemetry.py` records one JSON object per line to
`<ha-config>/mzt_telemetry/mzt_YYYY-MM-DD.jsonl` (UTC days).

| Property | Value |
|---|---|
| Buffering | `record()` only appends to a bounded in-memory deque (5000); never raises, never does I/O |
| Flushing | every 30 s via the executor, and on unload |
| Retention | 7 days, 150 MB total (oldest files removed first), 50 MB per file (then `.1`, `.2`...) |
| Non-finite floats | written as `null`, original listed under `nonfinite` (files stay valid JSON) |
| Torn lines | skipped by `read_records` (expected after a crash) |
| Clocks | every record has `wall` (epoch s) **and** `mono` (monotonic s) |

Record envelope: `{"v":1,"seq":N,"wall":..,"mono":..,"zone":"..","kind":"..","data":{..}}`

| kind | emitted by | zone | key fields |
|---|---|---|---|
| `pid` | `ZoneController` after `MultizonePID.calc` | climate entity id | `input`, `set_point`, `effective_target`, `p`,`i`,`d`,`ext`, `error`, `dt`, gains, `demand`, `dead_sensor`, `sensor_age` |
| `pwm` | `ZoneSyncManager.async_pwm_tick` (valve switches) | zone name | `demand` (null when master OFF), `state`, `time_on/off`, `min_on/off`, `cycle_start_before`, `prev_demand_before` |
| `boiler` | `RelayBoilerBackend.async_update` | `boiler` | same PWM fields **plus** `switch_state`, `pending_command`, `since_last_change`, `reason`, `reason_before` |
| `sync` | `ZoneSyncManager.async_sync_switches` | zone name | `switch`, `desired`, `observed`, `action` (`turn_on`/`turn_off`/`skipped_unavailable`); in-sync ticks are not logged |
| `estimator` | `ThermalObserver.update` | zone id | `ts`, `temp`, `is_heating`, `outdoor_temp`, `estimator`, `heating_rate`, `cooling_rate` |
| `event` | free-form | any | currently `hysteresis_demand` (autotune learning phase) |

`pwm`/`boiler` records carry the engine state *before* the call, so a replay can be exact from any
starting record.

Instrumentation wraps or follows existing calls; it does not change control flow or values.

Not yet instrumented (next): `OpenThermBoilerBackend`, proportional valves, `DemandAggregator`,
frost/anti-seize, DHW override, raw sensor events, config/option changes.

## 2. Replay (offline, `tests/mzt_replay`)

Runs the **real production modules** (loaded by path, so Home Assistant is not needed) under a
deterministic `ManualClock`.

* `drivers.replay_pwm(demands, ...)` - feed `(t, demand)` to the real `PWMEngine`.
* `drivers.replay_pwm_from_records(records, zone=...)` - re-run logged `pwm`/`boiler` records;
  compare `replayed_state` with `record["data"]["state"]`. Used by `tests/test_telemetry.py` and
  `tests/test_replay_sync.py` to prove production -> log -> replay is exact.
* `drivers.replay_estimator(estimator, samples)` and `drivers.replay_pid(samples, set_point, **gains)`.
* `synthetic.simulate_room(...)` - seeded first-order room with known ground truth and a sensor that
  quantises, adds noise/flicker, and reports only on change (like real HA sensors).
* Real recorded history lives in `sample_data/` and is replayed by `tests/test_thermal_harness.py`.

PWM is replayed by swapping `pwm_engine.time` for the clock; PID takes explicit timestamps; the
estimators already take `ts`. No production module needed editing for replay.

## 3. Known-defect registry (the deliberately red tests)

Tests marked `@pytest.mark.known_defect("ID", "reason")` assert the **correct** behaviour that the
code is known not to have.

* `pytest` - they are reported as **strict xfail**: the suite stays green, but fixing the code makes the
  test XPASS, which *fails* the run until the marker is removed (so fixes cannot go unnoticed).
* `pytest --show-red` - they run as ordinary tests and fail with the real measured values.
* `pytest -rx` - lists every registered defect with its reason.

| ID | Where | Symptom reproduced |
|---|---|---|
| EST-001 | `test_replay_estimators.py` | LegacyEma cooling 5-9 degC/h vs true 0.35 once the sensor has any noise (clean sensors are fine) |
| EST-002 | same | cooling estimate never settles (3.6..7.0 over 12 h) |
| EST-003 | same | heating rate 5.5 vs true 1.6 degC/h |
| EST-004 | same | cooling estimate polluted after PWM-style heat bursts |
| PWM-001/002 | `test_replay_pwm.py` | demand touching 0 or >=99 bypasses min_off/min_on (10 s runs against a 300 s minimum) |
| PWM-003 | same | 2%/0% demand flutter: relay toggles every 30 s |
| PWM-004 | same | 98%/99.5% flutter flips ON/OFF immediately |
| PID-001 | `test_replay_pid.py` | 0.1 degC flicker on a flat temperature drives the derivative to its limit (15) |
| PID-002 | same | after a sensor outage the integral jumps by the outage length (72 vs 0.3 per normal step) |
| PID-005 | same | NaN input returns the output from *before* the previous step |
| SYNC-001 | `test_replay_sync.py` | one failing switch aborts the loop; later switches are never commanded |
| SYNC-002 | same | a service-call exception escapes the 10 s PWM tick |
| SYNC-003 | same | valve switches use a private 0/0 PWM: 120 valve commands/hour on demand flutter |

Characterisation tests (green, named `test_characterisation_*` or documented in the docstring) pin
behaviour that is questionable but deliberate, e.g. integral discarded on a >=1.5 degC setpoint step
(PID-003) and duty inflation for tiny demands under `min_on`.

### Workflow for fixing a defect

1. `pytest --show-red -k <ID-ish test>` and confirm the failure is the real symptom.
2. Change the production code.
3. The test now XPASSes (strict) and the run fails: remove the `known_defect` marker.
4. Re-run replay on real logs/samples before and after; compare, then commit.

## 4. Gaps still to close

* Boiler-on-into-closed-valves (system level): needs a closed-loop harness wiring `DemandAggregator`,
  `RelayBoilerBackend` and fake valves with latency and faults.
* Replay of `pid` records from real logs (the log has everything needed; a driver is not written yet).
* Log viewer CLI (summary, PWM transitions per hour, estimator vs. reference plots).
* Instrumentation for the items listed under section 1.
* Time-base unification (`time.monotonic()` vs wall clock) so recorded wall time can drive replay of
  components that currently read only the monotonic clock.
