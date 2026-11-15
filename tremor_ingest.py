#!/usr/bin/env python3
"""Tremor monitoring prototype: turn sensor readings into tremor features.

Sources (``--source``):

  myo        Laptop. Myo accelerometer over Bluetooth, squeeze or Enter to log a dose.
  grove      Pi backup. Grove accelerometer, button on D5, buzzer on D16.
  simulate   Laptop. Fake 5.2 Hz tremor, Enter to log a dose.

Quick start:
  python3 tremor_ingest.py --selftest
  python3 tremor_ingest.py --source myo --calibrate
  python3 tremor_ingest.py --source myo --dry-run
  python3 tremor_ingest.py --source grove
  python3 tremor_ingest.py --source simulate
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import math
import os
import queue
import signal
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

# Dose threshold sits 60% of the way from a relaxed arm to a squeeze.
SQUEEZE_FRACTION = 0.6
EMG_WINDOW_S = 0.2
SQUEEZE_HOLD_S = 1.5
SQUEEZE_IGNORE_S = 10.0          # default --dose-lockout
REARM_BELOW_S = 1.0              # relaxed this long, and the lockout over, before the next dose
SMOOTH_S = 0.5                   # moving average applied before the squeeze threshold
RELEASE_FRACTION = 0.8           # a squeeze ends below 80% of the threshold
STILL_HOLD_FRACTION = 0.8        # the last 1.5 s must be at least this still
STILL_G_DEFAULT = 0.05           # default --still-g, g
MOTION_AVG_S = 1.0               # moving-average length for the stillness residual
MOTION_RMS_S = 0.5               # how much of that residual goes into the motion RMS
# A squeeze hides the tremor check from 0.5 s before it until 1.5 s after it.
MASK_BEFORE_S = 0.5
MASK_AFTER_S = 1.5
RELAX_RECORD_S = 5.0
SQUEEZE_RECORD_S = 2.0
SQUEEZE_COUNT = 3
SIM_TREMOR_HZ = 5.2
CALIBRATION_PATH = Path(__file__).resolve().parent / "myo_calibration.json"

# Explicit flags override these. Myo rate is only a fallback: the band sets the real rate.
SOURCE_SETTINGS: dict[str, dict[str, float | int | str]] = {
    "myo": {
        "device": "tremor", "rate": 50.0, "window": 128, "hop": 64,
        "min_rms": 0.015, "min_ratio": 0.5, "min_peak_share": 0.3, "min_windows": 3,
    },
    "grove": {
        "device": "tremor-pi",
        "rate": 120.0,
        "window": 256,
        "hop": 128,
        "min_rms": 0.04,
        "min_ratio": 0.5,
        "min_peak_share": 0.45,
        "min_windows": 2,
        "button_pin": 5,
        "buzzer_pin": 16,
    },
    "simulate": {
        "device": "tremor", "rate": 50.0, "window": 128, "hop": 64,
        "min_rms": 0.015, "min_ratio": 0.5, "min_peak_share": 0.45, "min_windows": 2,
    },
}

# ----------------------------------------------------------------------------
# Signal processing
# ----------------------------------------------------------------------------

TREMOR_BAND = (3.0, 12.0)    # Hz. Parkinsonian ~4-6 Hz, essential tremor ~4-12 Hz
FAST_FLOOR = 1.5             # Hz. Slower than this = deliberate arm movement, ignored
PEAK_SHARE_HZ = 1.0          # peak_share is the band power within this many Hz of the peak
EMG_ENVELOPE_S = 0.025       # envelope smoother; still passes a 3-12 Hz muscle rhythm
EMG_MIN_PEAK_SHARE = 0.3     # muscle peak must hold this share of the 3-12 Hz envelope
EMG_MIN_SAMPLES = 64
EMG_MIN_SPAN_S = 1.0
EMG_CHANNELS = 8


def _peak_in_band(
    power: np.ndarray, freqs: np.ndarray, band: tuple[float, float]
) -> tuple[float, float] | None:
    """Dominant frequency in ``band``, and the share of band power within 1 Hz of it.

    The peak uses the same parabolic interpolation as the motion check. Returns
    None when ``band`` contains no frequency bins.
    """
    in_band = (freqs >= band[0]) & (freqs <= band[1])
    band_idx = np.flatnonzero(in_band)
    if band_idx.size == 0:
        return None
    k = int(band_idx[np.argmax(power[band_idx])])
    peak_hz = float(freqs[k])
    if 0 < k < len(power) - 1:
        a, b, c = np.log(power[k - 1:k + 2] + 1e-20)
        denom = a - 2 * b + c
        if denom != 0:
            peak_hz += float(0.5 * (a - c) / denom) * (freqs[1] - freqs[0])
    band_power = float(power[in_band].sum())
    near_peak = in_band & (np.abs(freqs - peak_hz) <= PEAK_SHARE_HZ)
    peak_share = float(power[near_peak].sum() / band_power) if band_power > 0 else 0.0
    return peak_hz, peak_share


def tremor_features(xyz, fs, band=TREMOR_BAND):
    """
    xyz: (N, 3) array of acceleration in g. fs: sample rate in Hz.

    Returns dict with:
      peak_hz    - dominant frequency inside the tremor band
      rms_g      - RMS acceleration of the in-band (tremor) component, in g
      band_ratio - of all the fast (>1.5 Hz) motion, the fraction inside the band.
                   Sensor noise alone gives ~0.2; a real tremor gives ~0.8+.
      peak_share - of the 3-12 Hz power, the fraction within 1 Hz of peak_hz.
                   A steady tremor is near 1; a jerk spreads out and stays low.
    """
    x = np.asarray(xyz, dtype=float)
    n = len(x)
    # Remove gravity, wrist tilt and slow drift: subtract a best-fit line per axis
    t = np.arange(n)
    slope, intercept = np.polyfit(t, x, 1)
    x = x - (np.outer(t, slope) + intercept)
    freqs = np.fft.rfftfreq(n, 1.0 / fs)

    # Windowed power spectrum per axis, then SUM across axes.
    # (Taking the vector magnitude first would rectify the signal and double the frequency.)
    w = np.hanning(n)
    spec_w = np.fft.rfft(x * w[:, None], axis=0)
    power = (np.abs(spec_w) ** 2).sum(axis=1)

    in_band = (freqs >= band[0]) & (freqs <= band[1])
    fast = freqs >= FAST_FLOOR
    total = power[fast].sum()
    band_ratio = float(power[in_band].sum() / total) if total > 0 else 0.0

    # Strength: RMS of the in-band component via Parseval on the windowed spectrum
    # (one-sided spectrum, so x2; normalised by the window's energy)
    rms_g = float(np.sqrt(2.0 * power[in_band].sum() / (n * np.sum(w ** 2))))

    found = _peak_in_band(power, freqs, band)
    if found is None:
        peak_hz, peak_share = 0.0, 0.0
    else:
        peak_hz, peak_share = found
    return {"peak_hz": peak_hz, "rms_g": rms_g, "band_ratio": band_ratio, "peak_share": peak_share}


def is_tremor(f, min_rms, min_ratio, min_peak_share=0.45):
    """True when strength, band share, and peak concentration all clear their floors."""
    return (
        f["rms_g"] >= min_rms
        and f["band_ratio"] >= min_ratio
        and f["peak_share"] >= min_peak_share
    )


def _smooth_envelope(rectified: np.ndarray, fs: float) -> np.ndarray:
    """Centered moving average. A 25 ms window keeps a 3-12 Hz rhythm."""
    width = max(1, int(round(EMG_ENVELOPE_S * fs)))
    if width % 2 == 0:
        width += 1
    if width <= 1 or width >= len(rectified):
        return rectified
    kernel = np.ones(width, dtype=float) / float(width)
    pad = width // 2
    padded = np.pad(rectified, ((pad, pad), (0, 0)), mode="edge")
    smoothed = np.empty_like(rectified, dtype=float)
    for index in range(rectified.shape[1]):
        smoothed[:, index] = np.convolve(padded[:, index], kernel, mode="valid")
    return smoothed


def emg_rhythm(
    times: np.ndarray, values: np.ndarray, band: tuple[float, float] = TREMOR_BAND
) -> dict[str, float] | None:
    """Strongest 3-12 Hz peak in the summed muscle envelopes.

    ``values`` is ``(N, 8)`` samples covering one motion window. Each channel
    loses its mean, is rectified, and is smoothed into an envelope. Envelopes
    are detrended, Hann-windowed, and added as power spectra. Returns
    ``peak_hz`` and ``peak_share``, or None when the span cannot show the band.
    """
    stamps = np.asarray(times, dtype=float)
    samples = np.asarray(values, dtype=float)
    n = len(samples)
    if n < EMG_MIN_SAMPLES or samples.ndim != 2 or samples.shape[1] != EMG_CHANNELS:
        return None
    span = float(stamps[-1] - stamps[0])
    if span < EMG_MIN_SPAN_S:
        return None
    fs = (n - 1) / span
    if fs / 2.0 <= band[0]:
        return None
    centered = samples - samples.mean(axis=0, keepdims=True)
    envelopes = _smooth_envelope(np.abs(centered), fs)
    t = np.arange(n)
    slope, intercept = np.polyfit(t, envelopes, 1)
    envelopes = envelopes - (np.outer(t, slope) + intercept)
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    window = np.hanning(n)
    spectrum = np.fft.rfft(envelopes * window[:, None], axis=0)
    power = (np.abs(spectrum) ** 2).sum(axis=1)
    found = _peak_in_band(power, freqs, band)
    if found is None:
        return None
    peak_hz, peak_share = found
    return {"peak_hz": peak_hz, "peak_share": peak_share}


def muscle_confirms(emg_peak_hz: float, motion_peak_hz: float, peak_share: float) -> bool:
    """True when the muscle peak agrees with the motion peak and stands out.

    The peaks must be within 1 Hz. ``peak_share`` is the fraction of 3-12 Hz
    envelope power within 1 Hz of ``emg_peak_hz``, and it must be at least 0.3.
    """
    return (
        abs(emg_peak_hz - motion_peak_hz) <= PEAK_SHARE_HZ
        and peak_share >= EMG_MIN_PEAK_SHARE
    )


# ----------------------------------------------------------------------------
# Accelerometer drivers
# ----------------------------------------------------------------------------

try:
    from smbus2 import SMBus
except ImportError:
    try:
        from smbus import SMBus
    except ImportError:
        SMBus = None


class ADXL345:
    name = "ADXL345"

    def __init__(self, bus, addr):
        self.bus, self.addr = bus, addr
        bus.write_byte_data(addr, 0x2D, 0x00)   # POWER_CTL: standby while configuring
        bus.write_byte_data(addr, 0x2C, 0x0B)   # BW_RATE: 200 Hz output (we poll at 100)
        bus.write_byte_data(addr, 0x31, 0x08)   # DATA_FORMAT: full resolution, +/-2 g
        bus.write_byte_data(addr, 0x2D, 0x08)   # POWER_CTL: measure

    def read(self):
        d = self.bus.read_i2c_block_data(self.addr, 0x32, 6)
        x, y, z = struct.unpack("<hhh", bytes(d))
        s = 0.0039                               # g per LSB in full-res mode
        return x * s, y * s, z * s


class LIS3DH:
    name = "LIS3DH"

    def __init__(self, bus, addr):
        self.bus, self.addr = bus, addr
        bus.write_byte_data(addr, 0x20, 0x67)   # CTRL_REG1: 200 Hz, X/Y/Z on
        bus.write_byte_data(addr, 0x23, 0x88)   # CTRL_REG4: block update, +/-2 g, high-res

    def read(self):
        d = self.bus.read_i2c_block_data(self.addr, 0x28 | 0x80, 6)  # 0x80 = auto-increment
        x, y, z = struct.unpack("<hhh", bytes(d))
        s = 1.0 / 16000.0                        # 12-bit left-justified, 1 mg/digit
        return x * s, y * s, z * s


class MMA7660:
    name = "MMA7660"

    def __init__(self, bus, addr):
        self.bus, self.addr = bus, addr
        bus.write_byte_data(addr, 0x07, 0x00)   # MODE: standby
        bus.write_byte_data(addr, 0x08, 0x00)   # SR: 120 samples/s
        bus.write_byte_data(addr, 0x07, 0x01)   # MODE: active

    def read(self):
        out = []
        for reg in (0x00, 0x01, 0x02):
            for _ in range(5):
                v = self.bus.read_byte_data(self.addr, reg)
                if not v & 0x40:                 # alert bit set = mid-update, re-read
                    break
            v &= 0x3F
            if v & 0x20:
                v -= 64
            out.append(v / 21.33)
        return tuple(out)


def _try_read(bus, addr, reg):
    try:
        return bus.read_byte_data(addr, reg)
    except OSError:
        return None


def detect_sensor(bus):
    for addr in (0x53, 0x1D):
        if _try_read(bus, addr, 0x00) == 0xE5:
            return ADXL345(bus, addr)
    for addr in (0x19, 0x18):
        if _try_read(bus, addr, 0x0F) == 0x33:
            return LIS3DH(bus, addr)
    if _try_read(bus, 0x4C, 0x07) is not None:
        print("WARNING: MMA7660 found. It only has 6-bit resolution, so it will catch "
              "big shakes but miss mild tremor. Swap for an ADXL345/LIS3DH if you can.")
        return MMA7660(bus, 0x4C)

    found = [hex(a) for a in range(0x03, 0x78) if _try_read(bus, a, 0x00) is not None]
    sys.exit("No supported accelerometer found on I2C bus 1. Devices seen: "
             f"{found or 'none'}.\nCheck wiring (3.3V, GND, SDA=pin 3, SCL=pin 5) and that "
             "I2C is enabled (sudo raspi-config > Interface Options > I2C).")


# ----------------------------------------------------------------------------
# Tiger Data writer (background thread so slow network never stalls sampling)
# ----------------------------------------------------------------------------

def ts(t: object) -> dt.datetime:
    """Unix seconds or a datetime, as a timezone-aware UTC instant.

    ``float()`` strips NumPy scalars so psycopg2 does not treat ``np.float64``
    as a schema name.
    """
    if isinstance(t, dt.datetime):
        if t.tzinfo is None or t.tzinfo.utcoffset(t) is None:
            return t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(dt.timezone.utc)
    return dt.datetime.fromtimestamp(float(t), tz=dt.timezone.utc)


def _db_float(value: object) -> float:
    """A measurement as a plain float."""
    return float(value)


def _db_bool(value: object) -> bool:
    """A flag as a plain bool."""
    return bool(value)


def _db_optional_float(value: object) -> float | None:
    """A measurement as a plain float, or None when it was not computed."""
    if value is None:
        return None
    return float(value)


def _db_optional_bool(value: object) -> bool | None:
    """A flag as a plain bool, or None when it was not evaluated."""
    if value is None:
        return None
    return bool(value)


class TigerWriter(threading.Thread):
    MAX_BUFFERED_RAW = 100 * 60 * 10   # keep up to ~10 min of raw data if offline

    def __init__(self, url, device_id):
        super().__init__(daemon=True)
        import psycopg2
        from psycopg2.extras import execute_values
        self.psycopg2, self.execute_values = psycopg2, execute_values
        self.url, self.device = url, device_id
        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.raw, self.feat, self.med = [], [], []
        self.conn = None

    def put(self, kind, row):
        self.q.put((kind, row))

    def _drain(self, timeout):
        end = time.time() + timeout
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                break
            try:
                kind, row = self.q.get(timeout=remaining)
            except queue.Empty:
                break
            {"raw": self.raw, "feat": self.feat, "med": self.med}[kind].append(row)

    def _flush(self):
        if not (self.raw or self.feat or self.med):
            return
        try:
            if self.conn is None:
                self.conn = self.psycopg2.connect(self.url, connect_timeout=10)
            d = str(self.device)
            with self.conn.cursor() as cur:
                if self.raw:
                    self.execute_values(
                        cur, "INSERT INTO accel_raw (time, device_id, ax, ay, az) VALUES %s",
                        [
                            (ts(t), d, _db_float(x), _db_float(y), _db_float(z))
                            for t, x, y, z in self.raw
                        ],
                        page_size=1000,
                    )
                if self.feat:
                    self.execute_values(
                        cur, "INSERT INTO tremor_features "
                             "(time, device_id, peak_hz, rms_g, band_ratio, tremor, "
                             "emg_peak_hz, emg_confirmed) VALUES %s",
                        [
                            (
                                ts(t), d, _db_float(p), _db_float(r), _db_float(b), _db_bool(tr),
                                _db_optional_float(ep), _db_optional_bool(ec),
                            )
                            for t, p, r, b, tr, ep, ec in self.feat
                        ],
                    )
                if self.med:
                    self.execute_values(
                        cur, "INSERT INTO med_events (time, device_id, note) VALUES %s",
                        [(ts(t), d, str(note)) for t, note in self.med])
            self.conn.commit()
            self.raw, self.feat, self.med = [], [], []
        except Exception as e:
            print(f"[db] write failed, will retry: {_db_error(e, self.url)}", file=sys.stderr)
            try:
                if self.conn:
                    self.conn.close()
            except Exception:
                pass
            self.conn = None
            if len(self.raw) > self.MAX_BUFFERED_RAW:
                self.raw = self.raw[-self.MAX_BUFFERED_RAW:]
            time.sleep(2)

    def run(self):
        while not self.stop_flag.is_set():
            self._drain(1.0)
            self._flush()
        self._drain(0.1)
        self._flush()

    def close(self):
        self.stop_flag.set()
        self.join(timeout=15)


# ----------------------------------------------------------------------------
# Self-test: synthetic tremor through the same math (no sensor or database)
# ----------------------------------------------------------------------------

def selftest():
    fs, n = 100.0, 256
    rng = np.random.default_rng(0)
    t = np.arange(n) / fs
    print("Synthetic signals through tremor_features():")
    cases = [("5.0 Hz tremor, 0.10 g", 5.0, 0.10),
             ("8.3 Hz tremor, 0.05 g", 8.3, 0.05),
             ("no tremor (noise + slow arm move)", None, 0.0)]
    ok = True
    for label, f0, amp in cases:
        g = np.array([0.0, 0.0, 1.0])                       # gravity on z
        slow = 0.2 * np.sin(2 * np.pi * 0.5 * t)[:, None] * np.array([1, 0, 0])
        noise = rng.normal(0, 0.004, size=(n, 3))
        sig = g + slow + noise
        if f0:
            # tremor mostly on x/y, with arbitrary phase
            sig += amp * np.sin(2 * np.pi * f0 * t + 0.7)[:, None] * np.array([0.8, 0.6, 0.0])
        f = tremor_features(sig, fs)
        tr = is_tremor(f, 0.015, 0.5)
        print(f"  {label:36s} -> {f['peak_hz']:5.2f} Hz  rms {f['rms_g']:.3f} g  "
              f"ratio {f['band_ratio']:.2f}  peak_share {f['peak_share']:.2f}  tremor={tr}")
        if f0:
            ok &= abs(f["peak_hz"] - f0) < 0.3 and tr
        else:
            ok &= not tr
    # One lift: a sharp start and stop about a second apart. The old rule calls
    # this tremor; the power is spread across the band, so peak_share rejects it.
    lift = g + slow + rng.normal(0, 0.004, size=(n, 3))
    lift_t, lift_gap, lift_width, lift_amp = 0.9, 0.8, 0.02, 0.5
    pulse = np.exp(-0.5 * ((t - lift_t) / lift_width) ** 2)
    pulse -= np.exp(-0.5 * ((t - (lift_t + lift_gap)) / lift_width) ** 2)
    lift[:, 0] += lift_amp * pulse
    f = tremor_features(lift, fs)
    tr = is_tremor(f, 0.015, 0.5)
    print(f"  {'1 s jerk (lifting a glass)':36s} -> {f['peak_hz']:5.2f} Hz  rms {f['rms_g']:.3f} g  "
          f"ratio {f['band_ratio']:.2f}  peak_share {f['peak_share']:.2f}  tremor={tr}")
    ok &= (not tr) and f["rms_g"] >= 0.015 and f["band_ratio"] >= 0.5 and f["peak_share"] < 0.45
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


# ----------------------------------------------------------------------------
# Squeeze calibration and detection (Myo muscle sensors)
# ----------------------------------------------------------------------------

def squeeze_threshold(relaxed_rms: float, squeezing_rms: float) -> float:
    """Return the dose threshold, 60% of the way from relaxed to squeezing."""
    return relaxed_rms + SQUEEZE_FRACTION * (squeezing_rms - relaxed_rms)


def emg_rms(samples: list[tuple[int, ...]]) -> float:
    """RMS across every channel of one block of muscle samples."""
    if not samples:
        return 0.0
    values = np.asarray(samples, dtype=float)
    return float(np.sqrt(np.mean(values * values)))


def window_rms_values(
    timed: list[tuple[float, tuple[int, ...]]], window_s: float = EMG_WINDOW_S
) -> list[float]:
    """RMS of each full ``window_s`` block. Each item is ``(time, 8 channels)``."""
    if len(timed) < 2:
        return []
    times = np.asarray([stamp for stamp, _sample in timed], dtype=float)
    emg = np.asarray([sample for _stamp, sample in timed], dtype=float)
    cursor = float(times[0])
    last = float(times[-1])
    values: list[float] = []
    while cursor + window_s <= last + 1e-6:
        mask = (times >= cursor) & (times < cursor + window_s)
        if np.any(mask):
            block = emg[mask]
            values.append(float(np.sqrt(np.mean(block * block))))
        cursor += window_s
    return values


class MotionLevel:
    """How much the hand is moving, in g, after a 1 s average is removed.

    ``level`` is the RMS of the per-axis residual over the last 0.5 s. A still
    fist is near zero. Shaking leaves the tremor in the residual.
    """

    def __init__(self) -> None:
        """Start with no samples."""
        self._samples: collections.deque[tuple[float, float, float, float]] = collections.deque()

    def push(self, stamp: float, ax: float, ay: float, az: float) -> None:
        """Add one acceleration sample. ``stamp`` is seconds on the sample clock."""
        self._samples.append((stamp, ax, ay, az))
        cutoff = stamp - (MOTION_AVG_S + MOTION_RMS_S + 0.5)
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def level(self, at: float | None = None) -> float | None:
        """RMS residual at ``at``, or None until 1.5 s of samples exist.

        If ``at`` is slightly ahead of the newest sample, use the newest sample.
        """
        if len(self._samples) < 2:
            return None
        newest = self._samples[-1][0]
        end = newest if at is None else at
        if end > newest:
            if end - newest > 0.5:
                return None
            end = newest
        rows = [sample for sample in self._samples if sample[0] <= end + 1e-9]
        if len(rows) < 2 or rows[0][0] > end - MOTION_AVG_S - MOTION_RMS_S + 0.05:
            return None
        times = np.asarray([sample[0] for sample in rows], dtype=float)
        xyz = np.asarray([sample[1:] for sample in rows], dtype=float)
        in_window = np.flatnonzero(times >= end - MOTION_RMS_S)
        if in_window.size == 0:
            return None
        residuals: list[np.ndarray] = []
        for index in in_window:
            stamp = times[index]
            block = xyz[(times >= stamp - MOTION_AVG_S) & (times <= stamp)]
            residuals.append(xyz[index] - block.mean(axis=0))
        resid = np.asarray(residuals, dtype=float)
        return float(np.sqrt(np.mean(np.sum(resid * resid, axis=1))))


def stillness_limit(squeeze_motion: float) -> float:
    """Return ``max(0.05 g, 2 × the motion measured while squeezing)``."""
    return max(STILL_G_DEFAULT, 2.0 * squeeze_motion)


def is_still_squeeze(rms: float, motion: float | None, threshold: float, still_g: float) -> bool:
    """True when muscle activity is above the threshold and the hand is still."""
    return motion is not None and rms >= threshold and motion < still_g


def save_calibration(
    path: Path,
    relaxed: float,
    squeezing: float,
    threshold: float,
    still_g: float,
    squeeze_motion: float | None,
) -> None:
    """Write the muscle threshold and the stillness limit."""
    payload = {
        "relaxed_rms": relaxed,
        "squeezing_rms": squeezing,
        "threshold": threshold,
        "fraction_from_relaxed": SQUEEZE_FRACTION,
        "still_g": still_g,
        "squeeze_motion_g": squeeze_motion,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def load_calibration(path: Path) -> tuple[float, float] | None:
    """Return ``(threshold, still_g)``, or None when the file is absent.

    A file saved before stillness was added uses 0.05 g.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        threshold = float(data["threshold"])
        still_g = float(data["still_g"]) if "still_g" in data else STILL_G_DEFAULT
        return threshold, still_g
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Could not read {path} ({exc}). Run --calibrate again.") from exc


