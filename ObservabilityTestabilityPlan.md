# Observability & Testability Plan: Before Any Control Code Changes

**Date:** November 6, 2026  
**Status:** In progress; core telemetry, replay, and known-defect tests implemented, with important gaps remaining

**Approach:** Comprehensive logging, test data infrastructure, and replay capability BEFORE touching any numerical code

## Progress update

The implementation differs from the illustrative classes and file names in this proposal.
For the actual record format, commands, coverage and limitations, see
[TelemetryAndReplay.md](./docs/TelemetryAndReplay.md).
For deployment checks and real-world data collection, see
[DeploymentTestPlan.md](./DeploymentTestPlan.md).

- [x] Bounded JSONL telemetry with wall and monotonic timestamps, retention, and lossless
  `NaN`/`+Inf`/`-Inf` handling. PID, valve PWM, relay boiler, switch sync and estimator
  events are instrumented, along with plant aggregation/overrides, OpenTherm updates and
  proportional-valve decisions; this is **not** complete coverage of all control inputs and decisions.
- [x] Offline CSV export and summary CLI; optional diagnostic sensor entities default to off.
- [x] Synthetic scenarios and replay drivers for PWM, PID and estimators. PWM and PID
  telemetry round trips are tested using generated records; PID replay requires a clean-state
  boundary. A frozen PID reference supports differential tests.
- [x] Known-defect tests for PID, PWM, estimators, switch sync and closed-loop valve/boiler
  scenarios; strict expected failures can be run as ordinary failing tests with `--show-red`.
- [x] Added adapted ASHRAE Addendum y section-referenced tests for selected plant decisions,
  OpenTherm mapping and proportional-valve command observation; these do not establish conformance.
- [x] Added selected sensor and configuration events, aggregation inputs, frost/anti-seize
  lifecycle observations, record replay for estimator epochs, aggregation, individual plant
  decisions, complete frost scans, selected anti-seize failures and cancellations, linked
  proportional-valve decisions, delayed relay task lifecycles and rechecks, and OpenTherm
  updates/commands, and an orchestrated synthetic relay-plant replay.
  See the telemetry guide for boundaries and omissions.
- [x] Linked sequential recorded plant decisions to a stateful real relay backend for updates
  without valve delays and master-off operations, including switch-service errors and cancellation,
  checking recorded inputs, commands and backend state. One non-overlapping delayed cycle can
  also replay through the real delayed task and relay command; consecutive non-overlapping
  cycles are checked for continuity. Sequential updates during a single pending delay can also
  share a backend and task, including a final update that cancels an already-waiting task;
  otherwise, cancellation during a wait can be injected with its source marked unknown.
  Consecutive waiting cycles are independently replayed with backend and PWM continuity checks.
  Switch-path decisions are independently replayed. Full asynchronous recorded-plant replay remains outstanding.
- [x] Captured schedule evaluation inputs and before/after overrides, and replayed individual
  evaluations including decisions based on captured smart-return plans. Plan calculations replay
  from effective raw inputs and can be linked to evaluations by identifier. Sequential
  evaluation replay checks lifecycle and state continuity, including captured manual set/clear
  mutations; other override and configuration lifecycles remain open.
- [x] The existing GitHub Actions workflow runs the test suite. This records the configured
  CI step, not a claim that a particular remote run passed.
- [ ] Complete instrumentation, replay and ground-truth coverage for every numerical and
  control path, including real installation telemetry and full plant orchestration.
- [x] Record the PWM engine's actual calculation clock read without changing its numerical
  calculation; boundary tests cover valve and boiler records. Older records do not contain it.
- [ ] Validate exact replay with captured real installation telemetry.
- [ ] Validate on captured real-world logs and known datasets before changing numerical
  control behavior or changing the default estimator. The legacy estimator remains the default.

The phase descriptions and code snippets below are design proposals, not the current API.
Week labels are historical sequencing suggestions, not deadlines.

---

## Philosophy

> "You cannot fix what you cannot see, and you cannot trust a fix without being able to replay the failure."

