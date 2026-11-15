"""Two-stage ankle gait detector (numpy only, so it also runs on the Pi).

Stage 1, freeze and tremor gate. The zero-mean signal vector magnitude (SVM) is
cut into 2.5 s windows with 50% overlap. Power in the 0.5-3 Hz locomotor band and
the 3-8 Hz freeze band gives the Moore-Baechlin freeze index (freeze / locomotor).
  rest      total power below a noise floor
  fog       freeze index > 2 while walking happened in the last few seconds
  tremor    freeze index > 2 with no recent walking
  walk      otherwise
Stage 2, stride detection, only inside walk windows. The SVM is band-passed
(4th-order Butterworth, 0.5-4 Hz, zero phase), or passed through a Mexican-hat
wavelet at 1-2 Hz scales. Peaks must clear 0.6 x rolling std (with a floor),
be 600 ms apart, and be the dominant peak in their neighbourhood, so the smaller
toe-off/mid-swing bump is ignored. One ankle sees one stride per foot landing,
so steps = 2 x strides.

Monitoring prototype only; thresholds are tuned on simulated data.

  python3 gait_algo.py data/sim/*.csv            # score against ground truth
  python3 gait_algo.py walk.csv --method cwt     # any recording, cwt or bandpass
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass, field

import numpy as np

RATE_HZ = 100.0
WIN_S, HOP_S = 2.5, 1.25
LOCO_BAND, FREEZE_BAND = (0.5, 3.0), (3.0, 8.0)
FI_THRESH = 2.0
REST_POWER = 0.00008     # g^2, 0.5-8 Hz power below this is rest (shuffling is ~0.0002+)
FOG_MEMORY_S = 4.0       # FI spike within this long after walking counts as FoG
LOCKOUT_S = 0.6
THRESH_FLOOR_G = 0.06
MATCH_TOL_S = 0.3


# ----------------------------------------------------------------------------
# Filters
# ----------------------------------------------------------------------------

def _biquad(kind: str, f0: float, q: float, rate: float) -> tuple[np.ndarray, np.ndarray]:
    """RBJ cookbook low/high-pass biquad coefficients (b, a), a[0] == 1."""
    w0 = 2 * math.pi * f0 / rate
    alpha, c = math.sin(w0) / (2 * q), math.cos(w0)
    if kind == "low":
        b = np.array([(1 - c) / 2, 1 - c, (1 - c) / 2])
    else:
        b = np.array([(1 + c) / 2, -(1 + c), (1 + c) / 2])
    a = np.array([1 + alpha, -2 * c, 1 - alpha])
    return b / a[0], a / a[0]


def _lfilter(b: np.ndarray, a: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Direct-form II transposed biquad, started at the first sample's steady state."""
    y = np.empty_like(x)
    z1 = z2 = 0.0
    if b.sum() and abs(a.sum()) > 1e-12:             # low-pass: settle on x[0]
        gain = b.sum() / a.sum()
        z1 = x[0] * gain - b[0] * x[0]
        z2 = b[2] * x[0] - a[2] * x[0] * gain
        z1 = z1  # keep explicit for clarity
    for i, xi in enumerate(x):
        yi = b[0] * xi + z1
        z1 = b[1] * xi - a[1] * yi + z2
        z2 = b[2] * xi - a[2] * yi
        y[i] = yi
    return y


# 4th-order Butterworth = two biquads with these Q values.
_BUTTER4_Q = (0.5412, 1.3066)


def bandpass(x: np.ndarray, lo: float = 0.5, hi: float = 4.0, rate: float = RATE_HZ,
             zero_phase: bool = True) -> np.ndarray:
    """4th-order Butterworth high-pass at ``lo`` then low-pass at ``hi``."""
    stages = [_biquad("high", lo, q, rate) for q in _BUTTER4_Q]
    stages += [_biquad("low", hi, q, rate) for q in _BUTTER4_Q]

    def run(v: np.ndarray) -> np.ndarray:
        for b, a in stages:
            v = _lfilter(b, a, v)
        return v

    y = run(np.asarray(x, dtype=float))
    return run(y[::-1])[::-1] if zero_phase else y