def _activity_level(values: list[float], what: str) -> float | None:
    """Median RMS of one recording. Prints and returns None when samples are missing."""
    if len(values) < 3:
        print(
            f"Not enough muscle samples while recording {what}. Check the Myo and try again.",
            file=sys.stderr,
        )
        return None
    return float(np.median(np.asarray(values, dtype=float)))


def _import_myo_source():
    """Import MyoSource. Raise RuntimeError when the laptop packages are missing."""
    try:
        from myo_source import MyoSource
    except ImportError as exc:
        raise RuntimeError(
            "Myo support is not installed "
            f"({exc}). Install bleak==0.22.3, then: pip install --no-deps dl-myo==1.1.8"
        ) from exc
    return MyoSource


def _squeeze_motion(
    samples: list[tuple[float, float, float, float]], t0: float
) -> float | None:
    """Median motion at and after ``t0``. Earlier samples warm up the 1 s average."""
    level = MotionLevel()
    values: list[float] = []
    for stamp, ax, ay, az in samples:
        level.push(stamp, ax, ay, az)
        if stamp < t0:
            continue
        motion = level.level(stamp)
        if motion is not None:
            values.append(motion)
    if len(values) < 3:
        return None
    return float(np.median(np.asarray(values, dtype=float)))


def calibrate_myo(path: Path = CALIBRATION_PATH) -> int:
    """Record 5 s relaxed and 3 squeezes, then save the threshold and stillness limit."""
    try:
        MyoSource = _import_myo_source()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    samples: list[tuple[float, tuple[int, ...]]] = []
    motion_samples: list[tuple[float, float, float, float]] = []
    lock = threading.Lock()

    def on_emg(when, emg) -> None:
        with lock:
            samples.append((when.timestamp(), tuple(int(value) for value in emg)))

    def on_motion(when, ax: float, ay: float, az: float) -> None:
        with lock:
            motion_samples.append((when.timestamp(), float(ax), float(ay), float(az)))

    def capture(seconds: float) -> tuple[list[float], float | None]:
        with lock:
            emg_i = len(samples)
            mot_i = len(motion_samples)
        time.sleep(seconds)
        with lock:
            emg_chunk = list(samples[emg_i:])
            mot_all = list(motion_samples)
        motion = None
        if mot_i < len(mot_all):
            motion = _squeeze_motion(mot_all, mot_all[mot_i][0])
        return window_rms_values(emg_chunk), motion

    try:
        band = MyoSource(on_motion=on_motion, on_emg=on_emg)
    except Exception as exc:
        print(f"Could not start the Myo: {exc}", file=sys.stderr)
        return 1

    relaxed: float | None = None
    squeeze_levels: list[float] = []
    squeeze_motions: list[float] = []
    try:
        print("Connecting to the Myo. Relax your arm and hold it still.")
        try:
            band.start()
        except KeyboardInterrupt:
            print("\nCalibration cancelled.")
            return 1
        except Exception as exc:
            print(f"Could not start the Myo: {exc}", file=sys.stderr)
            return 1
        print(f"Recording relaxed muscle activity for {RELAX_RECORD_S:.0f} seconds.")
        relaxed_windows, _relaxed_motion = capture(RELAX_RECORD_S)
        relaxed = _activity_level(relaxed_windows, "the relaxed arm")
        if relaxed is None:
            return 1
        print(f"Relaxed RMS {relaxed:.1f}")
        for number in range(1, SQUEEZE_COUNT + 1):
            print(f"Relax. Squeeze {number} of {SQUEEZE_COUNT} starts in 2 seconds.")
            time.sleep(2.0)
            print("Squeeze and hold. Keep the hand still.")
            windows, motion = capture(SQUEEZE_RECORD_S)
            level = _activity_level(windows, f"squeeze {number}")
            if level is None:
                return 1
            if motion is None:
                print(f"Squeeze {number} RMS {level:.1f}, motion unavailable")
            else:
                print(f"Squeeze {number} RMS {level:.1f}, motion {motion:.3f} g")
                squeeze_motions.append(motion)
            squeeze_levels.append(level)
    except KeyboardInterrupt:
        print("\nCalibration cancelled.")
        return 1
    finally:
        band.stop()

    if relaxed is None:
        return 1
    squeezing = float(np.median(np.asarray(squeeze_levels, dtype=float)))
    if squeezing <= relaxed:
        print(
            "The squeezes were not stronger than the relaxed arm "
            f"(relaxed {relaxed:.1f}, squeezing {squeezing:.1f}). Nothing was saved.",
            file=sys.stderr,
        )
        return 1
    threshold = squeeze_threshold(relaxed, squeezing)
    if squeeze_motions:
        squeeze_motion = float(np.median(np.asarray(squeeze_motions, dtype=float)))
        still_g = stillness_limit(squeeze_motion)
    else:
        squeeze_motion = None
        still_g = STILL_G_DEFAULT
        print(
            "No accelerometer samples during the squeezes. "
            f"Stillness limit stays {still_g:.3f} g.",
            file=sys.stderr,
        )
    save_calibration(path, relaxed, squeezing, threshold, still_g, squeeze_motion)
    print(
        f"Threshold {threshold:.1f} is 60% of the way from relaxed "
        f"{relaxed:.1f} to squeezing {squeezing:.1f}."
    )
    if squeeze_motion is None:
        print(f"Stillness limit {still_g:.3f} g.")
    else:
        print(
            f"Squeeze motion {squeeze_motion:.3f} g. "
            f"Stillness limit {still_g:.3f} g is max(0.050, twice that)."
        )
    print(f"Saved {path}")
    return 0


