# MZT: valves not opening, boiler firing with no demand (2026-11-04)

Evidence sources: HA recorder history and logbook for 2026-11-04, plus the code in
`custom_components/multizone_thermostat` (deployed 10:10, HA restarted 10:11).
There is no `home-assistant.log` on this instance (only a 0-byte `.fault` file), so no
integration log lines were available. Everything below is reconstructed from entity history
and the code.

## Timeline

| Time (UTC) | Event |
|---|---|
| 10:11:13 | HA restart. All five `switch.mqtt_io_*_valve` go `unavailable`, then `unknown` at 10:11:24. |
| 10:11 - 21:52 | **No valve switch ever changes state.** They stay `unknown` for about 11.5 hours. |
| 11:04 - 19:35 | The boiler is switched ON 10 times by the integration ("Demand 59-100% (after valve delay)"). Every primary zone is 2-4°C above its 15°C target, and the valves are not opening. |
| 21:16 | Boiler ON again, still no valves. |
| 21:18 | Preset switched to comfort. Targets rise to 20/20/17/20, zones report `hvac_action: heating`, valves still `unknown`. |
| 21:48 - 21:52 | Preset flapping, master OFF/ON several times. Valves finally change state (a `on` then immediate `off` pulse per valve at 21:52:00-13, then all `on` at 21:52:46 after the master toggle). |

## Root cause 1: valve switches in `unknown` are never commanded (the "stopped controlling valves" bug)

`sync_manager.py`, `async_sync_switches`:

```python
st = self.hass.states.get(switch)
if not st or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
    continue
```

After a restart the MQTT switches have no retained state, so they sit in `unknown`.
`unknown` is not "offline" for an MQTT switch; it just means no state has been reported
yet, and the only way to get one is to send a command. Because the integration refuses to
command `unknown` switches, it deadlocks until something else touches the valve. The
master toggle plus the manual toggles at 21:52 were what finally broke the deadlock.

Related defects that make it worse:

- `async_pwm_tick` only calls `async_sync_switches` when `new_pwm_state != self._local_pwm_state`.
  If the sync is skipped (switch unknown or unavailable), `_local_pwm_state` is already
  updated and the integration never retries. A transient `unavailable` blip leaves the valve
  desynchronised until the next PWM edge. Resync should be level-triggered (compare the
  desired state with the actual state on every tick), not edge-triggered.
- Same pattern in `boiler/relay_backend.py` (`_force_boiler_on`, `_force_boiler_off`,
  `async_emergency_off`) for the boiler switch.
- `async_sync_proportional_valves` also skips `unknown`.

Suggested fix:

- Skip only `unavailable`, and treat `unknown` as "send the command anyway".
- Make `async_sync_switches` run on every `async_pwm_tick`, and call it once at
  startup and whenever a valve switch goes `unavailable`/`unknown` -> anything else.
- Log a WARNING (with the entity id) whenever a command is skipped.

## Root cause 2: unfiltered PID derivative causes ghost boiler demand

The back bedroom was 18.2-18.3°C against a 15°C target (error -3.2°C), so demand should be 0.
Instead its demand flickered 0 -> 17-100% -> 0 and, at 11:04:03, latched at 59.2% for 9 minutes
(11:04:03 - 11:13:03), which is the first boiler run of the day ("Demand 59.2%").

Mechanism:

1. The sensor is quantised to 0.1°C and flickers 18.3 -> 18.2 -> 18.3.
2. `pid.py` line ~236: `derivative = -(Kd * input_diff) / dt` with `dt` in seconds. The autotuner
   computes `Kd = Kp * 0.5 * L` where `L` is the dead time in seconds, so the stored Kd values
   are about 33,000 - 108,000 (back bedroom 33k-60k, kitchen 72k-108k, bathroom 50k-63k,
   living room 40k-45k).
3. A -0.1°C step over 5-10 s gives `+0.1 * 60000 / 8 = +750%`. That beats the proportional term
   of `100 * -3.2 = -320%`, so the output is clamped to 100%.
