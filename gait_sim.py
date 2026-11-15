"""Synthetic ankle accelerometer data with ground truth, for the gait prototype.

Models an ankle-worn 3-axis sensor (x forward, y lateral, z along the shin) and
degrades it to match the Grove MMA7660: 100 Hz, 6-bit (~0.047 g steps), clipped
to about +/-1.5 g. Every row carries the true activity label and a 1 on the
sample where the instrumented foot lands, so detectors can be scored.

Scenarios
  normal       healthy walk, regular strides
  shuffle      Parkinsonian walk: short, low-impact, more variable strides
  festination  strides that speed up and shrink, then a freeze
  fog          walk, freezing of gait (3-8 Hz trembling in place), walk again
  tremor       standing still with a 4-6 Hz resting leg tremor
  mixed        rest, walk, festination, freeze, walk, tremor, rest

Simulated data for a monitoring prototype, not clinical data.

  python3 gait_sim.py --scenario normal --steps 40 --out data/sim/normal40.csv
  python3 gait_sim.py --dataset 60 --outdir data/sim
"""
from __future__ import annotations

import argparse
import csv
import math
import os
from dataclasses import dataclass

import numpy as np

RATE_HZ = 100.0
LSB_G = 1.0 / 21.33
CLIP_LO, CLIP_HI = -1.5, 1.45
LABELS = ("rest", "walk", "fog", "tremor")
SCENARIOS = ("normal", "shuffle", "festination", "fog", "tremor", "mixed")


@dataclass
class Gait:
    """Stride parameters for one walking segment."""

    period_s: float = 1.10              # stride time (same foot to same foot)
    cv: float = 0.02                    # stride-time coefficient of variation
    impact_g: float = 1.3               # heel-strike spike height
    swing_deg: float = 28.0             # shank swing amplitude
    end_period_s: float | None = None   # if set, period ramps to this (festination)
    end_scale: float = 1.0              # amplitude scale at the end of the segment


NORMAL = Gait()
SHUFFLE = Gait(period_s=1.15, cv=0.05, impact_g=0.45, swing_deg=11.0)
FESTINATE = Gait(period_s=1.0, end_period_s=0.6, cv=0.04, impact_g=0.5,
                 swing_deg=12.0, end_scale=0.6)


class Session:
    """Accumulates clean signal segments plus labels and true stride marks."""

    def __init__(self, rng: np.random.Generator) -> None:
        """Start an empty session using ``rng`` for all randomness."""
        self.rng = rng
        self.xyz: list[np.ndarray] = []
        self.labels: list[np.ndarray] = []
        self.strides: list[np.ndarray] = []

    def _add(self, xyz: np.ndarray, label: str, strides: np.ndarray | None = None) -> None:
        n = len(xyz)
        self.xyz.append(xyz)
        self.labels.append(np.full(n, label, dtype=object))
        self.strides.append(strides if strides is not None else np.zeros(n, dtype=int))

    def rest(self, seconds: float) -> None:
        """Standing still: gravity along the shin plus slight postural sway."""
        n = int(seconds * RATE_HZ)
        t = np.arange(n) / RATE_HZ
        tilt = np.radians(2.0) * np.sin(2 * np.pi * 0.25 * t + self.rng.uniform(0, 6))
        self._add(np.column_stack([np.sin(tilt), np.zeros(n), np.cos(tilt)]), "rest")

    def tremor(self, seconds: float) -> None:
        """Standing with a resting leg tremor (4-6 Hz, small amplitude)."""
        n = int(seconds * RATE_HZ)
        t = np.arange(n) / RATE_HZ
        f, amp = self.rng.uniform(4.0, 6.0), self.rng.uniform(0.08, 0.2)
        env = np.clip(t / 0.5, 0, 1) * np.clip((seconds - t) / 0.5, 0, 1)
        w = 2 * np.pi * f * t
        xyz = np.column_stack([0.6 * amp * env * np.sin(w), 0.3 * amp * env * np.sin(w + 1),
                               1.0 + amp * env * np.sin(w + 0.4)])
        self._add(xyz, "tremor")

    def freeze(self, seconds: float) -> None:
        """Freezing of gait: feet stuck, legs trembling at 3-8 Hz."""
        n = int(seconds * RATE_HZ)
        t = np.arange(n) / RATE_HZ
        f, amp = self.rng.uniform(3.5, 7.0), self.rng.uniform(0.15, 0.35)
        wobble = 1 + 0.3 * np.sin(2 * np.pi * 0.7 * t)
        phase = 2 * np.pi * np.cumsum(f * (1 + 0.05 * np.sin(2 * np.pi * 0.4 * t))) / RATE_HZ
        tilt = np.radians(8.0)                                  # knees bent forward
        xyz = np.column_stack([math.sin(tilt) + 0.7 * amp * wobble * np.sin(phase),
                               0.3 * amp * np.sin(phase + 2.0),
                               math.cos(tilt) + amp * wobble * np.sin(phase + 0.6)])
        self._add(xyz, "fog")

    def walk(self, strides: int, g: Gait) -> None:
        """Walk ``strides`` strides of the instrumented foot with gait ``g``."""
        rng = self.rng
        chunks, marks = [], []
        for i in range(strides):
            frac = i / max(1, strides - 1)
            p = g.period_s if g.end_period_s is None else (
                g.period_s + (g.end_period_s - g.period_s) * frac)
            p = max(0.45, p * (1 + rng.normal(0, g.cv)))
            s = 1.0 + (g.end_scale - 1.0) * frac
            n = int(round(p * RATE_HZ))
            ph = np.arange(n) / n                               # 0 = heel strike
            swing = np.clip((ph - 0.6) / 0.4, 0, 1)
            theta = np.radians(g.swing_deg * s) * np.where(
                ph < 0.6, 0.35 - 0.9 * ph, -0.2 + 1.2 * np.sin(np.pi * swing))
            imp = g.impact_g * s * rng.uniform(0.8, 1.2)
            dt = ph * p                                         # seconds since strike
            strike = imp * np.exp(-dt / 0.025) * np.cos(2 * np.pi * 18 * dt)
            toe = 0.35 * imp * np.exp(-((ph - 0.6) * p / 0.03) ** 2)
            fwd = 0.6 * s * (g.swing_deg / 28.0) * np.sin(2 * np.pi * swing) * (ph >= 0.6)
            chunks.append(np.column_stack([
                np.sin(theta) + fwd + 0.4 * strike,
                0.08 * s * np.sin(2 * np.pi * ph + 0.5) + 0.1 * strike,
                np.cos(theta) + strike + toe]))
            m = np.zeros(n, dtype=int)
            m[0] = 1
            marks.append(m)
        self._add(np.vstack(chunks), "walk", np.concatenate(marks))

    def finish(self, noise_g: float = 0.025) -> dict[str, np.ndarray]:
        """Add noise, quantise and clip like the MMA7660; return the arrays."""
        xyz = np.vstack(self.xyz)
        xyz = xyz + self.rng.normal(0, noise_g, xyz.shape)
        xyz = np.clip(np.round(xyz / LSB_G) * LSB_G, CLIP_LO, CLIP_HI)
        return {"t_s": np.arange(len(xyz)) / RATE_HZ, "xyz": xyz,
                "label": np.concatenate(self.labels),
                "true_stride": np.concatenate(self.strides)}


