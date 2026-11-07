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
| `pwm` | `ZoneSyncManager.async_pwm_tick` (valve switches) | zone name | `demand` (null when master OFF), `state`, `time_on/off`, `min_on/off`, `cycle_start_before`, `prev_demand_before`, `calculation_time` |
| `boiler` | `RelayBoilerBackend.async_update` | `boiler` | PWM snapshots, relay pre/post state and state-read/clock/zone/service trace, `switch_state`, `pending_command`, reason and update completion/error/cancellation |
| `sync` | `ZoneSyncManager.async_sync_switches` | zone name | `switch`, `desired`, `observed`, `action` (`turn_on`/`turn_off`/`skipped_unavailable`); in-sync ticks are not logged |
| `estimator` | `ThermalObserver.update` | zone id | sample inputs and rates, implementation, replay epoch/step, threshold, fresh/restored boundary and optional restore seed |
| `plant` | `PlantOrchestrator` | `boiler` | `action` (`aggregate`, `master_off`, `anti_seize`), pre-suppression `raw_demands`, `suppressed` reasons, DHW/frost flags, aggregate demand, strategy and primary/secondary results when aggregation runs |
| `aggregation` | `DemandAggregator.calculate` | `boiler` | effective per-zone mode, input/effective demand, configured weight, actual weight reads, strategy, override inputs and result |
| `opentherm` | `OpenThermBoilerBackend.async_update` | `boiler` | demand, target and weather inputs, pre-update state, state-read/clock/service trace, `inferred_on`, pending command, `update_completed`, `command_error` |
| `valve` | `ZoneSyncManager.async_sync_proportional_valves` | zone name | demand, mode and HA-observed state, prior command, target, action, command outcome and call/valve replay boundaries |
| `sensor` | climate, window, coordinator and OpenTherm diagnostics | entity id or null | initial/change observations for configured inputs and sampled OpenTherm diagnostic sources, plus external-temperature, TRV, window and zone-climate events |
| `config` | coordinator startup/settings, schedule manager and entry update listener | null | allowlisted control settings, zone and schedule snapshots and setting changes |
| `event` | free-form | any | schedule evaluation and manual overrides, hysteresis, zone mode resolution/changes, DHW freeze, weather compensation, frost, anti-seize, relay emergency-off and delayed relay task phases |

New `pwm`/`boiler` records capture selected engine state *before* the call and the exact
monotonic clock value read by the PWM calculation. Older records lack `calculation_time`; using
their later record timestamp can diverge at timing boundaries. Concurrent boiler updates may
interleave other state changes, so this alone does not establish full stateful plant replay.

Instrumentation wraps or follows existing calls; it does not change control flow or values.

`raw_demands` can be null if the snapshot is unavailable. Aggregation records capture effective
zone modes at the point they are resolved, configured weights and actual weight reads. Frost
records distinguish the virtual temperature and TRV average, include fallback TRV readings,
and record the mode and target inputs used during emergency action. Anti-seize records include
selected zones, saved states/demands, timing, lifecycle phases, boiler-off status and the operation
interrupted by a failure or cancellation. `update_completed` means the OpenTherm update returned from its service
calls, not that a particular command was sent or physically enacted. `command_completed` means
the proportional-valve service call returned, not that the valve moved. A false completion with
no error is distinguished from explicit cancellation; unsupported valve domains are identified separately.
HA entity states, OpenTherm `inferred_on` and switch states are not independent proof of burner
firing, valve position or water flow.

Sensor records cover the integration's external-temperature, TRV, window and virtual climate event
handlers, plus initial and change observations for configured zone, boiler, weather and presence
entities. They include selected control-relevant scalar attributes, not arbitrary HA attributes or
every dynamically discovered diagnostic sensor. OpenTherm diagnostic records also capture the
companion entity values read by each diagnostic evaluation and the resulting DHW/flame/fault state.
Numeric/boolean persistent settings and selected
control strings are logged; complex values and entry contents are not logged, to avoid indiscriminate
capture of unrelated configuration data. Entry updates include allowlisted control values and key
names. Startup records include selected core control fields, allowlisted zone configuration and
persisted scalar settings. Schedule startup/save records include profile definitions and overrides;
these can reveal household routines, so protect and handle telemetry files accordingly. This is not
a complete configuration snapshot; settings written through other storage paths can still be missed.
Weather inputs used by OpenTherm are recorded on calculation, not as a complete sensor event stream.
Zone mode resolution records distinguish window, calendar and configured modes; frequent mode
reads can generate many events. Zone-controller records identify DHW PID freezes/resets and
capture the PID result and weather feed-forward inputs and output. Calendar processing records
snapshot the resulting temperature, smart-start and mode overrides without recording calendar
titles. Those snapshots can still reveal household routines. They do not reconstruct every
calendar, presence or profile lifecycle.
Zone-control skip records distinguish missing or invalid temperatures, unregistered zones,
critical sensor faults and inactive modes, and identify whether demand was actually updated.
Schedule evaluations record before/after override state and the effective slot, upcoming blocks,
temperature and heating rate actually consulted for each zone. They can reveal household routines.
Smart-return plan records include its unrounded effective temperature, target and heating-rate
inputs. These records do not capture the underlying history or interrupted evaluations.

