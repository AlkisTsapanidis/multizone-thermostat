"""Offline tools for telemetry logs: CSV export and a quick health summary. Pure standard library.

CLI: `python scripts/mzt_log.py csv|summary <log files or directory> [...]` (see scripts/mzt_log.py).
"""
from __future__ import annotations

import csv
import glob
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, TextIO

try:
    from .telemetry import read_records
except ImportError:  # loaded by path in tests / scripts
    from telemetry import read_records

ENVELOPE = ["seq", "wall_iso", "wall", "mono", "zone", "kind"]


def expand_paths(paths: Iterable[str]) -> list[str]:
    """Files as given; directories expand to their mzt_*.jsonl files, oldest name first."""
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(glob.glob(os.path.join(p, "*.jsonl")))
        else:
            out += sorted(glob.glob(p)) or [p]
    return out


def _flatten(value: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(value, (list, tuple)):
        yield prefix, json.dumps(value, default=repr)
    else:
        yield prefix, value


def _cell(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "+Inf" if value > 0 else "-Inf"
    return "" if value is None else value


def _wall_iso(wall: Any) -> str:
    try:
        return datetime.fromtimestamp(float(wall), tz=timezone.utc).isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def export_csv(
    records: Iterable[dict[str, Any]],
    out: TextIO,
    *,
    kind: str | None = None,
    zone: str | None = None,
    since: float | None = None,
    until: float | None = None,
) -> int:
    """Write matching records as CSV (one row per record, `data` flattened to `a.b` columns).

    Columns are the union over the selected records, so export one `kind` at a time for clean tables
    (mixed kinds give a wide, sparse table). NaN/+Inf/-Inf are written as those literals so they
    remain visible in spreadsheets. Returns the number of rows written.
    """
    rows: list[dict[str, Any]] = []
    columns: dict[str, None] = dict.fromkeys(ENVELOPE)
    for rec in records:
        if kind and rec.get("kind") != kind:
            continue
        if zone and rec.get("zone") != zone:
            continue
        wall = rec.get("wall")
        if since is not None and (wall is None or wall < since):
            continue
        if until is not None and (wall is None or wall > until):
            continue
        row: dict[str, Any] = {
            "seq": rec.get("seq"), "wall_iso": _wall_iso(wall), "wall": wall, "mono": rec.get("mono"),
            "zone": rec.get("zone"), "kind": rec.get("kind"),
        }
        for key, val in _flatten(rec.get("data") or {}):
            row[key] = val
            columns.setdefault(key)
        rows.append(row)
    writer = csv.DictWriter(out, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(v) for k, v in row.items()})
    return len(rows)


def _runs(points: list[tuple[float, bool]]) -> list[float]:
    """Durations (s) of each completed ON/OFF run from (mono, state) samples at every tick."""
    runs: list[float] = []
    start, state = None, None
    for t, s in points:
        if state is None:
            start, state = t, s
        elif s != state:
            runs.append(t - start)
            start, state = t, s
    return runs


def summarize(records: Iterable[dict[str, Any]]) -> str:
    """Human-readable health summary: volume, anomalies, relay cycling and command activity."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    nonfinite: dict[tuple[str, str], int] = defaultdict(int)
    pwm_points: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    min_run: dict[str, float] = {}
    sync_actions: dict[tuple[str, str], int] = defaultdict(int)
    dead_sensor: dict[str, int] = defaultdict(int)
    first = last = None
    total = 0
    for rec in records:
        total += 1
        zone, kind = str(rec.get("zone")), str(rec.get("kind"))
        counts[(zone, kind)] += 1
        wall = rec.get("wall")
        if isinstance(wall, (int, float)):
            first = wall if first is None else min(first, wall)
            last = wall if last is None else max(last, wall)
        if rec.get("nonfinite"):
            nonfinite[(zone, kind)] += 1
        data = rec.get("data") or {}
        if kind in ("pwm", "boiler") and "state" in data and isinstance(rec.get("mono"), (int, float)):
            pwm_points[f"{zone}/{kind}"].append((rec["mono"], bool(data["state"])))
            if isinstance(data.get("min_on"), (int, float)) and isinstance(data.get("min_off"), (int, float)):
                min_run[f"{zone}/{kind}"] = max(data["min_on"], data["min_off"])
        if kind == "sync":
            sync_actions[(zone, str(data.get("action")))] += 1
        if kind == "pid" and data.get("dead_sensor"):
            dead_sensor[zone] += 1

    lines = [f"records: {total}"]
    if first is not None:
        span_h = (last - first) / 3600.0
        lines.append(f"span: {_wall_iso(first)} .. {_wall_iso(last)} ({span_h:.2f} h)")
    lines.append("")
    lines.append("records by zone/kind:")
    for (zone, kind), n in sorted(counts.items()):
        flag = f"   [non-finite values in {nonfinite[(zone, kind)]}]" if nonfinite.get((zone, kind)) else ""
        lines.append(f"  {zone:32s} {kind:10s} {n:8d}{flag}")
    if pwm_points:
        lines += ["", "relay cycling (from logged states):"]
        for key, pts in sorted(pwm_points.items()):
            runs = _runs(pts)
            if not runs:
                lines.append(f"  {key}: no transitions in {len(pts)} samples")
                continue
            floor = min_run.get(key, 0.0)
            short = sum(1 for r in runs if floor and r < floor - 1e-6)
            hours = max((pts[-1][0] - pts[0][0]) / 3600.0, 1e-9)
            lines.append(
                f"  {key}: {len(runs)} transitions ({len(runs) / hours:.1f}/h), run min {min(runs):.0f}s "
                f"median {sorted(runs)[len(runs) // 2]:.0f}s max {max(runs):.0f}s"
                + (f", {short} runs shorter than configured minimum {floor:.0f}s  <-- min_on/min_off violated"
                   if short else "")
            )
    if sync_actions:
        lines += ["", "sync commands:"]
        for (zone, action), n in sorted(sync_actions.items()):
            lines.append(f"  {zone:32s} {action:20s} {n:6d}")
    if dead_sensor:
        lines += ["", "dead-sensor demand overrides:"]
        for zone, n in sorted(dead_sensor.items()):
            lines.append(f"  {zone}: {n}")
    return "\n".join(lines)


def load(paths: Iterable[str]) -> Iterator[dict[str, Any]]:
    return read_records(expand_paths(paths))
