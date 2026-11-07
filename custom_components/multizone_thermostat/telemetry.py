"""Structured, replayable telemetry for the control loop.

Pure standard library (no Home Assistant imports) so that the recorder can be unit tested and the
resulting files can be read by offline tooling (tests/replay, scripts/mzt_log).

Design rules:
* Recording must NEVER raise into, or block, control code. `record()` only appends to a bounded
  in-memory buffer; `flush()` does the file I/O and is meant to be called from an executor job.
* Every record carries both clocks (`wall` epoch seconds and `mono` monotonic seconds) because the
  integration mixes them; replay needs to know which one each component used.
* Files are JSON Lines, one record per line, schema version in every record (`v`).
* Non-finite floats are written in place as the strings "NaN", "+Inf" or "-Inf" (strict JSON stays
  valid for other tools) and the record gains `nonfinite: {path: token}`. `read_records` restores the
  real floats at exactly those paths, so replay sees the original NaN/inf and a genuine string
  "NaN" is never confused with one.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Record kinds. Keep in sync with docs/Telemetry.md.
KIND_SENSOR = "sensor"          # raw sensor reading as received
KIND_PID = "pid"                # PID.calc() inputs, terms, output
KIND_PWM = "pwm"                # PWMEngine.calculate() input, timings, state
KIND_ESTIMATOR = "estimator"    # rate estimator update inputs and resulting rates
KIND_SYNC = "sync"              # desired vs commanded vs observed switch state
KIND_BOILER = "boiler"          # boiler backend decision and reason
KIND_EVENT = "event"            # free-form notable event (anomaly, restart, config change)


_NONFINITE_TOKENS = {"NaN": math.nan, "+Inf": math.inf, "-Inf": -math.inf}


def _token(value: float) -> str:
    return "NaN" if math.isnan(value) else ("+Inf" if value > 0 else "-Inf")


def _restore(value: Any, nonfinite: dict[str, str], path: str = "") -> Any:
    """Turn the tokens written by `_sanitize` back into floats at exactly the flagged paths."""
    if path in nonfinite and isinstance(value, str) and value in _NONFINITE_TOKENS:
        return _NONFINITE_TOKENS[value]
    if isinstance(value, dict):
        return {k: _restore(v, nonfinite, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_restore(v, nonfinite, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def _sanitize(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else _token(value)
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return repr(value)


def _find_nonfinite(value: Any, path: str = "") -> dict[str, str]:
    if isinstance(value, float) and not math.isfinite(value):
        return {path: _token(value)}
    found: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            found.update(_find_nonfinite(v, f"{path}.{k}"))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            found.update(_find_nonfinite(v, f"{path}[{i}]"))
    return found


class NullRecorder:
    """Drop-in recorder that does nothing; used when telemetry is disabled."""

    enabled = False

    def record(self, kind: str, zone: str | None = None, **data: Any) -> None:
        return None

    def flush(self) -> int:
        return 0

    def latest(self, zone: str | None, kind: str) -> dict[str, Any] | None:
        return None


class JsonlRecorder:
    """Buffered JSON Lines recorder with daily files and bounded disk/memory use."""

    enabled = True

    def __init__(
        self,
        directory: str,
        *,
        wall_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
        max_buffer: int = 5000,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_total_bytes: int = 150 * 1024 * 1024,
        retention_days: int = 7,
        prefix: str = "mzt",
        persist: bool = True,
    ) -> None:
        self.directory = directory
        self.persist = persist  # False: keep only the in-memory `latest` view (debug sensors), write nothing
        self._latest: dict[tuple[str | None, str], dict[str, Any]] = {}
        self._wall = wall_clock
        self._mono = mono_clock
        self._buffer: deque[dict[str, Any]] = deque()
        self._max_buffer = max_buffer
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes
        self._retention_days = retention_days
        self._prefix = prefix
        self._seq = 0
        self._lock = threading.Lock()
        self.dropped = 0
        self.write_errors = 0

    def record(self, kind: str, zone: str | None = None, **data: Any) -> None:
        """Queue a record. Never raises and never touches the disk."""
        try:
            with self._lock:
                self._seq += 1
                rec: dict[str, Any] = {
                    "v": SCHEMA_VERSION,
                    "seq": self._seq,
                    "wall": self._wall(),
                    "mono": self._mono(),
                    "zone": zone,
                    "kind": kind,
                    "data": data,
                }
                if len(self._buffer) >= self._max_buffer:
                    self._buffer.popleft()
                    self.dropped += 1
                self._latest[(zone, kind)] = rec
                self._buffer.append(rec)
        except Exception:  # pragma: no cover - defensive, recording must not break control
            self.dropped += 1

    def latest(self, zone: str | None, kind: str) -> dict[str, Any] | None:
        """Most recent record queued for (zone, kind), regardless of whether it was flushed."""
        return self._latest.get((zone, kind))

    def _path_for(self, wall: float, part: int) -> str:
        day = datetime.fromtimestamp(wall, tz=timezone.utc).strftime("%Y-%m-%d")
        suffix = "" if part == 0 else f".{part}"
        return os.path.join(self.directory, f"{self._prefix}_{day}{suffix}.jsonl")

    def flush(self) -> int:
        """Write queued records to disk. Blocking I/O: call from an executor, not the event loop."""
        with self._lock:
            batch = list(self._buffer)
            self._buffer.clear()
        if not batch or not self.persist:
            return 0
        try:
            os.makedirs(self.directory, exist_ok=True)
            lines_by_path: dict[str, list[str]] = {}
            for rec in batch:
                nonfinite = _find_nonfinite(rec["data"], "")
                clean = _sanitize(rec)
                if nonfinite:
                    clean["nonfinite"] = nonfinite
                path = self._pick_path(rec["wall"])
                lines_by_path.setdefault(path, []).append(json.dumps(clean, separators=(",", ":")))
            for path, lines in lines_by_path.items():
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            self._prune(self._wall())
            return len(batch)
        except Exception as err:
            self.write_errors += 1
            _LOGGER.warning("Telemetry flush failed (%d records lost): %s", len(batch), err)
            return 0

    def _pick_path(self, wall: float) -> str:
        part = 0
        while True:
            path = self._path_for(wall, part)
            try:
                if os.path.getsize(path) < self._max_file_bytes:
                    return path
            except OSError:
                return path
            part += 1

    def _prune(self, now: float) -> None:
        cutoff = now - self._retention_days * 86400
        try:
            entries = []
            for name in os.listdir(self.directory):
                if name.startswith(self._prefix + "_") and name.endswith(".jsonl"):
                    full = os.path.join(self.directory, name)
                    stat = os.stat(full)
                    if stat.st_mtime < cutoff:
                        os.remove(full)
                    else:
                        entries.append((stat.st_mtime, stat.st_size, full))
            total = sum(size for _, size, _ in entries)
            for _, size, full in sorted(entries):
                if total <= self._max_total_bytes or len(entries) <= 1:
                    break
                os.remove(full)
                total -= size
                entries = [e for e in entries if e[2] != full]
        except OSError as err:
            _LOGGER.debug("Telemetry prune failed: %s", err)


_recorder: "JsonlRecorder | NullRecorder" = NullRecorder()


def get_recorder() -> "JsonlRecorder | NullRecorder":
    """Process-wide recorder used by instrumented control code (a no-op until one is installed)."""
    return _recorder


def set_recorder(recorder: "JsonlRecorder | NullRecorder | None") -> None:
    """Install (or, with None, remove) the process-wide recorder."""
    global _recorder
    _recorder = recorder if recorder is not None else NullRecorder()


def read_records(paths: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Yield records from JSONL files in order, skipping (and counting nothing about) torn lines.

    A torn final line is expected after a crash; any unparsable line is skipped.
    """
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                flagged = rec.get("nonfinite") if isinstance(rec, dict) else None
                if flagged and isinstance(rec.get("data"), dict):
                    rec["data"] = _restore(rec["data"], flagged)
                yield rec
