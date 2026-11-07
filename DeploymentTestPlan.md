# Deployment Test and Data Collection Plan

This plan is for an **unpublished fork** deployed to one Home Assistant installation. Its purpose
is to check the deployment, observe normal operation and collect useful evidence for offline replay
before changing numerical control behavior or the default rate estimator. It is not a certification
of boiler, pump or valve safety.

**Installation note:** The owner reports that the boiler has a built-in bypass. Closing all zone
valves is therefore not necessarily the same as closing every water path. Its presence alone does
not establish the permissible test conditions, flow or duration for this installation.

Related: [ObservabilityTestabilityPlan.md](./ObservabilityTestabilityPlan.md) and
[TelemetryAndReplay.md](./docs/TelemetryAndReplay.md). The latter describes the current record
schema, replay drivers and known defects.

## 1. Safety and scope

- Start with **passive observation**. Run any deliberate setpoint or mode change only when you are
  present, the heating system can safely accommodate it, and you know how to restore normal operation.
- Do not deliberately close every water path while firing the boiler, bypass safety interlocks,
  shorten hardware protection timings, disconnect a safety-critical sensor, or force switch
  failures to reproduce a test. A supervised all-zone-valves-closed observation is considered
  separately below, provided a safe bypass path is confirmed for the installation.
- Before deployment, identify the normal way to stop the heating and restore the previous integration
  version and configuration. Do not rely on Home Assistant telemetry as a safety interlock.
- If the boiler fires with no **confirmed safe open flow path**, relay cycling looks excessive,
  equipment behaves unexpectedly, or the house or equipment approaches an unsafe condition: stop
  the test, use the normal safe shutdown procedure, and investigate. Do not wait for a log flush.
- A switch reporting `on`, a requested valve opening, and a physically open valve are different
  things. Unless actual position or flow is independently measured, record the path as **unknown**,
  not confirmed open or closed. A system with an independently verified bypass may have a safe
  path even when zone valves are closed; document its behavior.
- Don't leave an active experiment running unattended. Ordinary baseline collection may continue
  under your normal operating arrangements if the initial checks are satisfactory.

## 2. Prepare before deployment

- [ ] Record the exact fork commit deployed, installation method, deployment time with timezone,
  Home Assistant version, and any local modifications. Keep a recoverable backup of the existing
  integration and configuration using your normal backup procedure.
- [ ] Note the boiler backend (relay or OpenTherm), boiler switch or gateway, zone names, sensor
  entity IDs, switch/valve entity IDs, and which zones can call for heat. Note any bypass, physical
  feedback and whether switch `on` actually means open. Map `climate` entity IDs to zone names.
- [ ] Record control settings: PID gains and mode, current estimator, zone/boiler PWM intervals,
  minimum on/off times, valve delay, sensor timeout, schedules, frost and DHW settings where
  applicable. Include sensor resolution and reporting behavior if known. Record changes and their
  exact times during the test rather than assuming telemetry captures configuration changes.
- [ ] Ensure there is sufficient disk space and that you can access the HA configuration directory
  and copy files off the device. Protect backups and logs: entity names, occupancy patterns, schedules
  and temperatures may be sensitive.
- [ ] Note whether HA History/Recorder retains the relevant temperature, outdoor temperature,
  setpoint, boiler, valve and DHW entity histories for the planned period. Preserve existing
  retention settings unless you deliberately choose to change them; exported history supplements
  telemetry, it does not replace the original JSONL.
- [ ] If available, identify an independent room temperature reference and a way to observe boiler
  firing and physical valve/flow status. Record its accuracy, sampling interval and clock. Do not
  treat an uncalibrated or different-location sensor as exact ground truth.

## 3. Deploy and smoke-test

1. Deploy the chosen fork revision using your usual method; reload or restart as required by your
   installation. Note start and completion times. Confirm the integration and its existing entities
   load and the configured estimator remains the intended one. Do not change the default estimator.