def _jitter(g: Gait, rng: np.random.Generator) -> Gait:
    return Gait(period_s=g.period_s * rng.uniform(0.9, 1.1), cv=g.cv,
                impact_g=g.impact_g * rng.uniform(0.8, 1.2),
                swing_deg=g.swing_deg * rng.uniform(0.85, 1.15))


def build(scenario: str, steps: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Build one session. ``steps`` counts both feet, so strides = steps // 2."""
    strides = max(2, steps // 2)
    half = strides // 2
    s = Session(rng)
    s.rest(rng.uniform(2.5, 4.0))
    if scenario == "normal":
        s.walk(strides, _jitter(NORMAL, rng))
    elif scenario == "shuffle":
        s.walk(strides, _jitter(SHUFFLE, rng))
    elif scenario == "festination":
        s.walk(half, _jitter(SHUFFLE, rng))
        s.walk(strides - half, FESTINATE)
        s.freeze(rng.uniform(3, 6))
    elif scenario == "fog":
        s.walk(half, _jitter(SHUFFLE, rng))
        s.freeze(rng.uniform(3, 8))
        s.walk(strides - half, _jitter(SHUFFLE, rng))
    elif scenario == "tremor":
        s.tremor(rng.uniform(8, 15))
    elif scenario == "mixed":
        q = max(2, strides // 3)
        s.walk(q, _jitter(NORMAL, rng))
        s.walk(q, FESTINATE)
        s.freeze(rng.uniform(3, 6))
        s.walk(strides - 2 * q, _jitter(SHUFFLE, rng))
        s.rest(2.0)
        s.tremor(rng.uniform(5, 8))
    else:
        raise ValueError(f"unknown scenario {scenario!r}")
    s.rest(rng.uniform(2.5, 4.0))
    return s.finish()


def write_csv(path: str, data: dict[str, np.ndarray], scenario: str) -> None:
    """Write a session in gait_cue.py's recording columns, plus the ground truth."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    xyz = data["xyz"]
    mag = np.sqrt((xyz ** 2).sum(axis=1))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "x_g", "y_g", "z_g", "mag_g", "label", "true_stride", "scenario"])
        for i in range(len(xyz)):
            w.writerow([f"{data['t_s'][i]:.3f}", f"{xyz[i, 0]:.3f}", f"{xyz[i, 1]:.3f}",
                        f"{xyz[i, 2]:.3f}", f"{mag[i]:.3f}", data["label"][i],
                        int(data["true_stride"][i]), scenario])


def main() -> None:
    """Parse arguments and write one session or a whole dataset."""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", choices=SCENARIOS, default="normal")
    p.add_argument("--steps", type=int, default=40, help="steps, both feet (default 40)")
    p.add_argument("--out", default="data/sim/session.csv")
    p.add_argument("--dataset", type=int, default=0, help="write N random sessions instead")
    p.add_argument("--outdir", default="data/sim")
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    rng = np.random.default_rng(args.seed)
    if args.dataset:
        for i in range(args.dataset):
            sc = SCENARIOS[i % len(SCENARIOS)]
            steps = int(rng.integers(20, 61))
            write_csv(os.path.join(args.outdir, f"{i:03d}_{sc}_{steps}steps.csv"),
                      build(sc, steps, rng), sc)
        print(f"Wrote {args.dataset} sessions to {args.outdir}/")
        return
    data = build(args.scenario, args.steps, rng)
    write_csv(args.out, data, args.scenario)
    print(f"Wrote {args.out}: {len(data['t_s']) / RATE_HZ:.1f} s, "
          f"{int(data['true_stride'].sum())} true strides ({args.steps} steps), "
          f"labels {sorted(set(data['label']))}")


if __name__ == "__main__":
    main()