This plan prioritizes:
1. **Complete visibility** into what the system is doing at every decision point
2. **Deterministic replay** of real-world scenarios and synthetic test cases
3. **Test-first approach:** Write tests that fail on current behavior, then fix the behavior
4. **No changes to control logic** until we can observe and replay the broken behavior

---

## Phase 1: Comprehensive Logging Infrastructure

### 1.1 Control Decision Logging

**Goal:** Log every input, calculation, and decision in one place with timestamps and context.

**Create:** `custom_components/multizone_thermostat/diagnostics/control_logger.py`

```python
"""
Comprehensive logging of control decisions for replay and debugging.
Logs all PID inputs/outputs, PWM decisions, sync events, etc. in JSON format.
"""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Optional
import json
import logging
from enum import Enum

_LOGGER = logging.getLogger(__name__)

class ControlEventType(Enum):
    """Types of control events to log."""
    PID_INPUT = "pid_input"
    PID_OUTPUT = "pid_output"
    PWM_TICK = "pwm_tick"
    SYNC_COMMAND = "sync_command"
    BOILER_DECISION = "boiler_decision"
    VALVE_DECISION = "valve_decision"
    RATE_UPDATE = "rate_update"
    THERMAL_MODEL_UPDATE = "thermal_model_update"
    DEMAND_AGGREGATION = "demand_aggregation"
    TRV_STATE_CHANGE = "trv_state_change"
    SENSOR_READ = "sensor_read"
    ANOMALY_DETECTED = "anomaly_detected"

@dataclass
class ControlEvent:
    """A single control system event with full context."""
    timestamp: float  # time.time() - wall clock
    event_type: ControlEventType
    zone_id: str
    wall_time: str  # ISO format for human readability
    
    # Inputs
    inputs: dict[str, Any]  # temperature, demand, mode, etc.
    
    # Decision point
    decision: str  # "turn_on", "increase_demand", etc.
    reason: str  # Why this decision was made
    
    # Outputs
    outputs: dict[str, Any]  # new_state, command_sent, etc.
    
    # Metadata
    duration_ms: Optional[float] = None  # How long the calculation took
    error: Optional[str] = None  # If something went wrong

class ControlLogger:
    """Centralized logging for all control decisions."""
    
    def __init__(self, hass, storage_path: str = ".mzt_control_log"):
        self.hass = hass
        self.storage_path = storage_path
        self.buffer: list[ControlEvent] = []
        self.max_buffer = 10000  # Keep last 10k events in memory
        self.log_file = None
        
        # File rotation: daily logs
        self._open_daily_log()
    
    def _open_daily_log(self):
        """Open today's log file."""
        import os
        from datetime import date
        
        os.makedirs(self.storage_path, exist_ok=True)
        today = date.today().isoformat()
        self.log_file = os.path.join(self.storage_path, f"control_{today}.jsonl")
    
    def log_event(self, event: ControlEvent) -> None:
        """Log a control event to buffer and disk."""
        self.buffer.append(event)
        if len(self.buffer) > self.max_buffer:
            self.buffer.pop(0)
        
        # Write to disk
        try:
            with open(self.log_file, "a") as f:
                event_dict = asdict(event)
                event_dict["event_type"] = event.event_type.value
                f.write(json.dumps(event_dict) + "\n")
        except Exception as e:
            _LOGGER.error("Failed to write control log: %s", e)
    
    def get_zone_events(self, zone_id: str, event_type: Optional[ControlEventType] = None, 
                       limit: int = 100) -> list[ControlEvent]:
        """Retrieve recent events for a zone."""
        events = [e for e in reversed(self.buffer) if e.zone_id == zone_id]
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return events[:limit]
    
    def export_for_replay(self, zone_id: str, start_time: float, end_time: float, 
                         output_file: str) -> None:
        """Export events between two timestamps for replay analysis."""
        events = [e for e in self.buffer 
                 if e.zone_id == zone_id and start_time <= e.timestamp <= end_time]
        
        with open(output_file, "w") as f:
            for event in events:
                event_dict = asdict(event)
                event_dict["event_type"] = event.event_type.value
                f.write(json.dumps(event_dict) + "\n")
        
        _LOGGER.info("Exported %d events to %s", len(events), output_file)
    
    def clear_old_logs(self, days_to_keep: int = 7) -> None:
        """Remove log files older than N days."""
        import os
        from datetime import datetime, timedelta
        
        cutoff = datetime.now() - timedelta(days=days_to_keep)
        for filename in os.listdir(self.storage_path):
            if filename.startswith("control_") and filename.endswith(".jsonl"):
                filepath = os.path.join(self.storage_path, filename)
                mtime = os.path.getmtime(filepath)
                if datetime.fromtimestamp(mtime) < cutoff:
                    os.remove(filepath)
                    _LOGGER.info("Deleted old log: %s", filename)
```