Independent physical actuator/flow feedback is deliberately out of scope until suitable hardware
and tests are available. Stateful full-plant replay from real recordings, complete override inputs,
all raw sensor events and complete configuration snapshots remain gaps; component replay is not a
substitute for these.

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
  compare `replayed_state` with `record["data"]["state"]`. `require_calculation_time=True`
  rejects older records without the exact clock read; the compatibility default falls back to
  their later record timestamp and is approximate at timing boundaries.
  `drivers.replay_stateful_pwm_from_records(records, zone, kind)` checks engine epochs, step
  continuity, pre-state and outputs, using the snapshots taken immediately after calculation.
  It rejects missing demand inputs except for explicit, consistent master-off PWM records, which
  did not call the engine and are skipped.
  The first record seeds its engine, and a missing tail cannot be detected without a later step.
  Boiler records can complete out of calculation order when updates overlap; such recordings
  are rejected rather than sorted or treated as complete plant replay.
* `drivers.replay_estimator(estimator, samples)` and `drivers.replay_pid(samples, set_point, **gains)`.
* `drivers.replay_estimator_from_records(records, zone)` replays contiguous recorded estimator
  epochs from a newly created estimator or a captured initial restore seed. It rejects missing
  steps, older record formats, unknown midstream state and implementation mismatches. It does
  not reconstruct observer inertia or detect arbitrary direct mutation of estimator internals.
* `drivers.replay_aggregation_from_records(records)` runs recorded effective inputs through the
  real aggregator. `drivers.replay_plant_decisions_from_records(records)` re-runs individual
  orchestrator decisions with recorded override results and mock backends; it requires matching
  adjacent aggregation records and does not replay the underlying override lifecycle or hardware.
  `drivers.replay_linked_plant_pwm_from_records(records)` checks the connection from each normal
  orchestrator decision to a contiguous boiler PWM calculation. It uses independently replayed
  orchestrator decisions and a stateful PWM engine, rejecting incomplete, overlapping or failed
  boiler updates, duplicate sequence numbers and non-update plant actions.
  `drivers.replay_linked_relay_plant_from_records(records)` additionally feeds those decisions
  into one real relay backend using the recorded state reads, clock reads, zone inputs and service
  outcomes. It checks backend state continuity, commands and outcomes, seeding the first backend
  state from its record. It supports sequential updates with zero valve delay and master-off
  operations, including recorded switch-service errors and cancellations. Other error sources
  without sufficient trace context are rejected. Ordinary service exceptions are injected as
  `RuntimeError` with the recorded message, not necessarily their original type. It rejects
  delayed or pending tasks, overlapping updates and other non-update plant actions. When the trace starts with
  master-off, PWM and diagnostic state are seeded by the first subsequent update.
  Old records lack the relay trace.
  It does not reconstruct the complete asynchronous
  plant, override lifecycle or physical hardware. Module clocks are temporarily patched, so use
  it offline without concurrent replay or live control.
  `drivers.replay_single_delayed_plant_from_records(records)` links one completed plant update
  to its real delayed task and relay command, with the switch-path calculation independently
  replayed from its captured reads. It requires one non-overlapping positive-delay cycle,
  checks the recorded wait elapsed with a 1-microsecond clock tolerance, and rejects incomplete
  or premature delays. The replay advances the wait immediately after checking its recorded
  duration; it does not reconstruct intervening updates, physical hardware or a whole plant history.
  `drivers.replay_sequential_delayed_plant_from_records(records)` applies this to consecutive,
  non-overlapping delayed cycles from one backend and PWM epoch, checking backend and PWM state
  continuity. Each cycle is independently seeded and executed; this is not a shared asynchronous
  backend replay and it rejects intervening updates and unmatched tasks.
  `drivers.replay_waiting_delayed_plant_from_records(records)` runs sequential recorded updates
  against one relay backend while one positive-delay task is pending. It supports updates before
  the task starts waiting and while it waits, checks backend and PWM continuity, validates the
  recorded wait, and executes the real task and relay command. The switch-path decision is
  independently replayed and supplied to the task. It also supports a final sequential update
  cancelling a task already waiting, when the cancellation and task completion are recorded after
  that update. For a cancellation not caused by a replayed update, it injects cancellation into
  the waiting task and reports the source as `unknown`; it does not identify or reconstruct the
  external action. It rejects overlapping task/update execution, multiple delay cycles and
  incomplete traces; it does not reconstruct other plant actions, physical hardware or arbitrary
  asynchronous histories.
  `drivers.replay_sequential_waiting_plant_from_records(records)` applies that driver to consecutive,
  non-overlapping waiting cycles, checking backend and PWM continuity and task identities across
  cycles. Each cycle is independently seeded and executed, not run as a single shared backend
  across the whole recording. Updates or actions outside those cycles remain unsupported.