class SqueezeDetector:
    """Log a dose when a still squeeze lasts 1.5 s.

    Muscle activity is a 0.5 s average of the 200 ms RMS, so one noisy window
    cannot drop it through the threshold. The squeeze starts above the
    threshold and continues until the average falls below 80% of it. Brief
    motion is allowed: the dose still counts when at least 80% of the last
    1.5 s was still. After a dose, the next one waits for the lockout and a
    1 s release.
    """

    def __init__(
        self,
        threshold: float,
        on_dose: Callable[[], None],
        lockout_s: float = SQUEEZE_IGNORE_S,
        still_g: float = STILL_G_DEFAULT,
        motion: MotionLevel | None = None,
        dry_run: bool = False,
    ) -> None:
        """``on_dose`` runs once per armed hold. ``dry_run`` explains a missed dose."""
        self.threshold = threshold
        self.lockout_s = lockout_s
        self.still_g = still_g
        self._motion = motion
        self._dry_run = dry_run
        self._on_dose = on_dose
        self._buf: list[tuple[int, ...]] = []
        self._window_start: float | None = None
        self._rms_hist: collections.deque[tuple[float, float]] = collections.deque()
        self._still_hist: collections.deque[tuple[float, float, bool]] = collections.deque()
        self._in_squeeze = False
        self._squeeze_start: float | None = None
        self._crossed_at: float | None = None
        self._logged = False
        self._below_since: float | None = None
        self._locked_until = 0.0
        self._armed = True

    def push(self, stamp: float, emg: tuple[int, ...]) -> None:
        """Add one 8-channel sample. ``stamp`` is seconds (the sample clock)."""
        if self._window_start is None:
            self._window_start = stamp
        elif stamp - self._window_start >= EMG_WINDOW_S and self._buf:
            self._finish(self._window_start, stamp)
            self._buf = []
            self._window_start = stamp
        self._buf.append(emg)

    def _smoothed(self, end: float) -> float | None:
        """Mean RMS over the last 0.5 s, or None until that average is full."""
        recent = [(stamp, rms) for stamp, rms in self._rms_hist if stamp >= end - SMOOTH_S - 1e-9]
        if not recent:
            return None
        oldest = min(stamp for stamp, _rms in recent)
        if end - (oldest - EMG_WINDOW_S) < SMOOTH_S - 0.02:
            return None
        return float(sum(rms for _stamp, rms in recent) / len(recent))

    def _still_enough(self) -> bool:
        """True when moving time in the last 1.5 s is at most 20%."""
        moving = sum(dur for _stamp, dur, still in self._still_hist if not still)
        return moving <= (1.0 - STILL_HOLD_FRACTION) * SQUEEZE_HOLD_S + 1e-6

    def _finish(self, start: float, end: float) -> None:
        rms = emg_rms(self._buf)
        self._rms_hist.append((end, rms))
        while self._rms_hist and self._rms_hist[0][0] < end - SMOOTH_S - 1.0:
            self._rms_hist.popleft()
        motion = None if self._motion is None else self._motion.level(end)
        still = motion is not None and motion < self.still_g
        self._still_hist.append((end, max(0.0, end - start), still))
        while self._still_hist and self._still_hist[0][0] < end - SQUEEZE_HOLD_S:
            self._still_hist.popleft()
        smoothed = self._smoothed(end)
        if smoothed is None:
            return
        release = RELEASE_FRACTION * self.threshold
        if self._in_squeeze:
            self._continue_squeeze(start, end, smoothed, release)
            return
        if smoothed < release:
            if self._below_since is None:
                self._below_since = start
            self._rearm(end)
        else:
            self._below_since = None
        if smoothed >= self.threshold:
            self._in_squeeze = True
            self._logged = False
            self._crossed_at = end
            # The average is already 0.5 s of this squeeze, so the dose clock starts then.
            self._squeeze_start = end - SMOOTH_S
            self._continue_squeeze(start, end, smoothed, release)

    def _continue_squeeze(self, start: float, end: float, smoothed: float, release: float) -> None:
        """Log at 1.5 s, or explain when the squeeze ends without a dose."""
        if self._squeeze_start is None:
            return
        held = end - self._squeeze_start
        still_ok = self._still_enough()
        if self._armed and not self._logged and held >= SQUEEZE_HOLD_S and still_ok:
            self._logged = True
            self._armed = False
            self._locked_until = end + self.lockout_s
            self._below_since = None
            self._on_dose()
        if smoothed >= release:
            return
        if not self._logged:
            shown = end - self._crossed_at if self._crossed_at is not None else held
            self._explain(shown, still_ok)
        self._in_squeeze = False
        self._squeeze_start = None
        self._crossed_at = None
        self._logged = False
        self._below_since = start
        self._rearm(end)

    def _rearm(self, end: float) -> None:
        """Arm the next dose after the lockout and a 1 s release."""
        if self._armed or self._below_since is None or self._in_squeeze:
            return
        if end - self._below_since >= REARM_BELOW_S and end >= self._locked_until:
            self._armed = True
            self._below_since = None
            print("dose detector re-armed")

    def _explain(self, held: float, still_ok: bool) -> None:
        """In a dry run, say why this squeeze did not log a dose."""
        if not self._dry_run:
            return
        if not self._armed:
            print("too soon after last dose")
        elif held < SQUEEZE_HOLD_S:
            print(f"released too early (held {held:.1f} s)")
        elif not still_ok:
            print("too much motion")
        else:
            print(f"released too early (held {held:.1f} s)")