### 1.2 Instrument Existing Control Modules

**Files to instrument:**
- `pid.py` → log P, I, D components; input; output
- `pwm_engine.py` → log demand; calculated time_on/time_off; state transition
- `thermal_model.py` → log measured rate; EMA update; weather multiplier calculation
- `demand_aggregator.py` → log all zone demands; aggregation result
- `sync_manager.py` → log command sent; state before/after
- `relay_backend.py` → log boiler decision; reason; min_cycle hold time

**Pattern:** At each decision point, create a ControlEvent with:
- **inputs:** What we observed
- **decision:** What we decided
- **reason:** Why (in plain English)
- **outputs:** What we commanded

**Example (PID):**

```python
class PID:
    def calc(self, input_val, set_point, ...):
        # ... existing calculation ...
        
        # Log the decision
        if self._control_logger:
            event = ControlEvent(
                timestamp=time.time(),
                event_type=ControlEventType.PID_OUTPUT,
                zone_id=self.zone_id,
                wall_time=datetime.now(timezone.utc).isoformat(),
                inputs={
                    "temperature": input_val,
                    "setpoint": set_point,
                    "error": self._error,
                    "dt": self._dt,
                },
                decision="output",
                reason=f"P={self._proportional:.1f} + I={self._integral:.1f} + D={self._derivative:.1f}",
                outputs={
                    "output": self._output,
                    "proportional": self._proportional,
                    "integral": self._integral,
                    "derivative": self._derivative,
                    "slope_available": slope is not None,
                },
                duration_ms=(time.time() - calc_start) * 1000,
            )
            self._control_logger.log_event(event)
        
        return self._output, True
```

---

## Phase 2: Replay Infrastructure

### 2.1 Create Replay Engine

**Create:** `tests/replay_engine.py`

