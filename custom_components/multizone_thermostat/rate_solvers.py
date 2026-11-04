"""Candidate heating/cooling rate solvers for the harness.

Batch solvers buffer raw samples and periodically analyse a trailing window; real-time solvers
update in O(1)/O(small) per sample. Both separate "how fast is the room changing" (HVAC-agnostic)
from "what was the HVAC doing" (overlaid afterwards on blocks of >= min_block_s), so flapping
hvac_action cannot corrupt the rate maths. Cooling = -median(rate | idle block), heating =
median(rate | heating block).
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

import numpy as np


# ----------------------------------------------------------------------------- batch base

class BatchEstimator:
    """Buffer (t, temp, heating); every refit_s resample the trailing window and analyse it."""

    window_s = 24 * 3600
    refit_s = 2 * 3600
    grid_s = 300.0
    min_block_s = 30 * 60
    uses_outdoor = False

    def __init__(self, zone_id: str):
        self.zone_id = zone_id
        self._t: list[float] = []
        self._x: list[float] = []
        self._h: list[bool] = []
        self._last_fit: float | None = None
        self.heating_rate = 0.0
        self.cooling_rate = 0.0

    def update(self, ts: datetime, temp: float, is_heating: bool) -> None:
        now = ts.timestamp()
        self._t.append(now)
        self._x.append(temp)
        self._h.append(is_heating)
        drop = 0
        while self._t[drop] < now - self.window_s:
            drop += 1
        if drop:
            del self._t[:drop], self._x[:drop], self._h[:drop]
        if self._last_fit is None or now - self._last_fit >= self.refit_s:
            self._last_fit = now
            self._refit()

    def _refit(self) -> None:
        t = np.array(self._t)
        if len(t) < 10 or t[-1] - t[0] < 2 * self.min_block_s:
            return
        grid = np.arange(t[0], t[-1], self.grid_s)
        idx = np.searchsorted(t, grid, side="right") - 1  # sample-and-hold onto a regular grid
        x = np.array(self._x)[idx]
        heating = np.array(self._h)[idx]
        idle, heat = self._block_values(grid - grid[0], x, heating)
        if idle:
            self.cooling_rate = max(0.0, -float(np.median(idle)))
        if heat:
            self.heating_rate = max(0.0, float(np.median(heat)))

    def _blocks(self, heating: np.ndarray):
        min_n = int(self.min_block_s / self.grid_s)
        start = 0
        for i in range(1, len(heating) + 1):
            if i == len(heating) or heating[i] != heating[start]:
                if i - start >= min_n:
                    yield start, i, bool(heating[start])
                start = i

    def _block_values(self, grid_s, x, heating):
        """Return (idle_values, heating_values) in °C/h. Default: pointwise derivative overlay."""
        rate = self._derivative(grid_s, x)
        idle, heat = [], []
        for a, b, is_heat in self._blocks(heating):
            (heat if is_heat else idle).extend(rate[a:b])
        return idle, heat

    def _derivative(self, grid_s: np.ndarray, x: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class TrendIdleEstimator(BatchEstimator):
    """Total-variation (trend-filtered) derivative of the whole window, HVAC overlaid afterwards."""

    alpha = 3e-2

    def _derivative(self, grid_s, x):
        from derivative import dxdt
        try:
            return dxdt(x, grid_s, kind="trend_filtered", order=0, alpha=self.alpha) * 3600
        except Exception:
            return dxdt(x, grid_s, kind="spline", s=len(x) * 0.5, order=2, d=1) * 3600


class SavGolEstimator(BatchEstimator):
    """Savitzky-Golay local quadratic derivative over a 65-minute window."""

    window_pts = 13
    polyorder = 2

    def _derivative(self, grid_s, x):
        from scipy.signal import savgol_filter
        return savgol_filter(x, self.window_pts, self.polyorder, deriv=1, delta=self.grid_s / 3600.0)


class SmoothingSplineEstimator(BatchEstimator):
    """Smoothing spline with GCV-chosen penalty (no manual alpha); analytic derivative."""

    def _derivative(self, grid_s, x):
        from scipy.interpolate import make_smoothing_spline
        spline = make_smoothing_spline(grid_s / 3600.0, x)
        return spline.derivative()(grid_s / 3600.0)


class GaussianProcessEstimator(BatchEstimator):
    """GP regression (RBF kernel, quantisation as noise); derivative of the posterior mean."""

    length_h = 1.5
    noise = 0.05
    window_s = 12 * 3600  # cubic in samples; 144 grid points

    def _derivative(self, grid_s, x):
        h = grid_s / 3600.0
        y = x - x.mean()
        var = max(float(y.var()), 0.1)
        d = h[:, None] - h[None, :]
        k = var * np.exp(-0.5 * (d / self.length_h) ** 2)
        alpha = np.linalg.solve(k + self.noise ** 2 * np.eye(len(h)), y)
        return (-d / self.length_h ** 2 * k) @ alpha


def kalman_trend(t_h: np.ndarray, x: np.ndarray, q: float = 1.0, r: float = 0.03,
                 smooth: bool = False) -> np.ndarray:
    """Local-linear-trend Kalman filter (state [T, dT/dt]); optional RTS smoother. Returns rate °C/h.

    q is the rate random-walk intensity ((°C/h)²/h), r the measurement sigma (°C).
    """
    n = len(x)
    xs = np.zeros((n, 2))
    ps = np.zeros((n, 2, 2))
    xp = np.zeros((n, 2))
    pp = np.zeros((n, 2, 2))
    s = np.array([x[0], 0.0])
    p = np.diag([r * r, 1.0])
    for i in range(n):
        if i:
            dt = max(t_h[i] - t_h[i - 1], 1e-6)
            f = np.array([[1.0, dt], [0.0, 1.0]])
            s = f @ s
            p = f @ p @ f.T + q * np.array([[dt ** 3 / 3, dt ** 2 / 2], [dt ** 2 / 2, dt]])
        xp[i], pp[i] = s, p
        gain = p[:, 0] / (p[0, 0] + r * r)
        s = s + gain * (x[i] - s[0])
        p = p - np.outer(gain, p[0])
        xs[i], ps[i] = s, p
    if not smooth:
        return xs[:, 1]
    out = xs.copy()
    for i in range(n - 2, -1, -1):
        dt = max(t_h[i + 1] - t_h[i], 1e-6)
        f = np.array([[1.0, dt], [0.0, 1.0]])
        c = ps[i] @ f.T @ np.linalg.inv(pp[i + 1])
        out[i] = xs[i] + c @ (out[i + 1] - xp[i + 1])
    return out[:, 1]


class RTSSmootherEstimator(BatchEstimator):
    """Kalman filter run forwards and backwards (RTS) over the window."""

    def _derivative(self, grid_s, x):
        return kalman_trend(grid_s / 3600.0, x, smooth=True)


class PerPeriodLSEstimator(BatchEstimator):
    """Least-squares slope per debounced idle/heating block; median of block slopes."""

    def _slope(self, t_h, y):
        return float(np.polyfit(t_h, y, 1)[0])

    def _block_values(self, grid_s, x, heating):
        idle, heat = [], []
        for a, b, is_heat in self._blocks(heating):
            slope = self._slope(grid_s[a:b] / 3600.0, x[a:b])
            (heat if is_heat else idle).append(slope)
        return idle, heat


class HampelTheilSenEstimator(PerPeriodLSEstimator):
    """Hampel outlier filter, then Theil-Sen (median pairwise slope) per block."""

    hampel_pts = 7
    hampel_sigmas = 3.0

    def _slope(self, t_h, y):
        from scipy.stats import theilslopes
        return float(theilslopes(self._hampel(y), t_h)[0])

    def _hampel(self, y):
        half = self.hampel_pts // 2
        out = y.copy()
        for i in range(len(y)):
            w = y[max(0, i - half): i + half + 1]
            med = np.median(w)
            mad = 1.4826 * np.median(np.abs(w - med))
            if abs(y[i] - med) > self.hampel_sigmas * max(mad, 0.05):
                out[i] = med
        return out


# -------------------------------------------------------------------------- real-time base

class RealtimeEstimator:
    """Per-sample instantaneous rate + HVAC-block overlay. Subclasses implement _instant_rate."""

    window_s = 24 * 3600
    min_block_s = 30 * 60
    stat_every_s = 300.0
    uses_outdoor = False

    def __init__(self, zone_id: str):
        self.zone_id = zone_id
        self._block_flag: bool | None = None
        self._block_start = 0.0
        self._idle: deque[tuple[float, float]] = deque()
        self._heat: deque[tuple[float, float]] = deque()
        self._last_stat = -1e18
        self.heating_rate = 0.0
        self.cooling_rate = 0.0

    def update(self, ts: datetime, temp: float, is_heating: bool) -> None:
        t = ts.timestamp()
        rate = self._instant_rate(t, temp, is_heating)
        if is_heating != self._block_flag:
            self._block_flag, self._block_start = is_heating, t
        if rate is None or t - self._block_start < self.min_block_s:
            return
        (self._heat if is_heating else self._idle).append((t, rate))
        for dq in (self._idle, self._heat):
            while dq and dq[0][0] < t - self.window_s:
                dq.popleft()
        if t - self._last_stat >= self.stat_every_s:
            self._last_stat = t
            if self._idle:
                self.cooling_rate = max(0.0, -float(np.median([r for _, r in self._idle])))
            if self._heat:
                self.heating_rate = max(0.0, float(np.median([r for _, r in self._heat])))

    def _instant_rate(self, t: float, temp: float, is_heating: bool) -> float | None:
        raise NotImplementedError


class KalmanEstimator(RealtimeEstimator):
    """Local-linear-trend Kalman filter (state = temperature, rate); irregular dt native."""

    q = 1.0
    r = 0.03

    def __init__(self, zone_id: str):
        super().__init__(zone_id)
        self._s: np.ndarray | None = None
        self._p = np.eye(2)
        self._t = 0.0

    def _instant_rate(self, t, temp, is_heating):
        if self._s is None:
            self._s = np.array([temp, 0.0])
            self._p = np.diag([self.r ** 2, 1.0])
            self._t = t
            return None
        dt = max((t - self._t) / 3600.0, 1e-6)
        self._t = t
        f = np.array([[1.0, dt], [0.0, 1.0]])
        s = f @ self._s
        p = f @ self._p @ f.T + self.q * np.array([[dt ** 3 / 3, dt ** 2 / 2], [dt ** 2 / 2, dt]])
        gain = p[:, 0] / (p[0, 0] + self.r ** 2)
        self._s = s + gain * (temp - s[0])
        self._p = p - np.outer(gain, p[0])
        return float(self._s[1])


class RLSEstimator(RealtimeEstimator):
    """Exponentially-forgotten least-squares line (time-based decay) over recent samples."""

    tau_h = 0.75

    def __init__(self, zone_id: str):
        super().__init__(zone_id)
        self._t: float | None = None
        self._s = np.zeros(5)  # S0, Sx, Sy, Sxx, Sxy about the latest sample time

    def _instant_rate(self, t, temp, is_heating):
        s0, sx, sy, sxx, sxy = self._s
        if self._t is not None:
            d = (t - self._t) / 3600.0
            w = np.exp(-d / self.tau_h)
            s0, sx, sy, sxx, sxy = w * s0, w * sx, w * sy, w * sxx, w * sxy
            s0, sx, sy, sxx, sxy = s0, sx - d * s0, sy, sxx - 2 * d * sx + d * d * s0, sxy - d * sy
        self._t = t
        s0, sy = s0 + 1.0, sy + temp
        self._s = np.array([s0, sx, sy, sxx, sxy])
        den = s0 * sxx - sx * sx
        if s0 < 6 or den < 1e-4:
            return None
        return float((s0 * sxy - sx * sy) / den)


class TheilSenWindowEstimator(RealtimeEstimator):
    """Median of pairwise slopes over a sliding 45-minute window (subsampled to <= 40 points)."""

    span_s = 45 * 60
    max_pts = 40

    def __init__(self, zone_id: str):
        super().__init__(zone_id)
        self._buf: deque[tuple[float, float]] = deque()

    def _instant_rate(self, t, temp, is_heating):
        self._buf.append((t, temp))
        while self._buf[0][0] < t - self.span_s:
            self._buf.popleft()
        if len(self._buf) < 8 or t - self._buf[0][0] < self.span_s * 0.6:
            return None
        arr = np.array(self._buf)
        if len(arr) > self.max_pts:
            arr = arr[np.linspace(0, len(arr) - 1, self.max_pts).astype(int)]
        dt = arr[None, :, 0] - arr[:, None, 0]
        dy = arr[None, :, 1] - arr[:, None, 1]
        iu = np.triu_indices(len(arr), 1)
        dt, dy = dt[iu], dy[iu]
        ok = dt > 1.0
        return float(np.median(dy[ok] / dt[ok])) * 3600.0 if ok.any() else None


class PhysicalKalmanEstimator(KalmanEstimator):
    """Kalman rate regressed on the room physics: dT/dt = a - k*T_ref + b*u  (RLS, forgetting).

    T_ref is the outdoor temperature when the harness supplies one (then a absorbs only solar and
    internal gains), otherwise the room temperature itself (a/k acts as a fitted constant ambient).
    Reported rates are the model's idle and heating rates at the current conditions, so the cooling
    rate scales with the indoor-outdoor delta instead of being a single global average.
    """

    uses_outdoor = True
    forget = 0.9995
    warmup = 300

    def __init__(self, zone_id: str):
        super().__init__(zone_id)
        self._theta = np.zeros(3)  # a, k, b
        self._P = np.eye(3) * 1e3
        self._n = 0
        self._outdoor: float | None = None

    def set_outdoor(self, outdoor: float | None) -> None:
        self._outdoor = outdoor

    def update(self, ts: datetime, temp: float, is_heating: bool) -> None:
        t = ts.timestamp()
        rate = self._instant_rate(t, temp, is_heating)
        if rate is None:
            return
        delta = temp - (self._outdoor if self._outdoor is not None else 0.0)
        phi = np.array([1.0, -delta, 1.0 if is_heating else 0.0])
        pphi = self._P @ phi
        gain = pphi / (self.forget + phi @ pphi)
        self._theta = self._theta + gain * (rate - phi @ self._theta)
        self._P = (self._P - np.outer(gain, pphi)) / self.forget
        self._n += 1
        if self._n < self.warmup:
            return
        a, k, b = self._theta
        if k <= 0:
            return
        self.cooling_rate = max(0.0, -(a - k * delta))
        self.heating_rate = max(0.0, a - k * delta + b)


# ----------------------------------------------------------------------------- naive trend filter

def extract_true_thermal_rates(timestamps, values):
    """Total-variation trend filter of a raw series; returns (heating, cooling) in °C/h.

    Takes the 99th/1st percentile of the derivative, which picks noise extremes, so it overestimates.
    Kept as a reference candidate; prefer trend_idle.
    """
    from derivative import dxdt

    t = np.array(timestamps, dtype=float)
    x = np.array(values, dtype=float)
    if len(t) < 10:
        return 0.0, 0.0
    try:
        raw = dxdt(x, t, kind="trend_filtered", order=0, alpha=3e-2)
    except Exception:
        raw = dxdt(x, t, kind="spline", s=len(x) * 0.5, order=2, d=1)
    rates_per_hour = raw * 3600
    return (max(0.0, np.percentile(rates_per_hour, 99.0)),
            abs(min(0.0, np.percentile(rates_per_hour, 1.0))))


class TrendFilteredEstimator:
    """Streaming wrapper that re-runs extract_true_thermal_rates() on a trailing 12 h window."""

    window_s = 12 * 3600
    refit_s = 6 * 3600

    def __init__(self, zone_id: str):
        self.zone_id = zone_id
        self._t: list[float] = []
        self._x: list[float] = []
        self._last_fit: float | None = None
        self.heating_rate = 0.0
        self.cooling_rate = 0.0

    def update(self, ts: datetime, temp: float, is_heating: bool) -> None:
        now = ts.timestamp()
        self._t.append(now)
        self._x.append(temp)
        drop = 0
        while self._t[drop] < now - self.window_s:
            drop += 1
        if drop:
            del self._t[:drop], self._x[:drop]
        if self._last_fit is None or now - self._last_fit >= self.refit_s:
            self._last_fit = now
            self.heating_rate, self.cooling_rate = extract_true_thermal_rates(self._t, self._x)


# name -> (class, third-party modules it needs at runtime). numpy ships with Home Assistant;
# scipy and derivative do not and are therefore optional.
SOLVERS = {
    "trend": (TrendFilteredEstimator, ("numpy", "derivative")),
    "trend_idle": (TrendIdleEstimator, ("numpy", "derivative")),
    "rt_kalman": (KalmanEstimator, ("numpy",)),
    "rt_physical": (PhysicalKalmanEstimator, ("numpy",)),
    "rt_rls": (RLSEstimator, ("numpy",)),
    "rt_theilsen": (TheilSenWindowEstimator, ("numpy",)),
    "b_savgol": (SavGolEstimator, ("numpy", "scipy")),
    "b_spline": (SmoothingSplineEstimator, ("numpy", "scipy")),
    "b_perperiod": (PerPeriodLSEstimator, ("numpy",)),
    "b_hampel_ts": (HampelTheilSenEstimator, ("numpy", "scipy")),
    "b_gp": (GaussianProcessEstimator, ("numpy",)),
    "b_rts": (RTSSmootherEstimator, ("numpy",)),
}