2. In the integration's **Options → Diagnostics**, confirm **Write telemetry log files** is enabled
   (default on). Debug sensors are optional and default off; enable them only if useful. If you
   change an option, allow for the integration to reload and record the time.
3. Allow at least 60 seconds and a normal control update. Check that files appear and grow under
   `<HA config>/mzt_telemetry/`. Inspect Home Assistant logs for integration errors and telemetry
   flush warnings. If no records are written, resolve that before relying on data collection.
4. If debug sensors are enabled, check that the latest PID, estimator, valve PWM and boiler readings
   appear when the relevant record type has occurred. Some may have no data until that path runs;
   switch-only, relay-only and active-mode applicability varies. Debug sensors show the *latest*
   record, not a history, and do not replace the JSONL files. If file logging is disabled while
   debug sensors remain enabled, their in-memory readings are not saved to telemetry files.
5. Observe at least one ordinary demand/boiler/valve transition if conditions permit. Check that
   reported states make sense, normal protections appear to work, and the system is safe. Note
   the times and any discrepancies between commanded, reported and independently observed states.

**Continue only if:** normal operation is acceptable, a safe flow path is maintained, and new
telemetry is being written. Otherwise stop and retain the evidence.

## 4. Baseline collection

- [ ] Collect at least 24 hours of normal operation; preferably 2–7 days if practical. Capture
  ordinary schedules, heating and cooling periods, and naturally occurring cycling; do not extend
  a run at the expense of comfort or safety.
- [ ] Check the file directory and HA logs daily. Files are flushed approximately every 30 seconds;
  abrupt shutdowns can lose buffered records. Retention is bounded to 7 days and 150 MB total,
  whichever removes data sooner; files rotate at approximately 50 MB. Copy files off daily and
  after significant incidents, including all rotated parts. Don't delete the live files as part of
  collection.
- [ ] Record normal weather/outdoor temperature and any unusual heat sources, open windows, occupancy
  changes, manual overrides or schedule changes. These affect rate estimates and are not necessarily
  captured by telemetry.
- [ ] Watch for sensor quantization/flicker, overshoot, valve chatter, boiler cycling, apparent
  demand without firing, and mismatch between demand, reported switches and observed operation.
  Log findings whether behavior looks good or bad; avoid selecting only interesting failures.

## 5. Optional supervised scenarios

Perform only scenarios that are safe and natural for this installation. Record a pre-observation,
action, response and recovery period; restore original settings afterward. A scenario that cannot
be run safely is **not run**, not a failed safety test. Do not repeatedly manipulate equipment to
chase a desired demand percentage.

| Scenario | Procedure and evidence | What to look for |
|---|---|---|
| Stable / near setpoint | Observe a zone already near its scheduled target for at least one normal cycle, if one occurs. | Temperature steps, PID P/I/D and demand changes, valve and boiler transitions, audible relay activity. Note if there is no cycle. |
| Small setpoint change | While present, make one modest change within normal comfort limits; observe through the response, then restore it. | Target and demand response, valve/boiler timing and any overshoot. Log the old/new target and times. |
| Natural low or high demand | Observe periods where a zone naturally reports low or high demand; don't force thresholds. | Compare requested PWM, reported switch state and independently confirmed flow path. PLANT-001/002 are simulation hypotheses, not live-system predictions. |
| Heating and subsequent cooling | Capture a normal heating period followed by cooling, preferably several hours, without deliberately disabling safety features. | Room/outdoor temperature, heating state, reference sensor, reported heating/cooling rates and confounding heat sources. |
| Multiple zones | Observe ordinary periods with different zones calling for heat. | Which zones demand heat, their reported and independently observed valve status, and boiler operation. Note any safe bypass. |
| Normal scheduled or manual mode transition | Observe one routine transition, or perform a normal UI mode change only if safe. Restore the original mode. | Whether output and reported states converge as expected; record timestamps and configuration. |
| Naturally occurring fault | If a sensor, switch or network becomes unavailable during ordinary use, do not manufacture or prolong the fault. Preserve the lead-up and recovery. | Unavailable duration, reported state, commands, actual observation, HA errors and recovery. If unsafe, stop the system rather than collect more data. |