```python
"""
Deterministic replay of control decisions from logged data.
Feeds historical inputs through control modules, compares outputs to logged values.
"""

from dataclasses import dataclass
from typing import Any, Callable, Optional
import json
from datetime import datetime

@dataclass
class ReplayEvent:
    """An event to replay."""
    timestamp: float
    event_type: str
    zone_id: str
    inputs: dict[str, Any]
    expected_output: dict[str, Any]
    reason: str

class ReplayEngine:
    """Replays recorded control events against current (or modified) code."""
    
    def __init__(self):
        self.events: list[ReplayEvent] = []
        self.results = []
    
    def load_from_jsonl(self, filepath: str) -> None:
        """Load events from exported control log."""
        with open(filepath, "r") as f:
            for line in f:
                data = json.loads(line)
                self.events.append(ReplayEvent(
                    timestamp=data["timestamp"],
                    event_type=data["event_type"],
                    zone_id=data["zone_id"],
                    inputs=data["inputs"],
                    expected_output=data["outputs"],
                    reason=data.get("reason", ""),
                ))
    
    def replay(self, callback: Callable[[ReplayEvent], dict[str, Any]]) -> list[dict]:
        """
        Replay each event through a control function.
        
        callback(event) should accept a ReplayEvent and return the actual output dict.
        """
        results = []
        
        for event in self.events:
            try:
                actual_output = callback(event)
                
                # Compare expected vs actual
                matches = self._compare_outputs(event.expected_output, actual_output)
                
                result = {
                    "event_type": event.event_type,
                    "timestamp": event.timestamp,
                    "zone_id": event.zone_id,
                    "input_summary": str(event.inputs),
                    "expected": event.expected_output,
                    "actual": actual_output,
                    "matches": matches,
                    "delta": self._compute_delta(event.expected_output, actual_output),
                }
                
                results.append(result)
            except Exception as e:
                results.append({
                    "event_type": event.event_type,
                    "timestamp": event.timestamp,
                    "error": str(e),
                })
        
        self.results = results
        return results
    
    def _compare_outputs(self, expected: dict, actual: dict) -> bool:
        """Compare two output dicts, allowing for small numeric differences."""
        for key in expected:
            if key not in actual:
                return False
            
            exp_val = expected[key]
            act_val = actual[key]
            
            # Numeric comparison with tolerance
            if isinstance(exp_val, float) and isinstance(act_val, float):
                if abs(exp_val - act_val) > 0.01:  # 1% tolerance
                    return False
            elif exp_val != act_val:
                return False
        
        return True
    
    def _compute_delta(self, expected: dict, actual: dict) -> dict:
        """Compute differences between expected and actual."""
        deltas = {}
        for key in expected:
            if key in actual:
                if isinstance(expected[key], float) and isinstance(actual[key], float):
                    deltas[key] = actual[key] - expected[key]
        return deltas
    
    def print_summary(self) -> None:
        """Print replay results summary."""
        if not self.results:
            print("No results yet. Run replay() first.")
            return
        
        total = len(self.results)
        matches = sum(1 for r in self.results if r.get("matches", False))
        errors = sum(1 for r in self.results if "error" in r)
        
        print(f"\n=== Replay Summary ===")
        print(f"Total events: {total}")
        print(f"Matches: {matches}/{total} ({100*matches/total:.1f}%)")
        print(f"Errors: {errors}")
        
        if errors > 0:
            print(f"\nFirst error:")
            for r in self.results:
                if "error" in r:
                    print(f"  Event: {r['event_type']} at {r['timestamp']}")
                    print(f"  Error: {r['error']}")
                    break
        
        # Show zones with mismatches
        mismatches = [r for r in self.results if not r.get("matches", True)]
        if mismatches:
            print(f"\nFirst mismatch:")
            r = mismatches[0]
            print(f"  Event: {r['event_type']}")
            print(f"  Expected: {r['expected']}")
            print(f"  Actual: {r['actual']}")
            print(f"  Delta: {r['delta']}")
```

### 2.2 Add Home Assistant Service for Export

**In `services.yaml`:**

```yaml
export_control_log:
  name: "Export Control Log"
  description: "Export control events for a zone to a file for replay analysis"
  fields:
    zone_id:
      name: "Zone ID"
      selector:
        text:
    start_time:
      name: "Start Time (ISO format)"
      selector:
        text:
    end_time:
      name: "End Time (ISO format)"
      selector:
        text:
    output_file:
      name: "Output File Path"
      selector:
        text:
```

**In `__init__.py`:**

```python
async def handle_export_control_log(call):
    zone_id = call.data.get("zone_id")
    start_time = datetime.fromisoformat(call.data.get("start_time"))
    end_time = datetime.fromisoformat(call.data.get("end_time"))
    output_file = call.data.get("output_file")
    
    coordinator.control_logger.export_for_replay(
        zone_id,
        start_time.timestamp(),
        end_time.timestamp(),
        output_file
    )
```

---

## Phase 3: Comprehensive Test Suite (Starts Red)

### 3.1 Test Structure

**Create:** `tests/test_control_replay.py`