class GestureMask:
    """Remember still squeezes so tremor windows that overlap one can be skipped.

    A run is muscle activity at or above the threshold while the hand is still,
    including squeezes shorter than 1.5 s. Shaking does not open a run. Coverage
    starts 0.5 s before the run and ends 1.5 s after it. This does not log doses.
    """

    def __init__(self, threshold: float, still_g: float, motion: MotionLevel) -> None:
        """``threshold`` is the calibrated squeeze RMS. ``still_g`` is ``--still-g``."""
        self.threshold = threshold
        self.still_g = still_g
        self._motion = motion
        self.latest_time: float | None = None
        self._buf: list[tuple[int, ...]] = []
        self._window_start: float | None = None
        self._run_start: float | None = None
        self._run_end: float | None = None
        self._run_motion: float | None = None
        self._runs: list[tuple[float, float, float]] = []

    def push(self, stamp: float, emg: tuple[int, ...]) -> None:
        """Add one 8-channel sample. ``stamp`` is seconds on the sample clock."""
        self.latest_time = stamp
        if self._window_start is None:
            self._window_start = stamp
        elif stamp - self._window_start >= EMG_WINDOW_S and self._buf:
            self._finish(self._window_start, stamp)
            self._buf = []
            self._window_start = stamp
        self._buf.append(emg)

    def covering_motion(self, start: float, end: float) -> float | None:
        """Motion of a still squeeze overlapping ``[start, end]``, else None."""
        best_end: float | None = None
        best_motion: float | None = None
        kept: list[tuple[float, float, float]] = []
        for run_start, run_end, run_motion in self._runs:
            if run_end + MASK_AFTER_S <= start:
                continue
            kept.append((run_start, run_end, run_motion))
            if self._overlaps(run_start, run_end, start, end) and (
                best_end is None or run_end >= best_end
            ):
                best_end = run_end
                best_motion = run_motion
        self._runs = kept
        if self._overlaps(self._run_start, self._run_end, start, end) and (
            best_end is None or (self._run_end is not None and self._run_end >= best_end)
        ):
            best_motion = self._run_motion
        return best_motion

    def _finish(self, start: float, end: float) -> None:
        rms = emg_rms(self._buf)
        motion = self._motion.level(end)
        if is_still_squeeze(rms, motion, self.threshold, self.still_g):
            if self._run_start is None:
                self._run_start = start
            self._run_end = end
            self._run_motion = motion
            return
        if self._run_start is None or self._run_end is None or self._run_motion is None:
            self._run_start = None
            self._run_end = None
            self._run_motion = None
            return
        self._runs.append((self._run_start, self._run_end, self._run_motion))
        self._run_start = None
        self._run_end = None
        self._run_motion = None

    @staticmethod
    def _overlaps(run_start: float | None, run_end: float | None, start: float, end: float) -> bool:
        if run_start is None or run_end is None:
            return False
        return start < run_end + MASK_AFTER_S and end > run_start - MASK_BEFORE_S