The simulation's delayed-actuation and closed-valve scenarios are **not** a reason to block a real
valve, cut power to an actuator, or run the boiler into a closed circuit. Test injected actuator
faults offline until a suitable safe test rig exists.

### Bypass-aware all-zone-valves-closed observation

This is valuable for investigating whether boiler demand, valve PWM and the relay backend converge
when the last zone valve closes, including the min-on interaction behind PLANT-001/002. It is not
the same as proving the simulated results or testing a blocked circuit.

- [ ] Before trying it, confirm with the boiler manufacturer's documentation or a qualified heating
  professional that the built-in bypass is appropriate for this installed system and the intended
  operating conditions. Establish the manufacturer's operating limits and how to verify the flow
  path. A switch state, a sound or the existence of a bypass does not by itself confirm adequate flow.
- [ ] Establish a normal safe stop and recovery procedure, and observe the plant throughout. Keep
  all protective settings and interlocks enabled. If the flow path or operating conditions cannot
  be confirmed, do not induce the condition; capture an ordinary naturally occurring transition
  instead and mark the deliberate test not run.
- [ ] With the safe path confirmed, observe a normal transition where all zone valves close using
  the integration's ordinary controls, rather than forcing the boiler or blocking an actuator.
  Record when demand changes, when each valve is commanded and physically observed closed, the
  boiler command and independently observed firing state, and the reported reason and minimum
  cycle timings. Record any bypass/flow measurement and its uncertainty.
- [ ] Stop and use the normal safe shutdown procedure if flow becomes uncertain, equipment behaves
  unexpectedly or any manufacturer's limit is approached. Restore normal zone settings afterward.
- [ ] Preserve the full lead-in and recovery, HA histories and telemetry. Note separately: time
  after the **last confirmed valve closure**, time the boiler is commanded on, time independently
  observed firing and whether bypass flow was verified. Unknowns must remain unknown.

Useful comparisons include whether the boiler starts with all demanding valves closed, remains on
after the last valve closes, turns off or holds due to minimum-on timing, and resumes normally after
a zone reopens. Treat a captured event as an observation of this configuration, not evidence that
all combinations of demand, valve latency or faults are safe.

### Optional passive collector

[`scripts/mzt_capture.py`](./scripts/mzt_capture.py) polls selected Home Assistant entities and
writes timestamped observations to a **new** local capture directory. It only uses the HA
`GET /api/states` endpoint: it does not change setpoints, modes, switches or protection settings.
Python's standard library is sufficient; no extra packages or integration changes are needed.

On the machine running the script, set a Home Assistant long-lived access token in the `HA_TOKEN`
environment variable. Use HTTPS with a trusted certificate. Plain HTTP sends the token unencrypted:
only on a trusted network, explicitly opt in with `--allow-http`. Avoid placing the token in a
command line, captured notes or shared files. Example in PowerShell, replacing all entity IDs:

```powershell
$secureToken = Read-Host "Home Assistant long-lived token" -AsSecureString
$env:HA_TOKEN = [System.Net.NetworkCredential]::new("", $secureToken).Password
Remove-Variable secureToken
python .\scripts\mzt_capture.py --url "https://homeassistant.example:8123" `
  --output "C:\MztCaptures\run-01" `
  --boiler "switch.boiler" --valve "switch.living_valve" --valve "switch.bedroom_valve" `
  --entity "climate.living" --entity "sensor.living_temperature" `
  --entity "sensor.outdoor_temperature" --duration 86400
Remove-Item Env:\HA_TOKEN
```

