"""Live gait + heart-rate engine shared by the Pi (Bluetooth) and the Mac (simulator).

All computing happens on the device: 100 Hz accelerometer sampling, the two-stage
gait detector from gait_algo.py once a second on the last 10 s, freeze cueing on
the buzzer, and heart rate from the MAX30102. Bluetooth only carries the results.

Monitoring prototype, not a medical device.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque

import numpy as np

import gait_algo
import gait_model

RATE_HZ = 100.0
ANALYSE_S = 10.0
CUE_FREQS = (3800, 3300)          # warble: the loudest sound on this buzzer


class Buzzer:
    """PWM buzzer with warble beeps and a background metronome."""

    def __init__(self, pin: int = 16) -> None:
        """Open GPIO ``pin``; stay silent if no GPIO is available (laptop)."""
        self.dev = None
        try:
            from gpiozero import PWMOutputDevice
            self.dev = PWMOutputDevice(pin, frequency=CUE_FREQS[0])
        except Exception:
            pass
        self.lock = threading.Lock()
        self.bpm = 0.0
        self._wake = threading.Event()
        threading.Thread(target=self._metro, daemon=True).start()

    def warble(self, seconds: float = 0.3) -> None:
        """Alternate the two cue pitches for ``seconds`` (blocking)."""
        with self.lock:
            if self.dev is None:
                time.sleep(seconds)
                return
            for i in range(max(1, int(seconds / 0.06))):
                self.dev.frequency = CUE_FREQS[i % 2]
                self.dev.value = 0.5
                time.sleep(0.06)
            self.dev.value = 0

    def beep_async(self, seconds: float = 0.3) -> None:
        """Warble in a background thread."""
        threading.Thread(target=self.warble, args=(seconds,), daemon=True).start()

    def metronome(self, bpm: float) -> None:
        """Start a steady cue beat at ``bpm`` (0 stops it)."""
        self.bpm = max(0.0, min(180.0, bpm))
        self._wake.set()

    def _metro(self) -> None:
        nxt = time.monotonic()
        while True:
            if self.bpm <= 0:
                self._wake.wait()
                self._wake.clear()
                nxt = time.monotonic()
                continue
            self.warble(0.12)
            nxt += 60.0 / max(30.0, self.bpm)
            time.sleep(max(0.0, nxt - time.monotonic()))

    def close(self) -> None:
        """Silence and release the buzzer."""
        self.bpm = 0
        if self.dev is not None:
            self.dev.value = 0
            self.dev.close()


class SimSensor:
    """Plays simulated ankle sessions in real time (laptop demo, no hardware)."""

    def __init__(self, seed: int = 3) -> None:
        """Build a long looping walk / slow / freeze / tremor session."""
        import gait_sim
        rng = np.random.default_rng(seed)
        parts = [gait_sim.build(sc, 40, rng)["xyz"] for sc in ("normal", "fog", "mixed", "shuffle")]
        self.xyz = np.vstack(parts)
        self.t0 = time.monotonic()

    def read(self) -> tuple[float, float, float]:
        """Return the sample for the current wall-clock time."""
        i = int((time.monotonic() - self.t0) * RATE_HZ) % len(self.xyz)
        x, y, z = self.xyz[i]
        return float(x), float(y), float(z)


class SimPulse:
    """Plausible heart rate for the laptop demo: higher while walking."""

    def __init__(self) -> None:
        """Start at a resting 72 bpm."""
        self.bpm, self.walking = 72.0, False

    def status(self) -> dict:
        """Drift toward 92 bpm when walking and 72 at rest."""
        target = 92.0 if self.walking else 72.0
        self.bpm += (target - self.bpm) * 0.05 + np.random.normal(0, 0.6)
        return {"hr_ok": True, "finger": True, "bpm": round(self.bpm), "hr_quality": 0.6}

    def close(self) -> None:
        """Nothing to release."""


class Engine:
    """Samples, analyses and cues; exposes a compact status and a command handler."""

    def __init__(self, sensor, pulse=None, buzzer: Buzzer | None = None) -> None:
        """Start the sampling and analysis threads."""
        self.sensor, self.pulse, self.buzzer = sensor, pulse, buzzer
        self.buf: deque[tuple[float, float, float]] = deque(maxlen=int(ANALYSE_S * RATE_HZ))
        self.fresh: deque[tuple[int, int, int]] = deque(maxlen=400)   # for streaming, mg
        self.model = gait_model.load()
        self.cue_mode = "freeze"            # freeze | off
        self.cue_bpm: float | None = None   # None = learn from walking
        self.baseline_cad: float | None = None
        self.steps = 0
        self.fog_events = 0
        self.last_stride_t = -1e9
        self.state = "rest"
        self.metrics: dict = {}
        self.fi = 0.0
        self.cueing = False
        self.manual_metro = 0.0
        self._fog_since: float | None = None
        self._calm_since: float | None = None
        self.t_start = time.monotonic()
        self.n = 0
        self._stop = threading.Event()
        threading.Thread(target=self._sample, daemon=True).start()
        threading.Thread(target=self._analyse, daemon=True).start()

    def _sample(self) -> None:
        t0 = time.monotonic()
        while not self._stop.is_set():
            try:
                x, y, z = self.sensor.read()
            except OSError:
                x = y = z = 0.0
            self.buf.append((x, y, z))
            self.fresh.append((int(x * 1000), int(y * 1000), int(z * 1000)))
            self.n += 1
            time.sleep(max(0.0, t0 + self.n / RATE_HZ - time.monotonic()))

    def _analyse(self) -> None:
        while not self._stop.is_set():
            time.sleep(1.0)
            if len(self.buf) < RATE_HZ * 5:
                continue
            xyz = np.array(self.buf)
            now = time.monotonic() - self.t_start
            res = gait_algo.analyse(xyz, RATE_HZ, method="cwt")
            t_end = now
            t_first = t_end - len(xyz) / RATE_HZ
            # count strides once they are at least 0.8 s old (the filter looks ahead)
            bout = len(res.strides) >= 3 and float(np.median(np.diff(res.strides))) < 2.0 * RATE_HZ
            for idx in (res.strides if bout else []):   # ignore isolated bumps
                ts = t_first + idx / RATE_HZ
                if ts > self.last_stride_t + 0.35 and ts < t_end - 0.8:
                    self.steps += 2
                    self.last_stride_t = ts
            m = gait_algo.gait_metrics(res)
            last = res.windows[-1] if res.windows else None
            state = last.state if last else "rest"
            if self.model is not None and last is not None:
                state = gait_model.predict_window(self.model, xyz[-int(gait_algo.WIN_S * RATE_HZ):])
            if state == "fog" and self.state != "fog":
                self.fog_events += 1
            self.state, self.fi, self.metrics = state, (last.fi if last else 0.0), m
            if state == "walk" and "cadence_spm" in m and self.baseline_cad is None and self.steps >= 16:
                self.baseline_cad = m["cadence_spm"]
            if self.pulse is not None and hasattr(self.pulse, "walking"):
                self.pulse.walking = state == "walk"
            self._cue_logic(now)

    def _cue_logic(self, now: float) -> None:
        if self.buzzer is None or self.cue_mode == "off" or self.manual_metro:
            return
        if self.state == "fog":
            self._fog_since = self._fog_since or now
            self._calm_since = None
            if not self.cueing and now - self._fog_since >= 1.0:
                self.cueing = True
                self.buzzer.metronome(self.cue_bpm or self.baseline_cad or 100.0)
        else:
            self._fog_since = None
            if self.cueing and self.state in ("walk", "rest"):
                self._calm_since = self._calm_since or now
                if now - self._calm_since >= 3.0:
                    self.cueing = False
                    self.buzzer.metronome(0)

    def status(self) -> dict:
        """Compact status (short keys keep a Bluetooth notification under ~180 bytes)."""
        hr = self.pulse.status() if self.pulse is not None else {}
        m = self.metrics
        return {"s": self.state, "n": self.steps, "c": round(m.get("cadence_spm", 0)),
                "v": round(m.get("stride_cv_pct", 0), 1), "f": round(self.fi, 2),
                "k": self.fog_events, "q": int(self.cueing or bool(self.manual_metro)),
                "b": hr.get("bpm"), "h": int(bool(hr.get("finger"))),
                "m": self.cue_mode, "u": round(time.monotonic() - self.t_start)}

    def drain_samples(self) -> list[tuple[int, int, int]]:
        """Return and clear the samples gathered since the last call (milli-g)."""
        out = []
        while self.fresh:
            out.append(self.fresh.popleft())
        return out

    def handle(self, cmd: dict) -> str:
        """Apply a control command from the dashboard; return a short reply."""
        c = cmd.get("cmd")
        if c == "beep" and self.buzzer:
            self.buzzer.beep_async(float(cmd.get("s", 0.4)))
        elif c == "metronome" and self.buzzer:
            self.manual_metro = float(cmd.get("bpm", 0)) if cmd.get("on", True) else 0.0
            self.buzzer.metronome(self.manual_metro)
        elif c == "cue":
            self.cue_mode = "off" if cmd.get("mode") == "off" else "freeze"
            if self.cue_mode == "off" and self.buzzer and not self.manual_metro:
                self.cueing = False
                self.buzzer.metronome(0)
        elif c == "cue_bpm":
            self.cue_bpm = float(cmd["bpm"]) or None
        elif c == "fi":
            gait_algo.FI_THRESH = max(0.5, min(10.0, float(cmd.get("v", 2.0))))
        elif c == "reset":
            self.steps = self.fog_events = 0
        else:
            return f"unknown or unavailable: {c}"
        return "ok"

    def close(self) -> None:
        """Stop threads and release hardware."""
        self._stop.set()
        if self.buzzer:
            self.buzzer.close()
        if self.pulse:
            self.pulse.close()


def status_json(engine: Engine) -> bytes:
    """Status as compact UTF-8 JSON."""
    return json.dumps(engine.status(), separators=(",", ":")).encode()


def pack_samples(seq: int, samples: list[tuple[int, int, int]]) -> bytes:
    """Pack up to 10 samples as <uint16 seq, n x (int16 x, y, z)> in milli-g."""
    import struct
    flat = [max(-32768, min(32767, v)) for s in samples[:10] for v in s]
    return struct.pack(f"<H{len(flat)}h", seq & 0xFFFF, *flat)


def unpack_samples(data: bytes) -> tuple[int, list[tuple[float, float, float]]]:
    """Inverse of :func:`pack_samples`, returning g."""
    import struct
    n = (len(data) - 2) // 2
    vals = struct.unpack(f"<H{n}h", data[:2 + 2 * n])
    xs = vals[1:]
    return vals[0], [(xs[i] / 1000, xs[i + 1] / 1000, xs[i + 2] / 1000)
                     for i in range(0, len(xs) - 2, 3)]


def svm_of(samples: list[tuple[float, float, float]]) -> list[float]:
    """Signal vector magnitude of each sample."""
    return [math.sqrt(x * x + y * y + z * z) for x, y, z in samples]


DEVICE_NAME = "Tremor Tracker"
SERVICE_UUID = "7e570001-6a1e-4c3b-9d2f-7472656d6f72"
STATUS_UUID = "7e570002-6a1e-4c3b-9d2f-7472656d6f72"
ACCEL_UUID = "7e570003-6a1e-4c3b-9d2f-7472656d6f72"
CONTROL_UUID = "7e570004-6a1e-4c3b-9d2f-7472656d6f72"
LIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "live")