class EmgBuffer:
    """Recent 8-channel muscle samples on the same clock as the motion window."""

    def __init__(self, keep_s: float = 12.0) -> None:
        """Keep ``keep_s`` seconds, enough for one window plus the squeeze mask."""
        self.keep_s = keep_s
        self.latest_time: float | None = None
        self._samples: collections.deque[tuple[float, tuple[int, ...]]] = collections.deque()

    def push(self, stamp: float, emg: tuple[int, ...]) -> None:
        """Add one sample. ``stamp`` is seconds on the sample clock."""
        self.latest_time = stamp
        self._samples.append((stamp, tuple(int(value) for value in emg)))
        cutoff = stamp - self.keep_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def covering(self, start: float, end: float) -> tuple[np.ndarray, np.ndarray] | None:
        """Return ``(times, values)`` for samples in ``[start, end]``, or None if too few."""
        rows = [row for row in self._samples if start - 1e-6 <= row[0] <= end + 1e-6]
        if len(rows) < EMG_MIN_SAMPLES:
            return None
        rows.sort(key=lambda row: row[0])
        times = np.asarray([row[0] for row in rows], dtype=float)
        values = np.asarray([row[1] for row in rows], dtype=float)
        return times, values


def muscle_window(
    emg: EmgBuffer | None,
    start: float,
    end: float,
    motion_peak_hz: float,
    tremor: bool,
) -> tuple[float | None, bool | None]:
    """Return ``(emg_peak_hz, emg_confirmed)`` for one motion window.

    The peak is measured on every window the muscle samples cover. The flag is
    evaluated only when ``tremor`` is true: it is true when the muscle peak is
    within 1 Hz of ``motion_peak_hz`` and holds at least 0.3 of the band power.
    Otherwise the flag is None. Both are None when there is no muscle data.
    """
    if emg is None:
        return None, None
    covered = emg.covering(start, end)
    if covered is None:
        return None, None
    found = emg_rhythm(covered[0], covered[1])
    if found is None:
        return None, None
    peak = found["peak_hz"]
    if not tremor:
        return peak, None
    return peak, muscle_confirms(peak, motion_peak_hz, found["peak_share"])


