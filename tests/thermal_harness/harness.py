"""Replay recorded sensor history through heating/cooling rate estimators and compare them.

An estimator is any object with:
    update(ts: datetime, temp: float, is_heating: bool) -> None
    heating_rate: float   (°C/h, 0.0 when unknown)
    cooling_rate: float   (°C/h, positive, 0.0 when unknown)

Register new estimators in estimators.ESTIMATORS. Run from the repo root:
    python -m thermal_harness.harness [--data sample_data] [--estimators legacy,...]
"""
from __future__ import annotations

import argparse
import csv
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import numpy as np


@dataclass
class Series:
    name: str
    entity_id: str
    times: np.ndarray  # seconds since epoch
    temps: np.ndarray  # °C

    @property
    def hours(self) -> np.ndarray:
        return (self.times - self.times[0]) / 3600.0


def _parse_ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def load_series(path: Path) -> Series:
    """Load the temperature series (`sensor.*` rows) of an HA history export.

    Files may hold other entities too (switches, sun.sun); those are ignored here. Non-numeric
    states such as unavailable/unknown are dropped.
    """
    times, temps, entity = [], [], ""
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not row["entity_id"].startswith("sensor."):
                continue
            try:
                temp = float(row["state"])
            except ValueError:
                continue
            entity = row["entity_id"]
            times.append(_parse_ts(row["last_changed"]))
            temps.append(temp)
    order = np.argsort(times, kind="stable")
    return Series(path.stem, entity, np.array(times)[order], np.array(temps)[order])


def load_actuators(path: Path, entities: list[str]) -> dict[str, list[tuple[float, bool]]]:
    """Load on/off histories of switch-like entities from a (possibly mixed) export.

    'on'/'heating' is True, 'off'/'idle' is False; unavailable/unknown are skipped (state is held).
    """
    out: dict[str, list[tuple[float, bool]]] = {e: [] for e in entities}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            state = row["state"].strip().lower()
            if row["entity_id"] in out and state in ("on", "off", "heating", "idle"):
                out[row["entity_id"]].append((_parse_ts(row["last_changed"]), state in ("on", "heating")))
    return {e: sorted(v) for e, v in out.items()}


def actuator_heating(series: Series, actuators: dict[str, list[tuple[float, bool]]],
                     gap_s: float = 300.0, min_s: float = 300.0) -> tuple[Series, np.ndarray]:
    """Heating flag = ALL actuators on (e.g. zone valve AND boiler call), debounced.

    Samples before every actuator has reported are dropped (unknown is not idle). Idle gaps up to
    `gap_s` are filled and heating runs shorter than `min_s` removed, so PWM pulses do not count as
    separate heating periods.
    """
    if not actuators or any(not v for v in actuators.values()):
        raise SystemExit("heating entity has no on/off history in this file")
    start = max(v[0][0] for v in actuators.values())
    keep = series.times >= start
    series = Series(series.name, series.entity_id, series.times[keep], series.temps[keep])
    flags = np.ones(len(series.times), dtype=bool)
    for hist in actuators.values():
        ht = np.array([t for t, _ in hist])
        hs = np.array([s for _, s in hist])
        flags &= hs[np.searchsorted(ht, series.times, side="right") - 1]
    return series, debounce(series.times, flags, gap_s, min_s)


def debounce(times: np.ndarray, flags: np.ndarray, gap_s: float, min_s: float) -> np.ndarray:
    """Fill False gaps <= gap_s between True runs, then drop True runs shorter than min_s."""
    flags = flags.copy()
    n = len(flags)

    def runs(value):
        start = 0
        for i in range(1, n + 1):
            if i == n or flags[i] != flags[start]:
                if flags[start] == value:
                    yield start, i
                start = i

    for a, b in list(runs(False)):
        if a > 0 and b < n and times[b] - times[a - 1] <= gap_s:
            flags[a:b] = True
    for a, b in list(runs(True)):
        end = times[b] if b < n else times[-1]
        if end - times[a] < min_s:
            flags[a:b] = False
    return flags


def load_hvac_states(path: Path) -> list[tuple[float, bool]]:
    """Load a climate/hvac_action history export; heating when state is 'heating' or 'on'."""
    with path.open(newline="", encoding="utf-8") as fh:
        rows = [(_parse_ts(r["last_changed"]), r["state"].strip().lower() in ("heating", "on"))
                for r in csv.DictReader(fh)]
    return sorted(rows)


