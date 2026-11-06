"""Seeded synthetic data with known ground truth."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class Sample:
    t: float            # seconds from start
    temp: float         # what the sensor reports (quantised / noisy)
    true_temp: float    # ground truth
    heating: bool       # actuator state at this instant


@dataclass
class RoomRun:
    samples: list[Sample]
    tau_s: float
    outdoor: float
    heat_rate_c_per_h: float

    def true_mean_rate(self, t0: float, t1: float) -> float:
        """Mean true temperature change magnitude in °C/h between two instants (from the true trace)."""
        pts = [s for s in self.samples if t0 <= s.t <= t1]
        return abs(pts[-1].true_temp - pts[0].true_temp) / ((pts[-1].t - pts[0].t) / 3600.0)


def simulate_room(
    schedule: list[tuple[float, bool]],
    *,
    start_temp: float = 20.0,
    outdoor: float = 5.0,
    tau_h: float = 40.0,
    heat_rate_c_per_h: float = 2.0,
    dt_s: float = 10.0,
    quantum: float = 0.1,
    flicker_prob: float = 0.0,
    noise_sd: float = 0.0,
    report_on_change: bool = True,
    seed: int = 1,
) -> RoomRun:
    """First-order room: dT/dt = (Tout - T)/tau + heat_rate*(heating).

    `schedule` is a list of (duration_s, heating) segments. The sensor quantises to `quantum`,
    optionally adds Gaussian noise before quantising and random one-quantum flicker, and (like real
    Home Assistant sensors) only emits a sample when its reported value changes.
    """
    rng = random.Random(seed)
    tau_s = tau_h * 3600.0
    temp = start_temp
    t = 0.0
    out: list[Sample] = []
    last_reported: float | None = None
    for duration, heating in schedule:
        end = t + duration
        while t < end:
            d = (outdoor - temp) / tau_s + (heat_rate_c_per_h / 3600.0 if heating else 0.0)
            temp += d * dt_s
            t += dt_s
            reading = temp + (rng.gauss(0.0, noise_sd) if noise_sd else 0.0)
            reading = round(reading / quantum) * quantum
            if flicker_prob and rng.random() < flicker_prob:
                reading += rng.choice((-quantum, quantum))
            reading = round(reading, 6)
            if report_on_change and reading == last_reported:
                continue
            last_reported = reading
            out.append(Sample(t, reading, temp, heating))
    return RoomRun(out, tau_s, outdoor, heat_rate_c_per_h)


def to_datetime(t: float, epoch: float = 1_700_000_000.0) -> datetime:
    return datetime.fromtimestamp(epoch + t, tz=timezone.utc)
