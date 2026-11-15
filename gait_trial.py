"""Record labelled walking trials on the Pi, with buzzer cues (no screen needed).

Strap the accelerometer to the outside of the ankle, cable up the leg, and run:
  python3 gait_trial.py --protocol            # guided set of trials, one after another
  python3 gait_trial.py --label normal --steps 40 --seconds 35

Cues (3300/3800 Hz warble):  3 short = get ready (3 s)   1 long = GO   3 quick = STOP
Each trial is saved to ~/trials/<time>_<label>_<steps>steps.csv with the same
columns as gait_sim.py (t_s, x_g, y_g, z_g, mag_g, label) plus expected_steps and
bpm, so gait_algo.py can analyse it and the counts can be checked.

Simulated Parkinsonian patterns (shuffle, freeze, festination) are acted by a
healthy person; label them as such. Monitoring prototype, not clinical data.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import time
from datetime import datetime

import numpy as np

import gait_algo
from gait_cue import MMA7660, ticks

TRIAL_DIR = os.path.expanduser("~/trials")
PROTOCOL = [
    # label, expected steps (0 = none), seconds, instruction
    ("rest", 0, 10, "Stand still, feet flat, arms relaxed."),
    ("normal", 40, 35, "Walk 40 steps at your normal pace, then stop and stand still."),
    ("normal_fast", 40, 30, "Walk 40 steps briskly, then stop and stand still."),
    ("shuffle", 40, 40, "Act a Parkinsonian shuffle: 40 short, low steps, feet barely lifting."),
    ("fog", 30, 40, "Walk ~15 steps, act a freeze (feet stuck, knees trembling 4-5 s), "
                    "then walk ~15 more steps."),
    ("festination", 30, 35, "Start slow, then take faster and smaller steps "
                            "as if falling forward; stop after ~30 steps."),
    ("turn", 30, 40, "Walk ~10 steps, turn around 180 degrees, walk back ~10, turn, ~10 more."),
    ("tremor", 0, 15, "Stand still and make a small fast shake of the lower leg (heel down)."),
]
QUICK = [
    ("rest", 0, 5, "Stand still."),
    ("normal", 20, 15, "Walk 20 steps at your normal pace, then stand still."),
    ("shuffle", 20, 18, "20 short, low, shuffling steps."),
    ("fog", 16, 20, "8 steps, freeze with knees trembling ~4 s, then 8 more steps."),
    ("tremor", 0, 8, "Stand still and shake your lower leg quickly, heel down."),
]

# One continuous recording; one long warble = switch to the next phase.
PHASED = [
    ("rest", 4, "stand still"),
    ("walk", 12, "WALK at your normal pace"),
    ("shuffle", 10, "SLOW DOWN: short, shuffling steps"),
    ("fog", 6, "FREEZE: feet stuck, knees trembling / shaking"),
    ("walk", 10, "WALK again, normal pace"),
    ("rest", 4, "STOP and stand still"),
]


class Cues:
    """Buzzer cues as a 3300/3800 Hz warble, the loudest sound on this buzzer."""

    def __init__(self, pin: int = 16) -> None:
        """Open the buzzer as PWM; fall back to silent if GPIO is unavailable."""
        try:
            from gpiozero import PWMOutputDevice
            self.dev = PWMOutputDevice(pin, frequency=3800)
        except Exception as exc:
            print(f"(buzzer unavailable: {exc})")
            self.dev = None

    def warble(self, seconds: float, gap_s: float = 0.0) -> None:
        """Alternate 3800 and 3300 Hz every 0.12 s for ``seconds``, then pause."""
        if self.dev:
            for i in range(max(1, int(seconds / 0.12))):
                self.dev.frequency = 3800 if i % 2 == 0 else 3300
                self.dev.value = 0.5
                time.sleep(0.12)
            self.dev.value = 0
        else:
            time.sleep(seconds)
        time.sleep(gap_s)

    def ready(self) -> None:
        """Three short warbles over about 3 seconds."""
        for _ in range(3):
            self.warble(0.36, 0.64)

    def go(self) -> None:
        """One long warble."""
        self.warble(1.2)

    def stop(self) -> None:
        """Three short warbles, quickly."""
        for _ in range(3):
            self.warble(0.36, 0.2)

    def close(self) -> None:
        """Release the buzzer."""
        if self.dev:
            self.dev.value = 0
            self.dev.close()


def record(sensor, cues: Cues, label: str, steps: int, seconds: float,
           pulse=None, rate: float = 100.0) -> str:
    """Record one trial and return the CSV path."""
    os.makedirs(TRIAL_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(TRIAL_DIR, f"{stamp}_{label}_{steps}steps.csv")
    print(f"CUE READY {label}", flush=True)
    cues.ready()
    print("CUE GO", flush=True)
    cues.go()
    rows, xyz = [], []
    for t in ticks(rate):
        x, y, z = sensor.read()
        bpm = pulse.status()["bpm"] if pulse and len(rows) % 100 == 0 else ""
        rows.append((t, x, y, z, bpm))
        xyz.append((x, y, z))
        if len(rows) % 100 == 0:
            print(f"\r  {t:5.1f}/{seconds:.0f} s", end="", flush=True)
        if t >= seconds:
            break
    print("\nCUE STOP", flush=True)
    cues.stop()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "x_g", "y_g", "z_g", "mag_g", "label", "expected_steps", "bpm"])
        for t, x, y, z, bpm in rows:
            w.writerow([f"{t:.3f}", f"{x:.3f}", f"{y:.3f}", f"{z:.3f}",
                        f"{math.sqrt(x * x + y * y + z * z):.3f}", label, steps, bpm])
    res = gait_algo.analyse(np.array(xyz), rate, method="cwt")
    m = gait_algo.gait_metrics(res)
    found = f"{int(m['steps'])} steps found"
    if steps:
        found += f" (expected {steps})"
    extra = f", cadence {m['cadence_spm']:.0f}/min" if "cadence_spm" in m else ""
    print(f"\r  saved {os.path.basename(path)}: {found}{extra}, "
          f"freeze events {int(m['fog_events'])}")
    return path


def record_phased(sensor, cues: Cues, plan=PHASED, pulse=None, rate: float = 100.0,
                  round_no: int = 1) -> str:
    """Record one continuous trial; each sample is labelled with its current phase.

    The buzzer cue runs in a thread so sampling never pauses during a warble.
    """
    import threading
    os.makedirs(TRIAL_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(TRIAL_DIR, f"{stamp}_phased_r{round_no}.csv")
    bounds, t_acc = [], 0.0
    for label, secs, _ in plan:
        bounds.append((t_acc, t_acc + secs, label))
        t_acc += secs
    print("  order: " + "  ->  ".join(how for _, _, how in plan))
    cues.ready()
    cues.go()
    print(f"  NOW: {plan[0][2]}", flush=True)
    rows, xyz, phase = [], [], 0
    for t in ticks(rate):
        while phase < len(bounds) - 1 and t >= bounds[phase][1]:
            phase += 1
            print(f"  NOW: {plan[phase][2]}", flush=True)
            threading.Thread(target=cues.go, daemon=True).start()
        x, y, z = sensor.read()
        bpm = pulse.status()["bpm"] if pulse and len(rows) % 100 == 0 else ""
        rows.append((t, x, y, z, bounds[phase][2], bpm))
        xyz.append((x, y, z))
        if t >= t_acc:
            break
    time.sleep(1.3)
    cues.stop()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "x_g", "y_g", "z_g", "mag_g", "label", "expected_steps", "bpm"])
        for t, x, y, z, label, bpm in rows:
            w.writerow([f"{t:.3f}", f"{x:.3f}", f"{y:.3f}", f"{z:.3f}",
                        f"{math.sqrt(x * x + y * y + z * z):.3f}", label, "", bpm])
    res = gait_algo.analyse(np.array(xyz), rate, method="cwt")
    m = gait_algo.gait_metrics(res)
    print(f"  saved {os.path.basename(path)}: {int(m['steps'])} steps found, "
          f"freeze events {int(m['fog_events'])}")
    return path


def main() -> None:
    """Run one labelled trial or the whole guided protocol."""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--protocol", action="store_true", help="run the guided set of trials")
    p.add_argument("--quick", action="store_true", help="short 5-trial protocol (~2 min)")
    p.add_argument("--phased", type=int, default=0, metavar="ROUNDS",
                   help="continuous walk/slow/freeze/walk recording, repeated ROUNDS times")
    p.add_argument("--label", default="normal")
    p.add_argument("--steps", type=int, default=40, help="steps you will take (both feet)")
    p.add_argument("--seconds", type=float, default=35.0)
    p.add_argument("--pause", type=float, default=8.0, help="seconds between protocol trials")
    p.add_argument("--no-hr", action="store_true", help="skip the heart-rate sensor")
    args = p.parse_args()

    sensor, cues, pulse = MMA7660(), Cues(), None
    if not args.no_hr:
        try:
            from heart_rate import PulseMonitor
            pulse = PulseMonitor()
            if not pulse.sensor:
                pulse = None
        except Exception:
            pulse = None
    if args.phased:
        try:
            for r in range(1, args.phased + 1):
                print(f"\nRound {r}/{args.phased} (46 s). One long warble = switch.")
                if r > 1:
                    print(f"  next round in {args.pause:.0f} s...")
                    time.sleep(args.pause)
                record_phased(sensor, cues, pulse=pulse, round_no=r)
            print(f"\nDone. Files are in {TRIAL_DIR}/")
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            cues.close()
            if pulse:
                pulse.close()
        return
    plan = QUICK if args.quick else PROTOCOL if args.protocol else [(args.label, args.steps, args.seconds, "")]
    try:
        for i, (label, steps, secs, how) in enumerate(plan, 1):
            print(f"\nTrial {i}/{len(plan)}: {label}  ({secs:.0f} s)  {how}")
            if (args.protocol or args.quick) and i > 1:
                print(f"  next trial starts in {args.pause:.0f} s...")
                time.sleep(args.pause)
            record(sensor, cues, label, steps, secs, pulse)
        print(f"\nDone. Files are in {TRIAL_DIR}/")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        cues.close()
        if pulse:
            pulse.close()


if __name__ == "__main__":
    main()