def infer_heating(series: Series, window_h: float = 0.5, rise_c_per_h: float = 0.5,
                  min_duration_h: float = 0.25) -> np.ndarray:
    """Label samples heating/idle when no hvac_action history exists.

    Heating when the centred least-squares slope over `window_h` exceeds `rise_c_per_h`; runs shorter
    than `min_duration_h` take the preceding state. It is a stand-in for real hvac_action data and is
    the same for every estimator, so comparisons stay fair.
    """
    t = series.hours
    n = len(t)
    slope = np.zeros(n)
    lo = hi = 0
    for i in range(n):
        while t[lo] < t[i] - window_h / 2:
            lo += 1
        while hi < n and t[hi] <= t[i] + window_h / 2:
            hi += 1
        if hi - lo >= 3 and t[hi - 1] > t[lo]:
            slope[i] = np.polyfit(t[lo:hi], series.temps[lo:hi], 1)[0]
    flags = slope > rise_c_per_h

    start = 0
    for i in range(1, n + 1):
        if i == n or flags[i] != flags[start]:
            if start > 0 and i < n and t[i] - t[start] < min_duration_h:
                flags[start:i] = flags[start - 1]
            start = i
    return flags


def heating_flags(series: Series, hvac: list[tuple[float, bool]] | None) -> np.ndarray:
    if not hvac:
        return infer_heating(series)
    hvac_times = np.array([t for t, _ in hvac])
    hvac_state = np.array([s for _, s in hvac])
    idx = np.searchsorted(hvac_times, series.times, side="right") - 1
    return np.where(idx >= 0, hvac_state[np.clip(idx, 0, None)], False)


@dataclass
class RunResult:
    estimator: str
    room: str
    heating_final: float
    heating_median: float
    heating_p90: float
    cooling_final: float
    cooling_median: float
    cooling_p90: float
    cooling_max: float
    seconds: float = 0.0
def _pct(values: list[float], q: float) -> float:
    return float(np.percentile(values, q)) if values else 0.0


def outdoor_at(series: Series, outdoor: Series | None) -> np.ndarray | None:
    """Outdoor temperature held-last-value at each sample time (NaN before the first reading)."""
    if outdoor is None:
        return None
    idx = np.searchsorted(outdoor.times, series.times, side="right") - 1
    return np.where(idx >= 0, outdoor.temps[np.clip(idx, 0, None)], np.nan)


def replay(factory: Callable[[str], object], name: str, series: Series, heating: np.ndarray,
           outdoor: np.ndarray | None = None) -> RunResult:
    """Feed every sample to a fresh estimator, summarising the learned-rate trajectory."""
    est = factory(series.name)
    cool, heat = [], []
    started = time.perf_counter()
    for i, (ts, temp, is_heating) in enumerate(zip(series.times, series.temps, heating)):
        if outdoor is not None and getattr(est, "uses_outdoor", False):
            est.set_outdoor(None if np.isnan(outdoor[i]) else float(outdoor[i]))
        est.update(datetime.fromtimestamp(float(ts), timezone.utc), float(temp), bool(is_heating))
        if est.cooling_rate > 0:
            cool.append(est.cooling_rate)
        if est.heating_rate > 0:
            heat.append(est.heating_rate)
    elapsed = time.perf_counter() - started
    return RunResult(
        name, series.name,
        est.heating_rate, statistics.median(heat) if heat else 0.0, _pct(heat, 90),
        est.cooling_rate, statistics.median(cool) if cool else 0.0, _pct(cool, 90), max(cool, default=0.0),
        elapsed,
    )


def reference_cooling(series: Series, heating: np.ndarray, min_hours: float = 2.0,
                      min_drop: float = 0.3) -> list[float]:
    """Model-independent cooling rates: LS slope over each long idle stretch that actually fell."""
    rates = []
    n = len(series.times)
    start = 0
    for i in range(1, n + 1):
        if i == n or heating[i] != heating[start]:
            if not heating[start] and i - start >= 3:
                t = series.hours[start:i]
                span = t[-1] - t[0]
                if span >= min_hours:
                    drop = -float(np.polyfit(t, series.temps[start:i], 1)[0])
                    if drop * span >= min_drop:
                        rates.append(drop)
            start = i
    return rates