def _emg_mark(peak_hz: float | None, confirmed: bool | None) -> str:
    """Dry-run text: the muscle peak, and muscle-confirmed when the flag is true."""
    if peak_hz is None:
        return "emg     —  "
    mark = "  muscle-confirmed" if confirmed else ""
    return f"emg {peak_hz:5.2f} Hz{mark}  "


# ----------------------------------------------------------------------------
# Shared sample window, dose logging, and the three sources
# ----------------------------------------------------------------------------

_dose_lock = threading.Lock()


def log_dose(writer: TigerWriter | None, feedback: Callable[[], None] | None) -> None:
    """Record one dose and give the source's feedback (buzz, beep, or nothing)."""
    with _dose_lock:
        now = time.time()
        print(f"\n💊 medication logged at {time.strftime('%H:%M:%S')}\n")
        if writer is not None:
            writer.put("med", (now, "dose"))
        if feedback is None:
            return
        try:
            feedback()
        except Exception as exc:
            print(f"(feedback failed: {exc})", file=sys.stderr)


def watch_enter(on_enter: Callable[[], None], stop: threading.Event) -> None:
    """Log a dose when the user presses Enter."""

    def loop() -> None:
        while not stop.is_set():
            try:
                line = sys.stdin.readline()
            except (KeyboardInterrupt, InterruptedError):
                return
            if line == "" or stop.is_set():
                return
            on_enter()

    threading.Thread(target=loop, name="enter-dose", daemon=True).start()


def install_stop(stop: threading.Event) -> None:
    """Stop the sample loop on Ctrl+C or SIGTERM."""

    def handle(*_args: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)


class SampleLog:
    """Window samples, write the CSV, and hand rows to the database thread."""

    def __init__(
        self,
        args: argparse.Namespace,
        writer: TigerWriter | None,
        csv_w: csv.writer | None,
        mask: GestureMask | None = None,
        emg: EmgBuffer | None = None,
    ) -> None:
        """Keep a window of ``args.window`` samples and analyse every ``args.hop``.

        ``mask`` skips tremor rows that overlap a squeeze. Raw samples are still stored.
        ``emg`` supplies the muscle samples for the same time span. Grove and
        simulate leave it unset, so their muscle columns stay null.
        """
        self.args = args
        self.writer = writer
        self.csv_w = csv_w
        self.mask = mask
        self.emg = emg
        self.window: collections.deque[tuple[float, float, float, float]] = collections.deque(
            maxlen=args.window
        )
        self.raw_batch: list[tuple[float, float, float, float]] = []
        self.since_last = 0
        self.last_batch_send = time.time()
        self._pending: list[tuple[float, np.ndarray]] = []
        self._tremor_streak = 0

    def push(self, window_t: float, wall_t: float, ax: float, ay: float, az: float) -> None:
        """Add one acceleration sample. ``window_t`` sets the measured rate."""
        if self.window:
            previous = self.window[-1][0]
            if window_t < previous or window_t - previous > 0.5:
                self.window.clear()
                self.since_last = 0
                self._tremor_streak = 0
        self.window.append((window_t, ax, ay, az))
        self.raw_batch.append((wall_t, ax, ay, az))
        self.since_last += 1
        if self.csv_w is not None:
            self.csv_w.writerow([f"{wall_t:.4f}", f"{ax:.5f}", f"{ay:.5f}", f"{az:.5f}"])
        if self.writer and wall_t - self.last_batch_send >= 1.0:
            for row in self.raw_batch:
                self.writer.put("raw", row)
            self.raw_batch = []
            self.last_batch_send = wall_t
        elif self.writer is None and len(self.raw_batch) > 1000:
            self.raw_batch = []
        if len(self.window) == self.args.window and self.since_last >= self.args.hop:
            self.since_last = 0
            snapshot = np.array(self.window)
            if self.mask is None and self.emg is None:
                self._analyze(wall_t, snapshot)
            else:
                self._pending.append((wall_t, snapshot))
                self.release_pending()

    def release_pending(self, force: bool = False) -> None:
        """Write queued windows once muscle samples cover the motion span.

        A squeeze mask also waits until a squeeze starting 0.5 s after the
        window would already be visible.
        """
        if not self._pending:
            return
        latest = self._muscle_time()
        motion_now = self.window[-1][0] if self.window else None
        wait_s = (MASK_BEFORE_S + EMG_WINDOW_S) if self.mask is not None else 0.0
        still: list[tuple[float, np.ndarray]] = []
        for wall_t, samples in self._pending:
            window_end = float(samples[-1, 0])
            covered = latest is not None and latest >= window_end + wait_s
            stalled = motion_now is not None and motion_now >= window_end + wait_s + 1.0
            if force or covered or stalled:
                self._analyze(wall_t, samples)
            else:
                still.append((wall_t, samples))
        self._pending = still

    def _muscle_time(self) -> float | None:
        """Newest muscle-sample time, used to decide when a window can be analysed."""
        if self.emg is not None and self.emg.latest_time is not None:
            return self.emg.latest_time
        if self.mask is not None:
            return self.mask.latest_time
        return None

    def _analyze(self, wall_t: float, samples: np.ndarray) -> None:
        """Print one window and store its features, unless a squeeze covers it."""
        arr = np.asarray(samples, dtype=float)
        if self.mask is not None:
            paused = self.mask.covering_motion(float(arr[0, 0]), float(arr[-1, 0]))
            if paused is not None:
                self._tremor_streak = 0
                print(
                    f"{time.strftime('%H:%M:%S')}  squeeze: tremor check paused  "
                    f"motion {paused:.3f} g"
                )
                return
        span = arr[-1, 0] - arr[0, 0]
        fs = (len(arr) - 1) / span if span > 0 else self.args.rate
        features = tremor_features(arr[:, 1:], fs)
        hit = is_tremor(
            features, self.args.min_rms, self.args.min_ratio, self.args.min_peak_share
        )
        self._tremor_streak = self._tremor_streak + 1 if hit else 0
        tremor = self._tremor_streak >= self.args.min_windows
        emg_peak_hz, emg_confirmed = muscle_window(
            self.emg, float(arr[0, 0]), float(arr[-1, 0]), features["peak_hz"], tremor
        )
        bar = "█" * min(40, int(features["rms_g"] * 400))
        emg_text = _emg_mark(emg_peak_hz, emg_confirmed) if self.emg is not None else ""
        print(f"{time.strftime('%H:%M:%S')}  {features['peak_hz']:5.2f} Hz  "
              f"{features['rms_g']:.3f} g  ratio {features['band_ratio']:.2f}  "
              f"peak_share {features['peak_share']:.2f}  fs {fs:5.1f}  "
              f"{'TREMOR ' if tremor else '       '}{emg_text}{bar}")
        if self.writer:
            self.writer.put("feat", (
                wall_t, features["peak_hz"], features["rms_g"], features["band_ratio"], tremor,
                emg_peak_hz, emg_confirmed,
            ))

    def flush(self) -> None:
        """Send any raw samples still waiting for the once-a-second batch."""
        self.release_pending(force=True)
        if self.writer is None:
            return
        for row in self.raw_batch:
            self.writer.put("raw", row)
        self.raw_batch = []