4. Demand is only recomputed from `_async_on_climate_state_changed` (an event on the zone climate
   entity), not on a timer. The next event, with the same temperature, gives `input_diff = 0` and
   the derivative falls to 0, hence the 0 -> 100 -> 0 flicker.
5. If the last event before a quiet period happens to be a spike, the demand is held until the
   next event: 59.2% for 9 minutes, which turned the boiler on for that duration.
6. The boiler PWM engine, the zone local PWM engine and `plant_diagnostics` all consume that
   latched demand.

The same pipeline also explains the ~94 back-bedroom valve toggles at 10 s intervals between
09:00 and 10:08 (eco preset): every rising edge of the flickering demand restarts the PWM cycle
(`PWMEngine.calculate`: `if prev_demand <= 0 and demand > 0: cycle_start_time = now`).

Suggested fix, in rough order of value:

- Derivative on a smoothed signal: compute the slope over a window (for example the last
  10-15 minutes, least squares), not between two consecutive events.
- Hard-gate D when `abs(error)` is large, or disable D entirely (PI only). Radiator rooms do
  not need D, and the autotuner's `Td = L/2` rule is too aggressive for a 0.1°C sensor.
- Clamp the D contribution (for example +/-10-20% of output).
- Recompute the PID on a fixed timer (for example every 60 s) so derived terms cannot latch.
- Debounce the PWM rising-edge reset so noise in demand does not restart cycles, and add a
  minimum on-time (`min_on` currently 0 for zone switches).
- Re-derive Kd in the autotuner with a cap, for example `Kd <= Kp * 60`.

## Contributing issues

1. **Valve PWM and boiler PWM are independent.** The boiler fires on `max(primary demand)` and
   `relay_backend` treats a zone with no TRVs as "heat path fully open by default"
   (`trvs_ready = True`). For switch-only zones that is not true: each zone's valve runs its own
   900 s PWM cycle with a different phase, so the boiler can fire while the valves are closed.
   Hold the boiler until at least one demanding zone's valve switch is actually `on`, or drive
   the valves from the boiler cycle.
2. **`hvac_action` reflects demand, not reality.** `climate.py` returns `HEATING` whenever
   `demand > 0`, so zones showed "heating" at 21:20-22:00 while their valves were closed.
   Report heating only when the zone's valve/switch is actually on (or add an attribute).
3. **Swallowed exceptions.** `PlantOrchestrator.async_tick` catches everything and logs only
   `str(err)` without a traceback; use `_LOGGER.exception`. The `homeassistant.components.logger` config
   in `configuration.yaml` is commented out, so there is no log file to debug from.
4. **Plant-anomaly sensor is noisy.** `binary_sensor.master_control_plant_anomaly` has been `on`
   since 21:18 for "Front Bedroom overshoot: 16.8°C exceeds target 15.0°C by >1.5°C from residual
   heat", with no recent heating in that zone. It also flapped on/off every 30-60 s between 20:04
   and 20:16. I did not read the overshoot condition in detail, but the room is simply warmer than
   its (low) target, so this looks like a false positive and should require recent heating plus a
   rising trend.
5. **Preset flapping.** 21:15 - 21:48 shows eco -> away -> eco -> comfort -> eco -> comfort within
   seconds; each change recomputes targets and demands. Worth checking the schedule automation
   (`automation.heating_daily_comfort_eco_schedule`) and the routine manager for competing writers.
6. **Pre-existing thermal-model rate sensors.** `*_heating_rate` is 0.0 for most zones after the
   restart (see `THERMAL_MODEL_RATE_FINDINGS.md`); cooling rates now look sane (0.08-0.16°C/h).

## Suggested test cases

- Valve switch state `unknown` at startup, demand > 0: expect a `switch.turn_on` call.
- Valve switch goes `unavailable` then back, with PWM state unchanged: expect resync.
- PID with Kd=60000, error = -3°C, one -0.1°C sample after 8 s: expect output 0.
- PID with a latched spike and no further events: expect demand to decay on the next timer tick.
- Boiler PWM ON while all demanding zones' valve switches are off: expect HOLD (not ON).
