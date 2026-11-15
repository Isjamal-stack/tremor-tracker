"""Gait monitoring prototype: leg-worn Grove accelerometer + buzzer cueing.

Hardware (Pi Zero 2 W): Grove MMA7660 accelerometer on I2C (0x4C), Grove buzzer on D16.

Modes
  tilt       Beep while the sensor is tilted past --angle degrees from its resting pose.
  metronome  Steady beep at --bpm (rhythmic auditory cueing, one beep per step).
  gait       Live stride detection, cadence, stride-time variability and a freeze index.
             When a possible freeze is detected the buzzer plays a cue beat at the
             walker's own baseline cadence until walking resumes. Records a CSV.

This is a monitoring prototype, not a diagnostic or medical device.

  python3 gait_cue.py tilt --angle 35
  python3 gait_cue.py metronome --bpm 100
  python3 gait_cue.py gait --record walk1.csv
  python3 gait_cue.py gait --fake            # simulated walk + freeze, no hardware
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
import sys
import threading
import time
from collections import deque

import numpy as np

RATE_HZ = 100.0          # polling rate; the MMA7660 updates at 120 samples/s
MIN_STRIDE_S = 0.5       # refractory time between strides of the instrumented leg
STRIDE_THRESH_G = 0.25   # high-passed magnitude must exceed this to count as a stride
FI_WINDOW_S = 4.0        # freeze index window (Moore et al. 2008 style)
FI_THRESH = 2.0          # freeze band / locomotor band power ratio
FI_MIN_STD_G = 0.05      # ignore the ratio when the leg is basically still


# ----------------------------------------------------------------------------
# Hardware
# ----------------------------------------------------------------------------

class MMA7660:
    """Grove 3-axis accelerometer, 6-bit, +/-1.5 g."""

    def __init__(self, bus_no: int = 1, addr: int = 0x4C) -> None:
        """Open the I2C bus and put the sensor in active mode at 120 samples/s."""
        from smbus2 import SMBus
        self.bus, self.addr = SMBus(bus_no), addr
        self.bus.write_byte_data(addr, 0x07, 0x00)   # standby while configuring
        self.bus.write_byte_data(addr, 0x08, 0x00)   # 120 samples/s
        self.bus.write_byte_data(addr, 0x07, 0x01)   # active

    def read(self) -> tuple[float, float, float]:
        """Return (x, y, z) in g, re-reading any axis caught mid-update."""
        out = []
        for reg in (0x00, 0x01, 0x02):
            for _ in range(5):
                v = self.bus.read_byte_data(self.addr, reg)
                if not v & 0x40:
                    break
            v &= 0x3F
            out.append((v - 64 if v & 0x20 else v) / 21.33)
        return out[0], out[1], out[2]


class FakeWalker:
    """Simulated shank accelerometer: walk, freeze (trembling in place), walk, stand."""

    def __init__(self) -> None:
        """Start the simulated clock at zero."""
        self.t0 = time.monotonic()

    def read(self) -> tuple[float, float, float]:
        """Return a synthetic (x, y, z) sample in g for the current time."""
        t = time.monotonic() - self.t0
        x, y, z = 0.0, 0.0, 1.0
        if t < 15 or 22 <= t < 35:                       # walking, ~1.1 s strides
            ph = (t % 1.1) / 1.1
            x += 0.5 * math.sin(2 * math.pi * ph)
            z += 1.2 * math.exp(-((ph - 0.1) / 0.03) ** 2) - 0.2 * math.sin(2 * math.pi * ph)
        elif 15 <= t < 22:                                # freeze: 6 Hz trembling
            z += 0.25 * math.sin(2 * math.pi * 6.0 * t)
            x += 0.15 * math.sin(2 * math.pi * 5.5 * t + 1)
        noise = lambda: random.gauss(0, 0.03)
        clip = lambda v: max(-1.5, min(1.45, v))           # mimic the sensor's range
        return clip(x + noise()), clip(y + noise()), clip(z + noise())


class Beeper:
    """Grove buzzer wrapper. Prints a bell character when no GPIO is available."""

    def __init__(self, pin: int, enabled: bool = True) -> None:
        """Open the buzzer on ``pin``; fall back to a terminal bell on failure."""
        self.buzzer = None
        if enabled:
            try:
                from gpiozero import Buzzer
                self.buzzer = Buzzer(pin)
            except Exception as exc:   # no GPIO (e.g. running on a laptop)
                print(f"(buzzer unavailable: {exc}; using terminal bell)")

    def beep(self, on_s: float = 0.06) -> None:
        """Sound one short beep without blocking the caller."""
        if self.buzzer is not None:
            self.buzzer.beep(on_time=on_s, off_time=0.01, n=1, background=True)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()

    def close(self) -> None:
        """Silence and release the buzzer."""
        if self.buzzer is not None:
            self.buzzer.off()
            self.buzzer.close()


class Metronome:
    """Background beat generator for rhythmic auditory cueing."""

    def __init__(self, beeper: Beeper, bpm: float) -> None:
        """Prepare a metronome at ``bpm`` beats per minute (not started)."""
        self.beeper, self.bpm = beeper, bpm
        self.running = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        next_t = time.monotonic()
        while True:
            if not self.running.is_set():
                self.running.wait()
                next_t = time.monotonic()
            self.beeper.beep(0.05)
            next_t += 60.0 / max(30.0, min(180.0, self.bpm))
            time.sleep(max(0.0, next_t - time.monotonic()))

    def start(self, bpm: float | None = None) -> None:
        """Start beating, optionally at a new tempo."""
        if bpm:
            self.bpm = bpm
        self.running.set()

    def stop(self) -> None:
        """Stop beating after the current beat."""
        self.running.clear()


def ticks(rate: float = RATE_HZ):
    """Yield the elapsed seconds at a steady ``rate`` until interrupted."""
    t0 = time.monotonic()
    n = 0
    while True:
        yield time.monotonic() - t0
        n += 1
        time.sleep(max(0.0, t0 + n / rate - time.monotonic()))


# ----------------------------------------------------------------------------
# Signal processing
# ----------------------------------------------------------------------------

def tilt_deg(g: tuple[float, float, float], ref: tuple[float, float, float]) -> float:
    """Angle in degrees between gravity vector ``g`` and the resting vector ``ref``."""
    dot = sum(a * b for a, b in zip(g, ref))
    na = math.sqrt(sum(a * a for a in g)) or 1e-9
    nb = math.sqrt(sum(b * b for b in ref)) or 1e-9
    return math.degrees(math.acos(max(-1.0, min(1.0, dot / (na * nb)))))


def freeze_index(mag: np.ndarray, rate: float) -> tuple[float, float]:
    """Return (freeze index, std of magnitude in g) for an acceleration-magnitude window.

    Freeze index = power in the 3-8 Hz "freeze" band / power in the 0.5-3 Hz
    "locomotor" band. Trembling in place pushes it up; normal walking keeps it low.
    """
    x = (mag - mag.mean()) * np.hanning(len(mag))
    p = np.abs(np.fft.rfft(x)) ** 2 / len(x)
    f = np.fft.rfftfreq(len(x), 1.0 / rate)
    loco = p[(f >= 0.5) & (f < 3.0)].sum()
    frz = p[(f >= 3.0) & (f < 8.0)].sum()
    return float(frz / (loco + 1e-9)), float(mag.std())


class StrideDetector:
    """Peak detector on the high-passed acceleration magnitude (one peak per stride).

    The threshold adapts to half the recent heel-strike height, and the minimum gap
    adapts to 60% of the recent stride time, so smaller mid-swing bumps are ignored.
    """

    def __init__(self, rate: float) -> None:
        """Set up a 1 s running mean, a 3-sample smoother and adaptive limits."""
        self.mean = deque(maxlen=int(rate))
        self.smooth = deque(maxlen=3)
        self.prev = [0.0, 0.0]
        self.last_t = -1e9
        self.heights: deque[float] = deque(maxlen=8)
        self.gaps: deque[float] = deque(maxlen=8)

    def update(self, t: float, mag: float) -> bool:
        """Feed one sample; return True when the previous sample was a stride peak."""
        self.mean.append(mag)
        self.smooth.append(mag - sum(self.mean) / len(self.mean))
        v = sum(self.smooth) / len(self.smooth)
        a, b = self.prev
        self.prev = [b, v]
        thresh = STRIDE_THRESH_G
        if len(self.heights) >= 3:
            thresh = max(thresh, 0.5 * statistics.median(self.heights))
        gap = MIN_STRIDE_S
        if len(self.gaps) >= 3:
            gap = max(gap, 0.6 * statistics.median(self.gaps))
        if b > thresh and b >= a and b > v and t - self.last_t >= gap:
            if t - self.last_t < 2.5:
                self.gaps.append(t - self.last_t)
            self.heights.append(b)
            self.last_t = t
            return True
        return False


# ----------------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------------

def bar(value: float, full: float, width: int = 30) -> str:
    """Return a text bar filled in proportion to ``value / full``."""
    n = int(max(0.0, min(1.0, value / full)) * width)
    return "#" * n + "." * (width - n)


def calibrate(sensor, seconds: float = 1.0) -> tuple[float, float, float]:
    """Average the sensor for ``seconds`` while it is held still; return gravity vector."""
    print(f"Hold still for {seconds:.0f} s to set the resting pose...")
    samples = []
    for t in ticks():
        samples.append(sensor.read())
        if t >= seconds:
            break
    return tuple(statistics.fmean(s[i] for s in samples) for i in range(3))


def run_tilt(sensor, beeper: Beeper, angle: float) -> None:
    """Beep repeatedly while the sensor is tilted more than ``angle`` from rest."""
    ref = calibrate(sensor)
    lp = list(ref)
    tilted, last_beep = False, 0.0
    print(f"Tilt past {angle:.0f} deg to beep. Ctrl+C to stop.")
    for t in ticks():
        g = sensor.read()
        lp = [0.8 * a + 0.2 * b for a, b in zip(lp, g)]   # low-pass to ignore jolts
        deg = tilt_deg(tuple(lp), ref)
        if not tilted and deg > angle:
            tilted = True
        elif tilted and deg < angle - 5:                    # 5 deg hysteresis
            tilted = False
        if tilted and t - last_beep >= 0.3:
            beeper.beep(0.1)
            last_beep = t
        state = "TILTED  BEEP" if tilted else "level       "
        print(f"\r tilt {deg:5.1f} deg [{bar(deg, 90)}] {state}", end="", flush=True)


def run_metronome(beeper: Beeper, bpm: float) -> None:
    """Play a steady cue beat at ``bpm`` until interrupted."""
    print(f"Cue beat at {bpm:.0f} beats/min. Step on each beep. Ctrl+C to stop.")
    Metronome(beeper, bpm).start()
    while True:
        time.sleep(1)


def run_gait(sensor, beeper: Beeper, args: argparse.Namespace) -> None:
    """Live stride detection, cadence, variability, freeze index and freeze cueing."""
    rate = RATE_HZ
    det = StrideDetector(rate)
    win = deque(maxlen=int(FI_WINDOW_S * rate))
    strides: deque[float] = deque(maxlen=20)
    stride_times: list[float] = []
    metro = Metronome(beeper, args.bpm or 100.0)
    baseline_cad: float | None = args.bpm
    cueing, freeze_since, calm_since = False, None, None
    fi, spread, n_strides, n_freezes, frozen = 0.0, 0.0, 0, 0, False

    out = open(args.record, "w", newline="") if args.record else None
    w = csv.writer(out) if out else None
    if w:
        w.writerow(["t_s", "x_g", "y_g", "z_g", "mag_g", "stride", "cadence_spm",
                    "freeze_index", "cue_on"])
    if args.cue == "always":
        metro.start()
        cueing = True
    print("Walking monitor. Strap the sensor to the shin. Ctrl+C to stop and summarise.")
    try:
        for i, t in enumerate(ticks(rate)):
            x, y, z = sensor.read()
            mag = math.sqrt(x * x + y * y + z * z)
            win.append(mag)
            is_stride = det.update(t, mag) and not frozen   # ignore trembling 'steps'
            if is_stride:
                if stride_times and t - stride_times[-1] < 2.5:
                    strides.append(t - stride_times[-1])
                stride_times.append(t)
                n_strides += 1
                if args.cue == "step":
                    beeper.beep(0.04)
            cad = 120.0 / statistics.median(strides) if len(strides) >= 3 else 0.0
            if baseline_cad is None and len(strides) >= 10:
                baseline_cad = cad                       # learned from the first strides
                print(f"\n Baseline cadence learned: {cad:.0f} steps/min")

            if i % 25 == 0 and len(win) == win.maxlen:   # update FI 4x per second
                fi, spread = freeze_index(np.array(win), rate)
                frozen = fi > FI_THRESH and spread > FI_MIN_STD_G
                if args.cue == "freeze":
                    if frozen and not cueing:
                        freeze_since = freeze_since or t
                        if t - freeze_since >= 1.0:      # 1 s of freeze pattern
                            metro.start(baseline_cad or 100.0)
                            cueing, calm_since = True, None
                            n_freezes += 1
                    elif not frozen:
                        freeze_since = None
                    if cueing and not frozen and t - det.last_t < 1.5:
                        calm_since = calm_since or t
                        if t - calm_since >= 3.0:        # walking again for 3 s
                            metro.stop()
                            cueing = False
                    elif cueing:
                        calm_since = None

            if w:
                w.writerow([f"{t:.3f}", f"{x:.3f}", f"{y:.3f}", f"{z:.3f}", f"{mag:.3f}",
                            int(is_stride), f"{cad:.1f}", f"{fi:.2f}", int(cueing)])
            if i % 10 == 0:
                cv = (statistics.stdev(strides) / statistics.fmean(strides) * 100
                      if len(strides) >= 4 else 0.0)
                flag = "POSSIBLE FREEZE - CUEING" if cueing else (
                    "walking" if t - det.last_t < 2.5 else "still")
                print(f"\r t {t:6.1f}s  strides {n_strides:3d}  cadence {cad:5.0f}/min  "
                      f"var {cv:4.1f}%  FI {fi:4.1f} [{bar(fi, 6, 12)}]  {flag:<24}",
                      end="", flush=True)
            if args.seconds and t >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        metro.stop()
        if out:
            out.close()
    summarise(strides_from(stride_times), n_freezes, args.record)


def strides_from(times: list[float]) -> list[float]:
    """Return stride durations from stride timestamps, dropping pauses over 2.5 s."""
    return [b - a for a, b in zip(times, times[1:]) if b - a < 2.5]


def summarise(strides: list[float], n_freezes: int, path: str | None) -> None:
    """Print a short session summary."""
    print("\n\n--- Session summary (monitoring prototype) ---")
    if len(strides) < 4:
        print(f"Only {len(strides)} strides detected; walk longer for a summary.")
    else:
        m = statistics.fmean(strides)
        cv = statistics.stdev(strides) / m * 100
        print(f"Strides            {len(strides) + 1}")
        print(f"Mean stride time   {m:.2f} s")
        print(f"Cadence            {120 / m:.0f} steps/min")
        print(f"Stride variability {cv:.1f}% (healthy adults are usually under ~3%)")
    print(f"Possible freezes   {n_freezes}")
    if path:
        print(f"Recording saved to {path}")


def main() -> None:
    """Parse arguments and run the chosen mode."""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=("tilt", "metronome", "gait"))
    p.add_argument("--angle", type=float, default=35.0, help="tilt threshold, degrees")
    p.add_argument("--bpm", type=float, default=None,
                   help="cue tempo in steps/min (gait default: learned from walking)")
    p.add_argument("--cue", choices=("freeze", "always", "step", "off"), default="freeze",
                   help="gait: cue on freeze (default), always, click per stride, or off")
    p.add_argument("--record", default=None, help="gait: CSV file to save")
    p.add_argument("--seconds", type=float, default=0, help="gait: stop after N seconds")
    p.add_argument("--buzzer-pin", type=int, default=16)
    p.add_argument("--quiet", action="store_true", help="no buzzer")
    p.add_argument("--fake", action="store_true", help="simulated walker, no hardware")
    args = p.parse_args()

    beeper = Beeper(args.buzzer_pin, enabled=not args.quiet and not args.fake)
    sensor = FakeWalker() if args.fake else MMA7660()
    try:
        if args.mode == "tilt":
            run_tilt(sensor, beeper, args.angle)
        elif args.mode == "metronome":
            run_metronome(beeper, args.bpm or 100.0)
        else:
            run_gait(sensor, beeper, args)
    except KeyboardInterrupt:
        print()
    finally:
        beeper.close()


if __name__ == "__main__":
    main()