def close_run(
    log: SampleLog | None,
    writer: TigerWriter | None,
    csv_file,
    band=None,
    button=None,
    bus=None,
    close_buzzer: Callable[[], None] | None = None,
    started: bool = False,
) -> None:
    """Flush samples and release the database, CSV, and hardware."""
    if started:
        print("\nStopping...")
    if band is not None:
        band.stop()
    if log is not None:
        log.flush()
    if writer is not None:
        writer.close()
    if csv_file is not None:
        csv_file.close()
    if button is not None:
        button.close()
    if close_buzzer is not None:
        close_buzzer()
    if bus is not None:
        bus.close()


def poll_samples(args: argparse.Namespace, log: SampleLog, read_xyz: Callable[[], tuple[float, float, float]], stop: threading.Event) -> str | None:
    """Read at ``args.rate`` until ``stop``. Return an error string if the sensor drops."""
    period = 1.0 / args.rate
    next_t = time.perf_counter()
    errors = 0
    print("Sampling... (Ctrl+C to stop)\n")
    while not stop.is_set():
        try:
            ax, ay, az = read_xyz()
            errors = 0
        except OSError as exc:
            errors += 1
            if errors > 50:
                return f"Lost the sensor ({exc}). Check the wiring."
            time.sleep(0.01)
            continue
        now_wall, now_mono = time.time(), time.perf_counter()
        log.push(now_mono, now_wall, ax, ay, az)
        next_t += period
        delay = next_t - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.perf_counter()       # fell behind; resync
    return None


def _mode_label(args: argparse.Namespace) -> str:
    return "DRY RUN" if args.dry_run else "writing to Tiger Data"


def _settings_line(args: argparse.Namespace) -> str:
    return (
        f"{args.rate:g} Hz, window {args.window}, hop {args.hop}, "
        f"min-rms {args.min_rms}, min-ratio {args.min_ratio}, "
        f"min-peak-share {args.min_peak_share}, min-windows {args.min_windows}"
    )


def open_buzzer(pin: int) -> tuple[Callable[[], None], Callable[[], None]]:
    """Return ``(beep, close)`` for the Grove buzzer. Beep is a no-op if setup fails."""
    try:
        from gpiozero import Buzzer
        buzzer = Buzzer(pin)
    except Exception as exc:
        print(f"(buzzer disabled: {exc})")
        return (lambda: None), (lambda: None)

    def beep() -> None:
        buzzer.beep(on_time=0.15, off_time=0.05, n=1, background=True)

    print(f"Buzzer on GPIO{pin}")
    return beep, buzzer.close


def open_button(pin: int, on_press: Callable[[], None]):
    """Arm the Grove button, or return None when GPIO is unavailable."""
    try:
        from gpiozero import Button
        button = Button(pin, pull_up=False, bounce_time=0.05)
        button.when_pressed = on_press
    except Exception as exc:
        print(f"(button disabled: {exc})")
        return None
    print(f"Medication button on GPIO{pin}")
    return button


def run_myo(args: argparse.Namespace, writer: TigerWriter | None, csv_file, csv_w) -> None:
    """Stream the Myo, detect a 1.5 s squeeze, and log doses."""
    motion_q: queue.Queue[tuple[float, float, float, float]] = queue.Queue()
    emg_q: queue.Queue[tuple[float, tuple[int, ...]]] = queue.Queue()

    def on_motion(when, ax: float, ay: float, az: float) -> None:
        motion_q.put((when.timestamp(), float(ax), float(ay), float(az)))

    def on_emg(when, emg) -> None:
        emg_q.put((when.timestamp(), tuple(int(value) for value in emg)))

    band = None
    logs: list[SampleLog] = []
    stop = threading.Event()
    failed: str | None = None
    connected = False
    try:
        band, failed = _open_myo(on_motion, on_emg)
        if band is None or failed is not None:
            return
        print("Connecting to the Myo...")
        try:
            band.start()
        except KeyboardInterrupt:
            return
        except Exception as exc:
            failed = f"Could not start the Myo: {exc}"
            return
        connected = True
        failed = _sample_myo(args, writer, csv_w, band, motion_q, emg_q, stop, logs)
    finally:
        stop.set()
        close_run(logs[0] if logs else None, writer, csv_file, band=band, started=connected)
        if failed:
            print(failed, file=sys.stderr)
            sys.exit(1)


def _open_myo(on_motion, on_emg):
    """Return ``(band, error)``. ``error`` is set when the band cannot be constructed."""
    try:
        MyoSource = _import_myo_source()
        return MyoSource(on_motion=on_motion, on_emg=on_emg), None
    except RuntimeError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"Could not start the Myo: {exc}"


def _sample_myo(args, writer, csv_w, band, motion_q, emg_q, stop, logs: list[SampleLog]) -> str | None:
    """Read queued Myo samples until ``stop``. Append the log to ``logs`` and return an error."""

    def buzz() -> None:
        band.vibrate("short")

    try:
        loaded = load_calibration(CALIBRATION_PATH)
    except RuntimeError as exc:
        return str(exc)
    detector: SqueezeDetector | None = None
    mask: GestureMask | None = None
    motion = MotionLevel()
    if loaded is None:
        print(f"No {CALIBRATION_PATH.name}. Squeeze logging is off until you run --calibrate.")
    else:
        threshold, saved_still = loaded
        still_g = args.still_g if args.still_g is not None else saved_still
        detector = SqueezeDetector(
            threshold, lambda: log_dose(writer, buzz), args.dose_lockout, still_g, motion,
            args.dry_run,
        )
        mask = GestureMask(threshold, still_g, motion)
        print(
            f"Squeeze threshold {threshold:.1f}. Stillness limit {still_g:.3f} g. "
            f"Hold a still fist for 1.5 s to log a dose. Lockout {args.dose_lockout:g} s."
        )
    print(f"Myo armband | device '{args.device}' | {_mode_label(args)}")
    print(_settings_line(args))
    print("Press Enter to log a dose.")
    emg_buf = EmgBuffer()
    log = SampleLog(args, writer, csv_w, mask, emg_buf)
    logs.append(log)
    install_stop(stop)
    watch_enter(lambda: log_dose(writer, buzz), stop)
    print("Sampling... (Ctrl+C to stop)\n")
    while not stop.is_set():
        pending = _take_motion(motion_q)
        for wall, ax, ay, az in pending:
            motion.push(wall, ax, ay, az)
            log.push(wall, wall, ax, ay, az)
        _drain_emg(emg_q, detector, mask, emg_buf)
        log.release_pending()
    return None