Set `--url` to the actual base URL; add `--allow-http` if intentionally using HTTP. Repeating
`--entity` captures any relevant states, including physical feedback entities, outdoor temperature,
DHW and reference sensors if available. The initial snapshots include their HA attributes, which
may contain some settings; record the remaining configuration and deployed revision separately.
Choose an output path that **does not exist**. For a passive run without boiler/valve candidate
alerts, omit both `--boiler` and `--valve` and provide at least one `--entity`.

The script polls every 10 seconds by default (`--interval` changes this). It writes an initial state
and subsequent changes, including attributes and HA timestamps, to `observations.jsonl`; it reports
missing selected entities explicitly. Type a line and press Enter to timestamp an observation marker,
such as `experiment start` or `heard relay click`. Enter markers promptly: they are written when
the collector next processes its input. Use Ctrl+C to stop; `--duration` is optional and is in
seconds. Capture files can contain sensitive information; store and share them accordingly.

When `--boiler` and all relevant `--valve` entities are supplied, a candidate is flagged if HA
reports the boiler `on` and **all selected** valves `off`; this assumes `on` means open. Unknown,
missing or unavailable states are reported as unknown, not closed. A second alert flags six or
more observed on/off transitions for a selected boiler or valve within an hour; adjust
`--rapid-threshold` for investigation. These are prompts to inspect the data, not safety or
correctness verdicts. They do not prove physical firing, valve position, adequate bypass flow or
that unlisted valves are closed.

If the HA telemetry directory is accessible as a local path or mounted share, add, for example,
`--telemetry-dir "Z:\config\mzt_telemetry" --copy-every 3600`. The collector copies all
`mzt_*.jsonl` files into separately timestamped snapshots at startup, at the interval and on exit;
it never deletes source files. A missing directory, missing files or copy failure stops the
collector with an error. Copies of active files can end mid-record: preserve a later snapshot too,
and do not combine duplicate snapshots as if they were independent events. If the HA filesystem
isn't mounted, omit this option and copy telemetry separately as described below.

Polling can miss short transitions and event timings are bounded by the polling interval and
network delays. It is not a continuous safety monitor. Connection failures are timestamped,
announced and retried; recovery begins a fresh snapshot and clears transition counts. If the
collector exits while disconnected it reports failure. Keep HA History, original telemetry,
manual observations and the independent flow checks: the collector does not retrieve historical
events that occurred between polls or while disconnected. Use the existing `mzt_log.py` commands
below for offline summaries and CSV exports; do not point them at a parent containing multiple
duplicate snapshot directories.

## 6. Incident and observation notes

For each interesting period, record:

```text
Observation ID:
Start / end (ISO 8601 with UTC offset):
Deployed commit / HA version:
Zone(s) and relevant entity IDs:
Expected behavior / actual behavior:
Setpoint, mode, schedule and any deliberate action:
Boiler command, HA-reported state and independent firing observation:
Valve command, HA-reported state and independent position/flow observation:
Safe bypass or other known flow path:
Sensor readings, independent reference and outside conditions:
Errors, audible events, latency and any interventions:
Data files and whether the beginning/end of the event were captured:
```

Keep a few minutes of lead-in and follow-up for short events, and the full relevant heating/cooling
period for rate-estimator analysis when possible. Distinguish exact timestamps from estimates. Note
when the independent observation was last checked: absence of a manual note is not proof of
continuous valve or boiler state.

## 7. Preserve and inspect the data

1. Copy the original JSONL files, including rotated parts, to a private, dated collection folder.
   The currently active file may be copied while growing; capture it again after the interval and
   note the copy time. Don't edit, deduplicate or overwrite the originals. Keep version, config
   notes, observation notes and relevant HA history/log exports alongside them.
2. Preserve UTC offsets and clock details. Telemetry has wall and monotonic clocks; filenames use
   UTC days. Recorder sequence numbers and monotonic time can reset across restarts/reloads:
   record boundaries and keep continuous segments separate rather than assuming one global sequence.