```python
"""
Test suite that replays real-world and synthetic scenarios.
Tests start RED (failing) on current code, then turn GREEN as fixes are applied.
"""

import pytest
from pathlib import Path
from replay_engine import ReplayEngine

class TestPIDDerivativeNoise:
    """Tests for PID derivative handling on noisy sensors."""
    
    def test_derivative_does_not_spike_on_sensor_flicker(self):
        """
        FAILING TEST (red):
        Sensor flickers 18.3 -> 18.2 -> 18.3 °C with huge Kd=60000.
        Expected: Output stays < 50% (no derivative spike).
        Actual (current code): Output = 100% (derivative spike).
        """
        engine = ReplayEngine()
        engine.load_from_jsonl("data/recorded_logs/sensor_flicker_2026-11-02.jsonl")
        
        results = engine.replay(lambda event: {
            "output": pid_instance.calc(
                event.inputs["temperature"],
                event.inputs["setpoint"],
            )[0]
        })
        
        # Assert that no output exceeds expected threshold
        for result in results:
            assert result["actual"]["output"] < 50, \
                f"Derivative spike detected: {result}"
    
    def test_cooling_rate_realistic_on_quantized_sensor(self):
        """
        FAILING TEST:
        Replaying history where sensor reports in 0.1°C steps with random flicker.
        Expected: Learned cooling rate 0.3-0.8 °C/hour.
        Actual (current code): 3-8 °C/hour (10x too high).
        """
        engine = ReplayEngine()
        engine.load_from_jsonl("data/recorded_logs/overnight_cooling_2026-11-02.jsonl")
        
        estimator = LegacyEmaEstimator("living_room")
        
        results = engine.replay(lambda event: {
            "cooling_rate": (
                estimator.update(...),
                estimator.cooling_rate
            )[1]
        })
        
        final_rate = results[-1]["actual"]["cooling_rate"]
        assert 0.3 <= final_rate <= 0.8, \
            f"Cooling rate unrealistic: {final_rate} (expected 0.3-0.8)"
    
    def test_pwm_cycle_no_division_by_zero(self):
        """
        PASSING TEST (green from start):
        Various demand values including edge cases.
        Expected: No exception raised; always returns valid bool.
        """
        engine = PWMEngine(pwm_interval=900, min_on=60, min_off=60)
        
        # Test edge cases
        demands = [0.0, 0.01, 0.1, 0.5, 1.0, 50.0, 99.0, 100.0]
        
        for demand in demands:
            result = engine.calculate(demand)
            assert isinstance(result, bool), f"PWM returned non-bool for demand {demand}"
    
    def test_valve_sync_resilient_to_unavailability(self):
        """
        FAILING TEST:
        Valve unavailable for 10 seconds during PWM cycle.
        Expected: Valve syncs within 5s of becoming available.
        Actual (current code): Syncs take 60+ seconds (state out of date).
        """
        engine = ReplayEngine()
        engine.load_from_jsonl("data/recorded_logs/valve_unavailable_recovery.jsonl")
        
        results = engine.replay(lambda event: {
            "sync_time": measure_sync_latency(event)
        })
        
        for result in results:
            sync_time = result["actual"]["sync_time"]
            assert sync_time < 5000, \
                f"Sync took too long: {sync_time}ms (expected < 5000ms)"
```

### 3.2 Synthetic Test Data

**Create:** `tests/synthetic_data_generator.py`