def _take_motion(
    motion_q: queue.Queue[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Return queued acceleration samples, waiting up to 0.2 s for the first."""
    try:
        first = motion_q.get(timeout=0.2)
    except queue.Empty:
        return []
    pending = [first]
    while True:
        try:
            pending.append(motion_q.get_nowait())
        except queue.Empty:
            return pending


def _drain_emg(
    emg_q: queue.Queue[tuple[float, tuple[int, ...]]],
    detector: SqueezeDetector | None,
    mask: GestureMask | None = None,
    emg_buf: EmgBuffer | None = None,
) -> None:
    """Feed queued muscle samples to dose detection, the mask, and the rhythm buffer."""
    while True:
        try:
            stamp, emg = emg_q.get_nowait()
        except queue.Empty:
            return
        if emg_buf is not None:
            emg_buf.push(stamp, emg)
        if detector is not None:
            detector.push(stamp, emg)
        if mask is not None:
            mask.push(stamp, emg)


def run_grove(args: argparse.Namespace, writer: TigerWriter | None, csv_file, csv_w) -> None:
    """Poll the Grove accelerometer. The button or Enter logs a dose and beeps."""
    bus = None
    button = None
    close_buzzer: Callable[[], None] | None = None
    log: SampleLog | None = None
    started = False
    message: str | None = None
    try:
        bus = SMBus(1)
        sensor = detect_sensor(bus)
        print(f"Sensor: {sensor.name} at {hex(sensor.addr)} | device '{args.device}' | "
              f"{_mode_label(args)}")
        print(_settings_line(args))
        beep, close_buzzer = open_buzzer(args.buzzer_pin)
        button = None if args.no_button else open_button(args.button_pin, lambda: log_dose(writer, beep))
        print("Press Enter to log a dose.")
        log = SampleLog(args, writer, csv_w)
        stop = threading.Event()
        install_stop(stop)
        watch_enter(lambda: log_dose(writer, beep), stop)
        started = True
        message = poll_samples(args, log, sensor.read, stop)
    finally:
        close_run(
            log, writer, csv_file, button=button, bus=bus,
            close_buzzer=close_buzzer, started=started,
        )
    if message:
        sys.exit(message)


def simulated_accel(stamp: float) -> tuple[float, float, float]:
    """Fake a 5.2 Hz tremor of about 0.10 g, plus gravity on z."""
    amp = 0.10 * math.sin(2.0 * math.pi * SIM_TREMOR_HZ * stamp)
    return (amp * 0.8, amp * 0.6, 1.0)


def run_simulate(args: argparse.Namespace, writer: TigerWriter | None, csv_file, csv_w) -> None:
    """Feed a 5.2 Hz tremor through the same windowing as a real sensor."""
    log = SampleLog(args, writer, csv_w)
    origin = time.perf_counter()
    stop = threading.Event()
    message: str | None = None
    try:
        print(f"Simulated {SIM_TREMOR_HZ:.1f} Hz tremor | device '{args.device}' | {_mode_label(args)}")
        print(_settings_line(args))
        print("Press Enter to log a dose.")
        install_stop(stop)
        watch_enter(lambda: log_dose(writer, None), stop)

        def read_xyz() -> tuple[float, float, float]:
            return simulated_accel(time.perf_counter() - origin)

        message = poll_samples(args, log, read_xyz, stop)
    finally:
        close_run(log, writer, csv_file, started=True)
    if message:
        sys.exit(message)


def load_local_env() -> None:
    """Load the laptop ``.env`` when python-dotenv is installed. The Pi uses export."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parent / ".env")


def apply_source_settings(args: argparse.Namespace) -> None:
    """Fill omitted settings from the chosen source. Explicit flags win."""
    for key, value in SOURCE_SETTINGS[args.source].items():
        if getattr(args, key) is None:
            setattr(args, key, value)


def _require_ssl(url: str) -> str:
    """Return ``url`` when it requires SSL."""
    modes = parse_qs(urlparse(url).query).get("sslmode") or []
    mode = modes[-1].strip().lower() if modes else ""
    if mode not in {"require", "verify-ca", "verify-full"}:
        sys.exit("The database URL must require SSL (sslmode=require).")
    return url


def _db_error(exc: object, url: str) -> str:
    """One line of a database error with connection secrets removed."""
    cleaned = " ".join(str(exc).split())
    secrets = [url]
    for name in ("TIGER_WRITER_URL", "TIGER_URL", "TIGER_READER_URL"):
        secret = os.environ.get(name, "").strip()
        if secret:
            secrets.append(secret)
            parsed = urlparse(secret)
            secrets.extend(
                part for part in (parsed.hostname, parsed.username, parsed.password) if part
            )
    if url:
        parsed = urlparse(url)
        secrets.extend(
            part for part in (parsed.hostname, parsed.username, parsed.password) if part
        )
    for secret in sorted({part for part in secrets if part}, key=len, reverse=True):
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def open_writer(args: argparse.Namespace) -> TigerWriter | None:
    """Start the database thread, or return None for ``--dry-run``.

    Uses ``--url``, then ``TIGER_WRITER_URL``, then ``TIGER_URL``.
    """
    if args.dry_run:
        return None
    url = (
        args.url
        or os.environ.get("TIGER_WRITER_URL")
        or os.environ.get("TIGER_URL")
        or ""
    ).strip()
    if not url:
        sys.exit("Set TIGER_WRITER_URL (or pass --url), or use --dry-run.")
    writer = TigerWriter(_require_ssl(url), args.device)
    writer.start()
    return writer


def open_csv(path: str | None):
    """Open the raw-sample CSV, or return ``(None, None)``."""
    if not path:
        return None, None
    handle = open(path, "w", newline="")
    writer = csv.writer(handle)
    writer.writerow(["unix_time", "ax_g", "ay_g", "az_g"])
    return handle, writer


def parse_args() -> argparse.Namespace:
    """Parse flags. ``--selftest`` does not need a source."""
    parser = argparse.ArgumentParser(description="Tremor monitoring prototype ingest")
    parser.add_argument("--source", choices=("myo", "grove", "simulate"),
                        help="myo armband, Grove sensor on the Pi, or a simulated 5.2 Hz tremor")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="no database, just print")
    parser.add_argument("--calibrate", action="store_true",
                        help="record 5 s relaxed and 3 squeezes, then save myo_calibration.json")
    parser.add_argument("--url", default=None, help="Tiger Data URL (or set TIGER_WRITER_URL)")
    parser.add_argument("--device", default=None, help="device id (myo/simulate: tremor, grove: tremor-pi)")
    parser.add_argument("--rate", type=float, default=None, help="sample rate, Hz (grove and simulate)")
    parser.add_argument("--window", type=int, default=None, help="samples per analysis window")
    parser.add_argument("--hop", type=int, default=None, help="samples between analyses")
    parser.add_argument("--min-rms", type=float, default=None, dest="min_rms")
    parser.add_argument("--min-ratio", type=float, default=None, dest="min_ratio",
                        help="minimum band_ratio (myo, grove, and simulate default 0.5)")
    parser.add_argument("--min-peak-share", type=float, default=None, dest="min_peak_share",
                        help="minimum share of 3-12 Hz power within 1 Hz of the peak (myo 0.3)")
    parser.add_argument("--min-windows", type=int, default=None, dest="min_windows",
                        help="consecutive passing windows required before tremor=true (myo 3)")
    parser.add_argument("--dose-lockout", type=float, default=SQUEEZE_IGNORE_S, dest="dose_lockout",
                        help="seconds after a dose before a squeeze can log another")
    parser.add_argument("--still-g", type=float, default=None, dest="still_g",
                        help="motion below this (g) counts as a still squeeze (default 0.05, or the calibration)")
    parser.add_argument("--button-pin", type=int, default=None, dest="button_pin")
    parser.add_argument("--buzzer-pin", type=int, default=None, dest="buzzer_pin")
    parser.add_argument("--no-button", action="store_true")
    parser.add_argument("--log-csv", default=None, dest="log_csv")
    args = parser.parse_args()
    if args.selftest:
        return args
    if args.calibrate and args.source is None:
        args.source = "myo"
    if args.calibrate and args.source != "myo":
        parser.error("--calibrate is only for --source myo")
    if args.source is None:
        parser.error("choose --source myo, grove, or simulate")
    return args


def main() -> None:
    """Run a self-test, a Myo calibration, or one of the three sources."""
    args = parse_args()
    if args.selftest:
        sys.exit(selftest())
    load_local_env()
    apply_source_settings(args)
    if args.calibrate:
        sys.exit(calibrate_myo())
    if args.source == "grove" and SMBus is None:
        sys.exit("Missing I2C library: sudo apt install -y python3-smbus")
    writer = open_writer(args)
    csv_file, csv_w = open_csv(args.log_csv)
    if args.source == "myo":
        run_myo(args, writer, csv_file, csv_w)
    elif args.source == "grove":
        run_grove(args, writer, csv_file, csv_w)
    else:
        run_simulate(args, writer, csv_file, csv_w)


if __name__ == "__main__":
    main()