3. From a checkout of the **same revision**, inspect copied logs offline. The following examples
   use PowerShell on Windows; replace the path with your copy:

   ```powershell
   python .\scripts\mzt_log.py summary "C:\MztCaptures\baseline"
   python .\scripts\mzt_log.py csv "C:\MztCaptures\baseline" --kind boiler -o "C:\MztCaptures\boiler.csv"
   python .\scripts\mzt_log.py csv "C:\MztCaptures\baseline" --kind pid --zone "climate.example" -o "C:\MztCaptures\pid.csv"
   ```

   Replace `climate.example` with the actual climate entity ID. PID and estimator records use
   climate/zone IDs, valve PWM and sync use zone names, and boiler records use `boiler`.
   CSV supports `--since` and `--until` as epoch seconds or ISO timestamps with an offset;
   timestamps without an offset are interpreted as UTC. Preserve the JSONL even after CSV export:
   it includes information needed for replay, including non-finite value metadata.
4. Verify the summary covers the expected period, zones and record kinds; compare against the
   incident notes and HA history. Look for unexplained missing periods, resets, flush warnings and
   missing rotated files. `sync` records are not emitted for every in-sync tick; missing `sync`
   entries alone do not prove a fault. Missing data is **unknown**, not a successful test.
   Treat summary cycling figures as preliminary: they describe logged PWM states, not measured
   hardware operation, and may be misleading across restarts or rotated files. Directory expansion
   sorts filenames lexically, which does not guarantee chronological order of rotated parts. Check
   timestamps and segment/order records before replay or calculating transition rates.
5. Keep raw copies private. Before sharing, inspect both telemetry and exports for sensitive names,
   locations, schedules, occupancy and other personal data. Remove credentials from configuration
   exports. If anonymizing IDs, preserve a consistent mapping and data types and keep the original
   securely; record every transformation so replay integrity can be assessed. Do not publish raw HA
   backups or logs without review.

## 8. What offline analysis can and cannot establish today

- PWM and relay boiler **PWM-engine calculations** can be replayed from records with
  `replay_pwm_from_records`; this does not replay the full relay backend, safety checks, plant or
  real actuator behavior. Select the appropriate zone and kind, and treat gaps, resets or parameter
  changes as boundaries needing inspection.
- PID records can be replayed with `replay_pid_from_records` from a clean-state boundary; preceding
  mid-run records and dead-sensor records are skipped. Preserve startup/reload and reset boundaries.
  The frozen PID reference and differential tests characterize the existing implementation; exact
  replay alone does **not** mean the controller's decisions are correct.
- Estimator replay exists for sample sequences and recorded history has an existing thermal harness,
  but direct replay of estimator telemetry records is not yet implemented. Preserve raw sensor and
  heating-state history to support future adaptation. Independent measurements provide context, not
  automatically exact thermal ground truth.
- Raw sensor events, demand aggregation, OpenTherm decisions, proportional valve behavior,
  frost/anti-seize, DHW overrides and configuration changes are not comprehensively instrumented.
  Capture supporting HA histories and manual notes. Debug sensors and relay/switch state alone
  cannot prove actual valve position or safe flow.
- Synthetic failures and predicted percentages in the known-defect registry are hypotheses to
  compare with field evidence, not acceptance thresholds for a live heating system.

## 9. Completion checklist

- [ ] Deployment identity and settings recorded; rollback remains available.
- [ ] Startup and normal-operation checks completed, or the test stopped with the reason recorded.
- [ ] Original telemetry copied with rotation parts, timestamps, restart boundaries and incident notes.
- [ ] Relevant HA history and independent observations preserved where available.
- [ ] CSV/summary checked for expected time range and kinds; gaps and anomalies documented.
- [ ] Open questions and unsafe or unavailable scenarios explicitly marked **not run**.
- [ ] Any data intended for sharing reviewed and sanitized consistently.

The next offline step is to turn captured intervals into reproducible fixtures, compare logged and
replayed decisions where supported, and add tests for observed failures before considering control
changes. Do not infer completion of uninstrumented paths from an uneventful baseline.