```python
"""
Generate synthetic scenarios that trigger known bugs.
"""

from dataclasses import dataclass
from typing import Callable
import json

@dataclass
class SyntheticScenario:
    """A scenario to test."""
    name: str
    description: str
    duration_seconds: int
    events: list[dict]  # Sensor readings, state changes, etc.

def generate_sensor_flicker_scenario() -> SyntheticScenario:
    """
    Sensor reading at setpoint with quantization noise.
    Expected: No unnecessary boiler cycling.
    Actual (bug): Boiler turns on due to D-term spike.
    """
    events = []
    setpoint = 20.0
    base_temp = 20.0
    
    for t in range(0, 600, 5):  # 10-minute cycle, sample every 5 seconds
        # Oscillate around setpoint by ±0.05°C (sensor jitter), quantized to 0.1°C
        angle = (t / 10.0) * 3.14159
        noise = 0.05 * sin(angle)
        temp = round((base_temp + noise) * 10) / 10  # Quantize to 0.1°C
        
        events.append({
            "timestamp": t,
            "event_type": "sensor_read",
            "zone_id": "living_room",
            "inputs": {
                "temperature": temp,
                "setpoint": setpoint,
                "is_heating": False,
            },
        })
    
    return SyntheticScenario(
        name="sensor_flicker",
        description="Sensor oscillating at setpoint with quantization noise",
        duration_seconds=600,
        events=events,
    )

def generate_valve_unavailability_scenario() -> SyntheticScenario:
    """
    Valve becomes unavailable, then available again.
    Expected: Sync restores state immediately.
    Actual (bug): State stays out of sync.
    """
    events = []
    
    # Valve available, demand 60%, ON
    for t in range(0, 10, 1):
        events.append({
            "timestamp": t,
            "event_type": "pwm_tick",
            "zone_id": "bedroom",
            "inputs": {"demand": 60.0},
            "expected_output": {"state": "on"},
        })
    
    # Valve unavailable
    for t in range(10, 20, 1):
        events.append({
            "timestamp": t,
            "event_type": "valve_state",
            "zone_id": "bedroom",
            "inputs": {"state": "unavailable"},
        })
    
    # Demand changes while unavailable
    for t in range(20, 30, 1):
        events.append({
            "timestamp": t,
            "event_type": "pwm_tick",
            "zone_id": "bedroom",
            "inputs": {"demand": 20.0},
            "expected_output": {"state": "off"},
        })
    
    # Valve available again
    for t in range(30, 40, 1):
        events.append({
            "timestamp": t,
            "event_type": "valve_state",
            "zone_id": "bedroom",
            "inputs": {"state": "available"},
        })
    
    return SyntheticScenario(
        name="valve_unavailable_recovery",
        description="Valve recovery from unavailable state",
        duration_seconds=40,
        events=events,
    )
```

### 3.3 Test Discovery & CI Integration

**In `pytest.ini`:**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
markers =
    replay: Test data replay scenarios
    synthetic: Synthetic generated test data
    red: Test expected to fail on current code (will turn green on fix)
    slow: Slow integration tests
```

**GitHub Actions (`.github/workflows/test_replay.yml`):**

```yaml
name: Replay Tests

on: [push, pull_request]

jobs:
  replay:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: "3.9"
      
      - name: Install dependencies
        run: pip install -r requirements-test.txt
      
      - name: Run replay tests (allow failures on 'red' tests)
        run: pytest tests/test_control_replay.py -v --tb=short
        continue-on-error: true
      
      - name: Report results
        run: |
          echo "### Replay Test Results" >> $GITHUB_STEP_SUMMARY
          pytest tests/test_control_replay.py --co -q >> $GITHUB_STEP_SUMMARY
```

---

## Phase 4: Visualization & Analysis Dashboard

### 4.1 Create Web Dashboard

**Create:** `www/control_dashboard.html`

Provides real-time and historical view of:
- Zone temperatures vs setpoints (graph)
- PID component breakdown (P, I, D over time)
- PWM state and cycle duration
- Boiler on/off cycles with reasons
- Valve sync events
- Rate estimator convergence

**Endpoint:** `/api/custom_components/multizone_thermostat/control_data`

```python
@app.get("/api/custom_components/multizone_thermostat/control_data")
async def get_control_data(zone_id: str, hours: int = 24):
    """Get control events for dashboard."""
    return {
        "events": coordinator.control_logger.get_zone_events(zone_id, limit=1000),
        "zone_id": zone_id,
        "time_range_hours": hours,
    }
```

### 4.2 Data Export for Analysis

**Create:** Script to generate analysis-ready CSV/Parquet files

```python
# Export control events as CSV for analysis in pandas/R/Excel
def export_as_csv(zone_id: str, output_file: str):
    events = coordinator.control_logger.get_zone_events(zone_id, limit=100000)
    
    rows = []
    for event in events:
        row = {
            "timestamp": event.timestamp,
            "event_type": event.event_type.value,
            "wall_time": event.wall_time,
            **flatten_dict(event.inputs),
            **{f"out_{k}": v for k, v in flatten_dict(event.outputs).items()},
        }
        rows.append(row)
    
    df = pd.DataFrame(rows)
    df.to_csv(output_file, index=False)
