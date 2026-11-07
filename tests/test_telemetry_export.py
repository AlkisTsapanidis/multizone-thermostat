"""CSV export and summary tooling for telemetry logs."""
import csv
import io
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from custom_components.multizone_thermostat import telemetry_export as ex
from mzt_replay.clock import ManualClock
from mzt_replay.loader import load_prod_module

telemetry = load_prod_module("telemetry")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def logdir(tmp_path):
    clock = ManualClock(start=1000.0)
    rec = telemetry.JsonlRecorder(str(tmp_path), wall_clock=clock.time, mono_clock=clock.monotonic)
    for i in range(6):
        clock.advance(60)
        rec.record("pid", "climate.a", input=19.0 + i / 10, demand=math.nan if i == 2 else 10.0 * i,
                   nested={"x": math.inf if i == 3 else 1.0}, series=[1, 2])
        rec.record("pwm", "boiler", demand=50.0, state=i % 2 == 0, min_on=120, min_off=120)
        rec.record("sync", "Zone", action="turn_on", switch="switch.v")
    rec.flush()
    return tmp_path


def _read(logdir, **kw):
    out = io.StringIO()
    n = ex.export_csv(ex.load([str(logdir)]), out, **kw)
    out.seek(0)
    return n, list(csv.DictReader(out))


def test_csv_flattens_data_and_keeps_non_finite_visible(logdir):
    n, rows = _read(logdir, kind="pid")
    assert n == 6 and len(rows) == 6
    assert rows[0]["zone"] == "climate.a" and rows[0]["kind"] == "pid"
    assert rows[0]["wall_iso"].endswith("+00:00")
    assert rows[2]["demand"] == "NaN" and rows[3]["nested.x"] == "+Inf"
    assert json.loads(rows[0]["series"]) == [1, 2]
    assert float(rows[1]["input"]) == pytest.approx(19.1)


def test_csv_filters_by_kind_zone_and_time(logdir):
    assert _read(logdir, kind="pwm")[0] == 6
    assert _read(logdir, zone="Zone")[0] == 6
    assert _read(logdir, kind="pid", zone="boiler")[0] == 0
    walls = sorted(r["wall"] for r in _read(logdir, kind="pid")[1])
    n, _ = _read(logdir, kind="pid", since=float(walls[2]), until=float(walls[3]))
    assert n == 2


def test_summary_reports_cycling_and_violations(logdir):
    text = ex.summarize(ex.load([str(logdir)]))
    assert "records: 18" in text
    assert "non-finite values in 2" in text
    assert "boiler/pwm: " in text and "transitions" in text
    assert "shorter than configured minimum" in text  # 60 s runs against a 120 s minimum
    assert "turn_on" in text


def test_cli_end_to_end(logdir, tmp_path):
    out = tmp_path / "pid.csv"
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts" / "mzt_log.py"), *a],  # noqa: E731
                                    capture_output=True, text=True)
    r = run("csv", str(logdir), "--kind", "pid", "-o", str(out))
    assert r.returncode == 0, r.stderr
    assert len(list(csv.DictReader(out.open()))) == 6
    r = run("summary", str(logdir))
    assert r.returncode == 0 and "records: 18" in r.stdout