* `drivers.replay_proportional_valves_from_records(records)` replays each proportional-valve
  decision from its recorded inputs and prior command, including service failures and cancellation.
  `drivers.replay_stateful_proportional_valves_from_records(records)` also checks command-state
  continuity and call ordering across recorded decisions. It rejects missing interior calls,
  incomplete or overlapping calls and divergent inputs; the initial previous command is seeded
  from the first record and a missing tail cannot be detected without a later record. It replays
  each decision separately through production code and checks the linked state, not the physical
  valve or an entire asynchronous plant. Older records lack the required boundaries.
* `drivers.replay_relay_delay_checks_from_records(records)` re-evaluates captured delayed-start
  recheck inputs. `drivers.replay_relay_delay_cycles_from_records(records)` replays complete
  delayed tasks independently with mocked boiler actions, including supported service errors
  and cancellation, and checks contiguous task steps within each backend epoch. A missing
  prefix or tail remains undetectable without another task boundary.
  `drivers.replay_relay_delayed_commands_from_records(records)` additionally
  matches recorded command attempts to those task lifecycles and re-runs the real relay command
  implementation from captured pre-state, state reads, clocks and service outcomes. It rejects
  missing command records and unsupported failure contexts; ordinary service exceptions are
  injected as `RuntimeError` with their recorded message. Command replay is independently
  seeded and does not reconstruct concurrent backend mutations. Older records lack its trace.
  `drivers.replay_relay_switch_paths_from_records(records)` separately replays
  the switch path calculation from captured zone modes, demands and switch reads. These are HA
  reported-state decisions, not physical flow proof; a complete asynchronous plant is not
  reconstructed. Incomplete tasks and unsupported failure contexts
  are rejected.
* `drivers.replay_weather_compensation_from_records(records)` re-runs the zone-controller
  feed-forward calculation using captured PID output, effective target, outdoor temperature and
  effective weather multiplier. It does not replay the PID, thermal model, or their combined state;
  those are separately replayed where supported.
* `drivers.replay_schedule_evaluations_from_records(records)` re-runs individual evaluations
  using recorded effective slot, future-block, temperature and heating-rate inputs, including
  profile expiry, manual overrides, explicit-off zones and smart-return decisions from the
  recorded effective plan. It checks the resulting schedule state and requested wake/preset
  actions, with those actions mocked. It does not recompute the smart-return plan or reconstruct
  profile editing, calendar events, external notifications or underlying sensor histories.
  Older records lack these evaluation inputs.
  `drivers.replay_smart_return_plans_from_records(records)` separately recomputes recorded plans
  from their effective inputs, including the unrounded heating rate.
  `drivers.replay_linked_smart_return_evaluations_from_records(records)` combines both checks and
  matches the plan to its evaluation using a captured per-evaluation identifier, validating the
  time, request and result. Missing, duplicate or reordered matching records are rejected.
  It does not reconstruct shared state across concurrent evaluations or the source of the
  effective numerical inputs; older records without identifiers cannot be linked.
  `drivers.replay_stateful_schedule_evaluations_from_records(records)` also checks explicit
  start/end boundaries, contiguous per-manager steps and before/after state continuity. It
  accepts a complete, explicitly recorded startup or saved-configuration snapshot as a
  state checkpoint between evaluations; it does not replay the configuration change itself.
  `drivers.replay_manual_schedule_overrides_from_records(records)` replays recorded manual
  set and clear mutations using the effective slot and target inputs. Stateful evaluation
  replay links these mutations to surrounding evaluations and checkpoints, checking their
  continuity. Clear replay covers the mutation itself; subsequent evaluation is replayed
  separately, with notification and task scheduling outside its scope. Manual actions have
  their own per-manager step counter, including while logging is disabled. Older logs lack
  these events and cannot reconstruct intervening manual mutations.
  It rejects overlapping, interrupted, missing or otherwise externally modified histories.
  The first record in an epoch seeds state, so an absent prefix or tail cannot be detected
  without another recorded boundary.