```

---

## Phase 5: Implementation Roadmap

### Week 1: Logging Infrastructure
- [x] Implement bounded control-event logging using `JsonlRecorder` and JSONL records
- [x] Instrument PID calculations via the wrapper and zone controller
- [x] Instrument valve and relay boiler PWM decisions at their callers
- [x] Integrate the recorder lifecycle with the Home Assistant coordinator
- [ ] Test on real system; capture 24+ hours of logs

**Partial:** Selected sensor and configuration events and frost/anti-seize internals are now
recorded, but not every raw event, configuration value or override transition. Full stateful plant
replay from real recordings remains outstanding. Independent physical feedback is deferred until suitable
hardware and testing are available.
**Deliverable outstanding:** First verified real log file with control decisions.

### Week 2: Replay Engine & Export
- [x] Implement component replay drivers and a manual clock in `tests/mzt_replay/`
- [ ] Add Home Assistant service to export logs
- [x] Add an offline CSV and summary CLI
- [x] Verify PWM and PID outputs against generated production telemetry records
- [ ] Test telemetry replay on captured real installation data and verify outputs
- [ ] Support reliable replay of all relevant control paths and arbitrary time windows

**Partial:** PID replay starts at a clean-state boundary; an arbitrary one-hour window is not
guaranteed to contain one. A Home Assistant export service is not implemented.

### Week 3: Test Suite (Red Tests)
- [x] Create seeded synthetic room and sensor scenarios
- [x] Write known-defect tests for sensor flicker, rate estimation, PWM, sync and plant scenarios
- [x] Document the covered defects and their expected versus observed behavior
- [x] Configure CI to run the test suite
- [ ] Extend coverage to the remaining serious failures and validate against known datasets

**Partial:** Known defects are strict expected failures by default and fail visibly with
`--show-red`; the existing tests do not establish comprehensive coverage.

### Week 4: Dashboard & Analysis
- [ ] Build web dashboard for control data visualization
- [x] Add CSV export for analysis
- [x] Document record fields, export, replay and known defects
- [ ] Develop complete guidance for interpreting real installation logs
- [ ] Train on interpreting results

**Partial:** Optional diagnostic sensors expose the latest records in Home Assistant; a
historical visualization dashboard is not implemented.

---

## Key Metrics

Once observability is in place, track:

| Metric | How to Measure | Baseline | Target |
|--------|----------------|----------|--------|
| **Replay accuracy** | % of events where replay output matches logged | N/A | 100% |
| **Test coverage** | # of control paths covered by tests | 0 | 50+ |
| **Log verbosity** | MB/day of control logs | N/A | < 50 MB (7 days retention) |
| **Dashboard latency** | Time to load 24h of data | N/A | < 1 second |
| **Red test count** | Known bugs with failing tests | 0 | 5-10 |

---

## Benefits Before Touching Any Code

1. **See the problems:** Logs show exactly when/how things go wrong
2. **Validate fixes:** Replay same data, verify output changes correctly
3. **Prevent regressions:** Red tests catch new bugs immediately
4. **Build confidence:** Dashboard shows "before" vs "after" improvement
5. **Share findings:** Non-developers can understand issues from logs
6. **Debug faster:** When users report problems, ask for exported logs

---

## Next Steps

1. Complete missing instrumentation and extend replay and test coverage.
2. Capture real-world telemetry and validate replay and defect tests against it and known datasets.
3. Address the documented gaps in full plant orchestration, delayed valve behavior and analysis.
4. Only then assess numerical fixes against the existing reference and red tests.

**You won't touch any PID, PWM, or rate estimation code until you can replay the failure.**

This approach trades short-term speed for long-term confidence and correctness.
