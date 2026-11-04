# Thermal model: heating/cooling rate findings

Module: `ThermalObserver` in `file_thermal_model.py`.
Zone checked: `climate.multizone_thermostat_living_room`, sensor `sensor.living_room_temperature`.
Data window: 2026-11-01 22:00 UTC to 2026-11-03 (HA history, ~1780 sensor states).

## Symptom

The UI shows the room losing 1°C in ~0.7 h (about 1.4°C/h). The steepest real gradient on the sensor graph is ~0.7°C/h, and the typical overnight/afternoon cooling is much lower.

## Cross-check against real data

- Real cooling, 10:03 (21.5°C) to 17:24 (18.6°C) on 2026-11-02: 2.9°C over 7.4 h, about **0.4°C/h**.
- Replaying `update()` over the recorded sensor and `hvac_action` history gives:
  - 130 cooling samples, median ~3°C/h, p90 ~7.8°C/h, max ~9.7°C/h.
  - Final cooling EMA about **5°C/h**, i.e. roughly 10x the real value.
- Typical offending sample: `dT -0.1°C over 0.62 min -> 9.7°C/h`.
- Slow real drops, e.g. `-0.1°C over 23 min -> 0.26°C/h`, are rare in the samples.

## Root causes

1. **Two-point instantaneous rate on a quantised, flickering sensor.** The sensor reports in 0.1°C steps and flickers (17.3, 17.4, 17.3, ...). The rate is computed between the last baseline and the current reading as soon as `abs(delta) >= 0.1`.
2. **Baseline reset on any change >= threshold, including the ignored direction.** An upward flicker resets `_last_temp`/`_last_temp_time` but is never counted in the cooling branch (`delta_temp < 0` is required). The next downward 0.1°C step is then timed from that reset, often 30-60 s later, giving a huge rate.
3. **Selection bias in the EMA.** Fast flicker-driven drops are far more frequent than genuine slow drops, so the average is dominated by them. The only guard is `> 0.01 h` (36 s), which still admits ~40 s samples (~9°C/h).
4. **Heating rate has the same flaw**, so `get_weather_multiplier()` (`cooling_rate / heating_rate`) inherits the error.
5. **`hvac_action` flaps.** Around 2026-11-02 02:43-02:51 the zone toggles heating/idle every few seconds (PWM/valve behaviour). Each toggle resets the baseline and discards data. Heating periods of seconds are useless for a rate.
6. Minor: `time.monotonic()` is used for sample time, so the learned data cannot be replayed from recorded history, and the state is not tied to sensor timestamps (sensor `last_changed` is more accurate than the update-call time).

## Suggested fix

- Compute rates over a **window**, not between consecutive changes:
  - Require a minimum span (e.g. >= 20-30 min) and a minimum net change (e.g. >= 0.3°C) before producing a sample.
  - Or least-squares fit temperature vs. time over a continuous idle (cooling) or heating period and use the slope.
- **Do not reset the baseline** on upward or downward flicker. Reset only when the heating state genuinely changes (after debounce) or when the window completes.
- **Debounce `is_heating`**: require the new state to hold for a few minutes before treating it as a transition. Ignore heating periods shorter than a minimum duration.
- Replace the single-step EMA with an EMA (or median / trimmed mean) over **per-period slopes**.
- Consider separating the cooling rate by outdoor/indoor delta (cooling is roughly proportional to `T_room - T_outdoor`) so the learned value is comparable across conditions.
- Use the sensor's own timestamp for sample times when available.

## Validation approach

1. Pull history for the sensor and the climate entity (HA `/api/history/period/...`).
2. Replay it through the observer with synthetic timestamps instead of `time.monotonic()`.
3. Expected result for this room on 2026-11-02: cooling ~0.3-0.5°C/h, never above ~1°C/h.
4. Add a unit test with a flickering 0.1°C-step series (a slow decline with +/-0.1 noise) and assert the learned cooling rate stays near the true slope.
