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
| Non-finite floats | written as the strings `"NaN"`, `"+Inf"`, `"-Inf"`; the record carries `nonfinite: {path: token}` and `read_records` restores real floats at exactly those paths (files stay strict JSON, round trip is lossless) |
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
| `plant` | `PlantOrchestrator` | `boiler` | `action` (`aggregate`, `master_off`, `anti_seize`), pre-suppression `raw_demands`, `suppressed` reasons, DHW/frost flags, aggregate demand, strategy and primary/secondary results when aggregation runs |
| `opentherm` | `OpenThermBoilerBackend.async_update` | `boiler` | demand, target and bounds, weather inputs/offset, HA state, `inferred_on`, pending command, `update_completed`, `command_error`; unavailable updates have a reduced record |
| `valve` | `ZoneSyncManager.async_sync_proportional_valves` | zone name | target, HA-observed state, previous command, scale, mode, action, `command_completed` and `command_error` for command paths |
| `event` | free-form | any | currently `hysteresis_demand` (autotune learning phase) |

`pwm`/`boiler` records carry the engine state *before* the call, so a replay can be exact from any
starting record.

Instrumentation wraps or follows existing calls; it does not change control flow or values.

`raw_demands` can be null if the snapshot is unavailable; aggregate maps describe the output,
not every mode or weight used to calculate it. Override records do not capture all frost or
anti-seize internals. `update_completed` means the OpenTherm update returned from its service
calls, not that a particular command was sent or physically enacted. `command_completed` means
the proportional-valve service call returned, not that the valve moved. A false completion with
no error can indicate cancellation; unsupported valve domains are identified separately.
HA entity states, OpenTherm `inferred_on` and switch states are not independent proof of burner
firing, valve position or water flow.

Still missing: raw sensor events, configuration changes, effective aggregation modes and weights,
complete override inputs and transitions, independent actuator/flow feedback, and complete plant
and OpenTherm replay. Direct estimator replay from telemetry records also remains to be implemented.

### ASHRAE-inspired test mapping

The tests in `tests/test_ashrae_plant_observability.py` cite ASHRAE Guideline 36-2018
Addendum y sections where relevant. They adapt §5.21.2 (plant decisions), §5.21.4 (supply
temperature control), §4.11 and §5.21.10 (command/feedback), and §5.21.11 (operating
states and faults) to this domestic system. Delayed relay tests also exercise path and request
rechecks and cancellation under §4.11 and §5.21.2.4, without simulating a complete plant.
MZT does not implement the guideline's full central
plant sequences, pump/flow proof or trim-and-respond reset. These are engineering tests, not
guideline conformance or certification tests, and the guideline's timings and thresholds are
not assumed to apply to this installation.

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
| PLANT-001 | `test_plant_closed_loop.py` | at 10-25 % demand the 5 min boiler min-on outlasts the valve open window: boiler fires into a closed valve (~74-89 % of its on-time in the simulation) |
| PLANT-002 | same | at 80 % demand boiler PWM and valve PWM are unsynchronised (~21 % of on-time into a closed valve) |
| PLANT-003 | same | 90 s valve actuation latency leaves the boiler firing while the valve is closed (~25 % of on-time) |

PLANT results come from `tests/mzt_replay/plant_sim.py`: real `ZoneSyncManager` valves and real
`RelayBoilerBackend` on a manual clock with fake switches (optional latency). Caveat: the simulation
replaces `PlantOrchestrator` with a minimal tick loop and does not run the delayed valve-open task, so
`valve_delay > 0` is not covered yet. Treat the percentages as indicative until validated on real logs.

Characterisation tests (green, named `test_characterisation_*` or documented in the docstring) pin
behaviour that is questionable but deliberate, e.g. integral discarded on a >=1.5 degC setpoint step
(PID-003) and duty inflation for tiny demands under `min_on`.

### Workflow for fixing a defect

1. `pytest --show-red -k <ID-ish test>` and confirm the failure is the real symptom.
2. Change the production code.
3. The test now XPASSes (strict) and the run fails: remove the `known_defect` marker.
4. Re-run replay on real logs/samples before and after; compare, then commit.

## 4. CSV export and summary CLI

```
python scripts/mzt_log.py csv  <dir-or-files> -o out.csv [--kind pid] [--zone NAME]
python scripts/mzt_log.py summary <dir-or-files>
```

Implemented in `telemetry_export.py` (`export_csv`, `summarize`). Non-finite values appear as `NaN`,
`+Inf`, `-Inf` in the CSV as well.

## 5. Diagnostics options and debug sensors

Options -> Diagnostics has two switches:

* `telemetry_enabled` (default **on**): write the rolling JSONL log.
* `debug_sensors` (default **off**): create diagnostic sensor entities (PID, estimator and valve PWM
  per zone, plus a boiler sensor) that show the latest record as attributes. They are excluded from
  the HA recorder, refresh every 10 s and publish NaN/Inf as strings. If telemetry is disabled but
  debug sensors are on, an in-memory recorder (`persist=False`) feeds the sensors and nothing is written
  to disk.

## 6. PID replay and the frozen reference

`replay_pid_from_records(records, zone, module=None)` re-runs logged `pid` records through a PID
class. Replay is exact from a clean-state boundary (a record whose `last_input_time` is None); dead
sensor records are skipped; gains, limits and mode come from each record.
`tests/mzt_replay/reference/pid_v0.py` is a frozen copy of the current (defective) PID. Differential
tests in `test_replay_pid_records.py` prove production == reference on defect-free domains (Kd=0 with
sane dt, smooth signals, OFF mode, mid-stream parameter changes). A deliberate fix may diverge only
where the PID-001/002/005 known-defect tests apply. No refactor of the PID was needed.

## 7. Gaps still to close

* `valve_delay` / delayed-check path in the closed-loop plant harness; valve closing while boiler ON
  with the real `PlantOrchestrator`.
* Instrumentation of OpenTherm backend, proportional valves, `DemandAggregator`, frost/anti-seize,
  DHW override and raw sensor events (a `sensor` record kind).
* Replay of estimator records from real logs through the frozen reference (estimators only replay
  synthetic data today).
* Config: `options_flow._save_options` rebuilds entry data from a fixed key list, so unlisted keys
  (e.g. a hand-set `rate_estimator`) look likely to be dropped on every options save. Unverified.
* Translations for the Diagnostics step are English placeholders in de/es/fr/it/ru.
* Time-base unification (`time.monotonic()` vs wall clock) so recorded wall time can drive replay of
  components that currently read only the monotonic clock.
* The older review documents (`AdversarialCodeReview.md`, `REMEDIATION_PLAN.md`,
  `EXECUTIVE_SUMMARY.md`) predate the observability-first priority order and the sync finding there is
  partly wrong (`_local_pwm_state` is level-triggered); `ObservabilityTestabilityPlan.md` is the
  authoritative ordering.