* `drivers.replay_hysteresis_from_records(records)` re-runs the learning-phase hysteresis using
  the recorded previous demand and effective target; it does not replay autotuner learning.
  `drivers.replay_zone_control_skips_from_records(records)` reproduces recorded early-exit
  decisions using their effective inputs, not the underlying sensor-fault detection.
* `drivers.replay_frost_checks_from_records(records)` re-evaluates recorded frost checks, including
  selected sensor inputs and TRV fallback readings, through the real frost manager.
  `drivers.replay_frost_scans_from_records(records)` replays complete, non-overlapping scans and
  their asynchronous mode and target requests using the recorded action inputs. It also injects
  recorded failures and cancellations of demand, mode and target actions; other interrupted,
  incomplete, overlapping or divergent scans are rejected. Ordinary failures are injected as
  `RuntimeError` with the recorded message, not necessarily the original exception type.
  `drivers.replay_opentherm_diagnostics_from_records(records)` re-evaluates the captured direct
  attributes and companion readings through the real diagnostic function.
  `drivers.replay_anti_seize_from_records(records)` re-runs complete, non-overlapping anti-seize
  cycles with mocked boiler and zone actions and recorded timing/configuration. It can inject
  recorded cancellation and supported operation failures, including the boiler's emergency-off
  request when its reported reason requires it. It rejects incomplete cycles, switch-only boiler
  fallback, unsupported failure stages and cleanup failures. Backend internals are mocked, not
  reconstructed, and injected ordinary failures retain their message but not their original type.
  These drivers neither reconstruct overlapping actions nor prove physical actuation.
* `drivers.replay_opentherm_targets_from_records(records)` re-runs the production temperature
  calculation from recorded weather and demand inputs.
* `drivers.replay_opentherm_commands_from_records(records)` runs each update through the real
  backend using recorded state reads, monotonic clock values, initial pending command and service
  outcomes. It detects missing/divergent trace events, including failed and cancelled calls. Each
  update is independently seeded, not a reconstruction of overlapping concurrent updates;
  comparisons of returned reason, pending state and commands with the original remain the caller's
  responsibility. Old records without a trace cannot be used.
* `plant_orchestrated.replay_orchestrated_plant(events, zones, ...)` replays synthetic timed input
  events through real aggregation, orchestration, zone PWM and relay boiler code, with simulated
  switches and deterministic valve delays. This is not yet a full real-log plant replay: it does
  not reconstruct every asynchronous HA input, frost or anti-seize lifecycle.
* `synthetic.simulate_room(...)` - seeded first-order room with known ground truth and a sensor that
  quantises, adds noise/flicker, and reports only on change (like real HA sensors).
* Real recorded history lives in `sample_data/` and is replayed by `tests/test_thermal_harness.py`.

PWM is replayed by swapping `pwm_engine.time` for the clock; PID takes explicit timestamps; the
estimators already take `ts`. Record-driven replay also depends on the production instrumentation
and its documented capture boundaries.

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

* Full stateful orchestration from real recordings, including arbitrary asynchronous ordering and
  delayed tasks, is not implemented. Narrow delayed-task cases are supported as described above;
  the orchestrated relay-plant harness uses synthetic inputs.
* Older PWM telemetry does not capture the engine's exact monotonic read; replay with its record
  timestamp can differ at a boundary. New records include the read, but overlapping boiler
  updates and missing records still prevent assuming exact full-plant reconstruction.
* Input and configuration records are selective, not a complete history of every sensor,
  configuration change or override lifecycle. Frost scan replay rejects unsupported interruptions;
  anti-seize replay rejects overlapping cycles and failures without supported operation context.
* Estimator record replay requires fresh or initially restored contiguous epochs. More real-world
  datasets and independent ground truth are needed; replay does not validate estimator accuracy.
* Config: `options_flow._save_options` rebuilds entry data from a fixed key list, so unlisted keys
  (e.g. a hand-set `rate_estimator`) look likely to be dropped on every options save. Unverified.
* Translations for the Diagnostics step are English placeholders in de/es/fr/it/ru.
* Time-base unification (`time.monotonic()` vs wall clock) so recorded wall time can drive replay of
  components that currently read only the monotonic clock.
* The older review documents (`AdversarialCodeReview.md`, `REMEDIATION_PLAN.md`,
  `EXECUTIVE_SUMMARY.md`) predate the observability-first priority order and the sync finding there is
  partly wrong (`_local_pwm_state` is level-triggered); `ObservabilityTestabilityPlan.md` is the
  authoritative ordering.
