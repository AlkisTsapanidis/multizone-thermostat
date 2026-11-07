"""Telemetry recorder behaviour and log -> replay determinism."""
import json
import math
import os
import random

import pytest

from mzt_replay.clock import ManualClock
from mzt_replay.drivers import constant_rate_ticks, replay_pwm, replay_pwm_from_records
from mzt_replay.loader import load_prod_module

telemetry = load_prod_module("telemetry")


def make(tmp_path, clock=None, **kw):
    clock = clock or ManualClock(start=1000.0)
    rec = telemetry.JsonlRecorder(str(tmp_path), wall_clock=clock.time, mono_clock=clock.monotonic, **kw)
    return rec, clock


def files(tmp_path):
    return sorted(str(p) for p in tmp_path.glob("*.jsonl"))


def test_record_is_buffered_until_flush(tmp_path):
    rec, _ = make(tmp_path)
    rec.record("pid", "living", output=12.5)
    assert files(tmp_path) == []
    assert rec.flush() == 1
    (record,) = list(telemetry.read_records(files(tmp_path)))
    assert record["v"] == telemetry.SCHEMA_VERSION
    assert record["kind"] == "pid" and record["zone"] == "living"
    assert record["data"] == {"output": 12.5}
    assert {"seq", "wall", "mono"} <= record.keys()


def test_sequence_numbers_are_monotonic_and_gapless(tmp_path):
    rec, clock = make(tmp_path)
    for _ in range(20):
        clock.advance(1)
        rec.record("sensor", "a", value=1)
    rec.flush()
    seqs = [r["seq"] for r in telemetry.read_records(files(tmp_path))]
    assert seqs == list(range(1, 21))


def test_non_finite_floats_are_preserved_losslessly_and_strict_json(tmp_path):
    rec, _ = make(tmp_path)
    rec.record("pid", "z", output=math.nan, nested={"x": math.inf, "y": [1.0, -math.inf]},
               label="NaN", ok=1.0)
    rec.flush()
    (path,) = files(tmp_path)
    raw = open(path, encoding="utf-8").read()
    # strict parsers must accept the file: no bare NaN/Infinity literals
    json.loads(raw.splitlines()[0], parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    on_disk = json.loads(raw.splitlines()[0])
    assert on_disk["data"]["output"] == "NaN" and on_disk["data"]["nested"]["x"] == "+Inf"
    assert on_disk["nonfinite"] == {".output": "NaN", ".nested.x": "+Inf", ".nested.y[1]": "-Inf"}
    (record,) = list(telemetry.read_records([path]))
    d = record["data"]
    assert math.isnan(d["output"]) and d["nested"]["x"] == math.inf
    assert d["nested"]["y"] == [1.0, -math.inf]
    assert d["label"] == "NaN"  # a real string is not turned into a float


def test_unserialisable_values_never_raise(tmp_path):
    rec, _ = make(tmp_path)
    rec.record("event", None, obj=object(), s={1, 2})
    assert rec.flush() == 1


def test_buffer_is_bounded_and_counts_drops(tmp_path):
    rec, _ = make(tmp_path, max_buffer=10)
    for i in range(25):
        rec.record("sensor", "a", i=i)
    assert rec.dropped == 15
    rec.flush()
    kept = [r["data"]["i"] for r in telemetry.read_records(files(tmp_path))]
    assert kept == list(range(15, 25))


def test_flush_failure_is_swallowed_and_counted(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    rec = telemetry.JsonlRecorder(str(blocker))
    rec.record("pid", "z", a=1)
    assert rec.flush() == 0
    assert rec.write_errors == 1


def test_torn_final_line_is_skipped(tmp_path):
    rec, _ = make(tmp_path)
    rec.record("pid", "z", a=1)
    rec.flush()
    (path,) = files(tmp_path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"v":1,"seq":2,"kind":"pi')
    assert len(list(telemetry.read_records([path]))) == 1


def test_daily_files_and_size_rotation(tmp_path):
    rec, clock = make(tmp_path, max_file_bytes=300)
    for _ in range(30):
        rec.record("pid", "zone", payload="x" * 40)
        rec.flush()
    assert len(files(tmp_path)) > 1
    day1 = set(files(tmp_path))
    clock.advance(86400)
    rec.record("pid", "zone", a=1)
    rec.flush()
    assert len(set(files(tmp_path)) - day1) == 1


def test_retention_prunes_old_files(tmp_path):
    rec, clock = make(tmp_path, retention_days=3)
    rec.record("pid", "z", a=1)
    rec.flush()
    (old,) = files(tmp_path)
    os.utime(old, (clock.time() - 10 * 86400, clock.time() - 10 * 86400))
    clock.advance(86400)
    rec.record("pid", "z", a=2)
    rec.flush()
    assert old not in files(tmp_path)


def test_total_size_cap_removes_oldest_files_first_but_keeps_newest(tmp_path):
    rec, clock = make(tmp_path, max_file_bytes=200, max_total_bytes=1000)
    for i in range(60):
        rec.record("pid", "zone", payload="x" * 40, i=i)
        rec.flush()
        os.utime(files(tmp_path)[-1], (clock.time() + i, clock.time() + i))
    remaining = files(tmp_path)
    assert sum(os.path.getsize(f) for f in remaining) <= 1000 + 400
    last = max(r["data"]["i"] for r in telemetry.read_records(remaining))
    assert last == 59


def test_null_recorder_is_inert():
    null = telemetry.NullRecorder()
    null.record("pid", "z", a=1)
    assert null.flush() == 0 and null.enabled is False


@pytest.mark.parametrize("seed", range(5))
def test_pwm_log_replays_to_identical_decisions(tmp_path, seed):
    rng = random.Random(seed)
    levels = [0.0, 0.5, 3.0, 25.0, 50.0, 97.0, 99.0, 100.0]
    level = 20.0
    ticks = []
    for i in range(1, 2000):
        if rng.random() < 0.05:
            level = rng.choice(levels) if rng.random() < 0.5 else rng.uniform(0, 100)
        ticks.append((i * 10.0, level))

    rec, clock = make(tmp_path, clock=ManualClock(start=0.0))
    live = replay_pwm(ticks, min_on=300, min_off=300, recorder=rec, zone="z", clock=clock)
    rec.flush()

    replayed = replay_pwm_from_records(telemetry.read_records(files(tmp_path)))
    assert len(replayed) == len(live)
    for (record, state), tick in zip(replayed, live):
        assert state == tick.state == record["data"]["state"], f"diverged at t={tick.t}"