def run(data_dir: Path, estimator_names: Iterable[str] | None, hvac_dir: Path | None,
        outdoor_path: Path | None = None, heating_entities: list[str] | None = None,
        gap_s: float = 300.0, min_s: float = 300.0) -> int:
    from .estimators import ESTIMATORS

    names = list(estimator_names or ESTIMATORS)
    unknown = [n for n in names if n not in ESTIMATORS]
    if unknown:
        raise SystemExit(f"Unknown estimators {unknown}; available: {list(ESTIMATORS)}")
    files = sorted(data_dir.glob("*.csv")) if data_dir.is_dir() else [data_dir]
    if not files:
        raise SystemExit(f"No CSV files in {data_dir}")

    print(f"{'room':<14}{'estimator':<13}{'heat fin':>9}{'heat med':>9}{'heat p90':>9}"
          f"{'cool fin':>9}{'cool med':>9}{'cool p90':>9}{'cool max':>9}{'x ref':>7}{'secs':>7}   (°C/h)")
    totals: dict[str, list[float]] = {n: [] for n in names}
    outdoor_series = load_series(outdoor_path) if outdoor_path else None
    for path in files:
        series = load_series(path)
        source = "inferred"
        actuators = load_actuators(path, heating_entities) if heating_entities else {}
        if heating_entities and all(actuators.values()):
            series, heating = actuator_heating(series, actuators, gap_s, min_s)
            source = "+".join(e.split(".")[-1] for e in heating_entities)
        else:
            hvac_path = hvac_dir / path.name if hvac_dir else None
            hvac = load_hvac_states(hvac_path) if hvac_path and hvac_path.exists() else None
            heating = heating_flags(series, hvac)
            source = "hvac" if hvac else "inferred"
        ref = reference_cooling(series, heating)
        ref_med = statistics.median(ref) if ref else 0.0
        days = (series.times[-1] - series.times[0]) / 86400
        print(f"-- {series.name}: {len(series.times)} samples over {days:.1f} d, heating "
              f"{100 * heating.mean():.0f}% ({source}); reference cooling "
              f"{len(ref)} idle segments, median {ref_med:.2f}, max {max(ref, default=0):.2f}")
        for name in names:
            r = replay(ESTIMATORS[name], name, series, heating, outdoor_at(series, outdoor_series))
            ratio = r.cooling_median / ref_med if ref_med else float("nan")
            totals[name].append(abs(r.cooling_median - ref_med))
            print(f"{r.room:<14}{r.estimator:<13}{r.heating_final:>9.2f}{r.heating_median:>9.2f}"
                  f"{r.heating_p90:>9.2f}{r.cooling_final:>9.2f}{r.cooling_median:>9.2f}"
                  f"{r.cooling_p90:>9.2f}{r.cooling_max:>9.2f}{ratio:>7.1f}{r.seconds:>7.1f}")
    print("\nMean |cooling median - reference median| across rooms (°C/h, lower is better):")
    for name, errs in sorted(totals.items(), key=lambda kv: np.mean(kv[1])):
        print(f"  {name:<13}{np.mean(errs):.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=Path("sample_data"), help="directory of CSVs, or one CSV")
    p.add_argument("--heating-entity", action="append", default=None,
                   help="switch entity in the data file(s); repeat to AND several (e.g. valve + boiler)")
    p.add_argument("--debounce-gap", type=float, default=300.0, help="fill idle gaps up to N seconds")
    p.add_argument("--min-heating", type=float, default=300.0, help="drop heating runs shorter than N s")
    p.add_argument("--hvac", type=Path, default=None,
                   help="dir with same-named CSVs of hvac_action history (optional)")
    p.add_argument("--outdoor", type=Path, default=None,
                   help="CSV of outdoor temperature history (entity_id,state,last_changed), optional")
    p.add_argument("--estimators", default=None, help="comma separated names (default: all)")
    args = p.parse_args(argv)
    return run(args.data, args.estimators.split(",") if args.estimators else None, args.hvac,
               args.outdoor, args.heating_entity, args.debounce_gap, args.min_heating)


if __name__ == "__main__":
    raise SystemExit(main())