def mexican_hat(x: np.ndarray, rate: float = RATE_HZ, freqs=(1.0, 1.5, 2.0)) -> np.ndarray:
    """Sum of Mexican-hat CWT coefficients at scales matching ``freqs`` Hz."""
    out = np.zeros_like(x, dtype=float)
    for f in freqs:
        s = rate * math.sqrt(2.5) / (2 * math.pi * f)   # mexh centre ~ sqrt(2.5)/(2*pi*s)
        t = np.arange(-int(4 * s), int(4 * s) + 1) / s
        k = (1 - t ** 2) * np.exp(-t ** 2 / 2)
        k -= k.mean()
        out += np.convolve(x, k / np.sqrt(s), mode="same")
    return out / len(freqs)


def rolling(x: np.ndarray, n: int, fn: str = "mean") -> np.ndarray:
    """Centred rolling mean or std over ``n`` samples (edges use shorter windows)."""
    c1 = np.concatenate([[0.0], np.cumsum(x)])
    c2 = np.concatenate([[0.0], np.cumsum(x * x)])
    i = np.arange(len(x))
    lo, hi = np.clip(i - n // 2, 0, len(x)), np.clip(i + n // 2 + 1, 0, len(x))
    cnt = hi - lo
    mean = (c1[hi] - c1[lo]) / cnt
    if fn == "mean":
        return mean
    return np.sqrt(np.maximum(0.0, (c2[hi] - c2[lo]) / cnt - mean ** 2))


# ----------------------------------------------------------------------------
# Stage 1 and stage 2
# ----------------------------------------------------------------------------

def band_powers(seg: np.ndarray, rate: float = RATE_HZ) -> tuple[float, float, float]:
    """Return (locomotor power, freeze power, dominant 0.5-8 Hz frequency) of a window."""
    x = (seg - seg.mean()) * np.hanning(len(seg))
    p = np.abs(np.fft.rfft(x)) ** 2 / (len(x) ** 2) * 2
    f = np.fft.rfftfreq(len(x), 1 / rate)
    loco = float(p[(f >= LOCO_BAND[0]) & (f < LOCO_BAND[1])].sum())
    frz = float(p[(f >= FREEZE_BAND[0]) & (f < FREEZE_BAND[1])].sum())
    sel = (f >= 0.5) & (f < 8.0)
    dom = float(f[sel][np.argmax(p[sel])]) if sel.any() else 0.0
    return loco, frz, dom


@dataclass
class Window:
    """One stage-1 window."""

    start: float
    end: float
    loco: float
    freeze: float
    fi: float
    state: str


def classify_windows(svm: np.ndarray, rate: float = RATE_HZ) -> list[Window]:
    """Stage 1: label each 2.5 s window rest, walk, fog or tremor."""
    n, hop = int(WIN_S * rate), int(HOP_S * rate)
    out: list[Window] = []
    last_walk = -1e9
    for s in range(0, max(1, len(svm) - n + 1), hop):
        loco, frz, _ = band_powers(svm[s:s + n], rate)
        fi = frz / (loco + 1e-9)
        t0, t1 = s / rate, (s + n) / rate
        if loco + frz < REST_POWER:
            state = "rest"
        elif fi > FI_THRESH:
            state = "fog" if t0 - last_walk <= FOG_MEMORY_S else "tremor"
        else:
            state, last_walk = "walk", t1
        if state == "fog":
            last_walk = t1                  # a freeze can last; keep it attached
        out.append(Window(t0, t1, loco, frz, fi, state))
    return out


def state_per_sample(windows: list[Window], n: int, rate: float = RATE_HZ) -> np.ndarray:
    """Spread window states onto samples; the later overlapping window wins."""
    st = np.full(n, "rest", dtype=object)
    for w in windows:
        st[int(w.start * rate):int(w.end * rate)] = w.state
    return st


def pick_peaks(sig: np.ndarray, thresh: np.ndarray, rate: float = RATE_HZ,
               lockout_s: float = LOCKOUT_S, dominance: float = 0.65) -> np.ndarray:
    """Indices of peaks above ``thresh``, tallest first, ``lockout_s`` apart.

    A peak also has to reach ``dominance`` x the tallest peak within one lockout
    on either side. Festination keeps both strikes (similar heights) while a
    smaller mid-swing bump is dropped. Lockout shrinks to 0.45 s for such pairs.
    """
    cand = np.where((sig[1:-1] > sig[:-2]) & (sig[1:-1] >= sig[2:]) &
                    (sig[1:-1] > thresh[1:-1]))[0] + 1
    order = cand[np.argsort(-sig[cand])]
    lock, short = int(lockout_s * rate), int(0.45 * rate)
    kept: list[int] = []
    for i in order:
        near = [k for k in kept if abs(k - i) < lock]
        if any(abs(k - i) < short for k in near):
            continue
        if near and sig[i] < dominance * max(sig[k] for k in near):
            continue
        kept.append(int(i))
    return np.array(sorted(kept), dtype=int)


def refine_to_impact(peaks: np.ndarray, zm: np.ndarray, rate: float = RATE_HZ,
                     before_s: float = 0.25, after_s: float = 0.55) -> np.ndarray:
    """Move each smoothed peak to the sharpest impact nearby (the foot landing).

    The 0.5-4 Hz band peaks during swing, a few hundred ms before the heel hits.
    The raw zero-mean magnitude has its biggest spike at the landing itself.
    """
    out = []
    for p in peaks:
        lo, hi = max(0, p - int(before_s * rate)), min(len(zm), p + int(after_s * rate))
        out.append(lo + int(np.argmax(zm[lo:hi])) if hi > lo else int(p))
    out = sorted(set(out))
    keep = [i for k, i in enumerate(out) if k == 0 or i - out[k - 1] >= int(0.35 * rate)]
    return np.array(keep, dtype=int)


@dataclass
class Result:
    """Output of :func:`analyse`."""

    t: np.ndarray
    svm: np.ndarray
    filtered: np.ndarray
    thresh: np.ndarray
    state: np.ndarray
    windows: list[Window]
    strides: np.ndarray                          # sample indices
    events: list[tuple[float, float, str]] = field(default_factory=list)


def analyse(xyz: np.ndarray, rate: float = RATE_HZ, method: str = "bandpass") -> Result:
    """Run both stages on an (n, 3) array of accelerations in g."""
    svm = np.sqrt((np.asarray(xyz, float) ** 2).sum(axis=1))
    zm = svm - rolling(svm, int(rate))                  # remove gravity / drift
    windows = classify_windows(zm, rate)
    state = state_per_sample(windows, len(svm), rate)
    filt = mexican_hat(zm, rate) if method == "cwt" else bandpass(zm, rate=rate)
    thresh = np.maximum(THRESH_FLOOR_G, 0.6 * rolling(filt, int(WIN_S * rate), "std"))
    peaks = refine_to_impact(pick_peaks(filt, thresh, rate), zm, rate)
    peaks = peaks[state[peaks] == "walk"] if len(peaks) else peaks
    events, cur = [], None
    for w in windows:                                   # merge fog/tremor windows
        if w.state in ("fog", "tremor"):
            if cur and cur[2] == w.state and w.start <= cur[1]:
                cur = (cur[0], w.end, w.state)
            else:
                if cur:
                    events.append(cur)
                cur = (w.start, w.end, w.state)
    if cur:
        events.append(cur)
    t = np.arange(len(svm)) / rate
    return Result(t, svm, filt, thresh, state, windows, peaks, events)


def gait_metrics(res: Result, rate: float = RATE_HZ) -> dict[str, float]:
    """Stride count, steps, cadence, stride time, variability and freeze totals."""
    ts = res.strides / rate
    gaps = np.diff(ts)
    gaps = gaps[gaps < 2.5]                             # ignore pauses
    m = {"strides": float(len(ts)), "steps": float(2 * len(ts)),
         "fog_events": float(sum(e[2] == "fog" for e in res.events)),
         "fog_seconds": float(sum(e[1] - e[0] for e in res.events if e[2] == "fog")),
         "tremor_seconds": float(sum(e[1] - e[0] for e in res.events if e[2] == "tremor"))}
    if len(gaps) >= 3:
        m["stride_s"] = float(np.median(gaps))
        m["cadence_spm"] = 120.0 / m["stride_s"]
        m["stride_cv_pct"] = float(gaps.std() / gaps.mean() * 100)
    return m


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------

def load_csv(path: str) -> dict[str, np.ndarray]:
    """Read a gait_cue.py or gait_sim.py CSV into arrays."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{path} is empty")
    out = {"t_s": np.array([float(r["t_s"]) for r in rows]),
           "xyz": np.array([[float(r["x_g"]), float(r["y_g"]), float(r["z_g"])] for r in rows])}
    if "label" in rows[0]:
        out["label"] = np.array([r["label"] for r in rows], dtype=object)
    if "true_stride" in rows[0]:
        out["true_stride"] = np.array([int(r["true_stride"]) for r in rows])
    return out


def score_strides(found: np.ndarray, truth: np.ndarray, rate: float = RATE_HZ) -> dict[str, float]:
    """Match detected and true stride indices within ``MATCH_TOL_S``; return P/R/F1."""
    tol, used, tp = int(MATCH_TOL_S * rate), set(), 0
    for i in found:
        best = [j for j in truth if abs(j - i) <= tol and j not in used]
        if best:
            used.add(min(best, key=lambda j: abs(j - i)))
            tp += 1
    p = tp / len(found) if len(found) else 1.0 if not len(truth) else 0.0
    r = tp / len(truth) if len(truth) else 1.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0}


def main() -> None:
    """Analyse CSV files and score them when they carry ground truth."""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+")
    p.add_argument("--method", choices=("bandpass", "cwt"), default="bandpass")
    args = p.parse_args()
    f1s, errs, state_acc = [], [], []
    print(f"{'file':<34}{'true':>5}{'found':>6}{'F1':>6}{'cad':>6}{'CV%':>6}{'FoG':>5}{'state':>7}")
    for path in args.files:
        d = load_csv(path)
        res = analyse(d["xyz"], method=args.method)
        m = gait_metrics(res)
        line = f"{path.split('/')[-1][:33]:<34}"
        if "true_stride" in d:
            truth = np.where(d["true_stride"] == 1)[0]
            sc = score_strides(res.strides, truth)
            f1s.append(sc["f1"])
            errs.append(abs(len(res.strides) - len(truth)))
            acc = float(np.mean(res.state == d["label"]))
            state_acc.append(acc)
            line += f"{len(truth):>5}{len(res.strides):>6}{sc['f1']:>6.2f}"
        else:
            line += f"{'-':>5}{len(res.strides):>6}{'-':>6}"
            acc = None
        line += f"{m.get('cadence_spm', 0):>6.0f}{m.get('stride_cv_pct', 0):>6.1f}"
        line += f"{int(m['fog_events']):>5}" + (f"{acc:>7.0%}" if acc is not None else f"{'-':>7}")
        print(line)
    if f1s:
        print(f"\n{len(f1s)} files  mean stride F1 {np.mean(f1s):.3f}  "
              f"mean |count error| {np.mean(errs):.2f} strides  "
              f"sample state accuracy {np.mean(state_acc):.1%}  ({args.method})")


if __name__ == "__main__":
    sys.exit(main())
