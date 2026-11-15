"""Tiny activity model for the ankle sensor: rest / walk / fog / tremor.

Softmax (multinomial logistic) regression on 9 features of each 2.5 s window,
written in numpy so the Pi can run it. Train on the laptop, copy gait_model.json
to the Pi; gait_engine.py loads it automatically and otherwise uses the rules.

Labels in trial files map to classes: shuffle/festination/turn/normal* -> walk.

  python3 gait_model.py train data/sim/*.csv data/real/*.csv
  python3 gait_model.py eval data/real/*.csv

Trained on simulated data plus acted trials from a healthy volunteer; a
monitoring prototype, not a clinical classifier.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

import gait_algo

CLASSES = ("rest", "walk", "fog", "tremor")
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gait_model.json")
LABEL_MAP = {"rest": "rest", "walk": "walk", "shuffle": "walk", "festination": "walk",
             "turn": "walk", "normal": "walk", "normal_fast": "walk", "fog": "fog",
             "tremor": "tremor"}
FEATURES = ("log_loco", "log_freeze", "log_fi", "svm_std", "dom_hz", "peak_ratio",
            "xyz_std", "jerk", "autocorr")


def window_features(xyz: np.ndarray, rate: float = gait_algo.RATE_HZ) -> np.ndarray:
    """Feature vector for one window of (n, 3) accelerations in g."""
    svm = np.sqrt((xyz ** 2).sum(axis=1))
    zm = svm - svm.mean()
    loco, frz, dom = gait_algo.band_powers(svm, rate)
    sd = float(zm.std()) + 1e-6
    ac = np.correlate(zm, zm, "full")[len(zm) - 1:]
    ac = ac / (ac[0] + 1e-9)
    lo, hi = int(0.4 * rate), min(len(ac), int(1.6 * rate))
    return np.array([
        np.log10(loco + 1e-7), np.log10(frz + 1e-7), np.log10(frz / (loco + 1e-9) + 1e-3),
        sd, dom, float(np.abs(zm).max() / sd), float(xyz.std(axis=0).mean()),
        float(np.abs(np.diff(svm)).mean() * rate), float(ac[lo:hi].max()) if hi > lo else 0.0])


def windows_from_file(path: str) -> tuple[np.ndarray, np.ndarray]:
    """Return (features, class index) for every labelled 2.5 s window in a CSV."""
    d = gait_algo.load_csv(path)
    if "label" not in d:
        return np.zeros((0, len(FEATURES))), np.zeros(0, int)
    n, hop = int(gait_algo.WIN_S * gait_algo.RATE_HZ), int(gait_algo.HOP_S * gait_algo.RATE_HZ)
    X, y = [], []
    for s in range(0, len(d["xyz"]) - n + 1, hop):
        labs = [LABEL_MAP.get(str(v)) for v in d["label"][s:s + n]]
        labs = [v for v in labs if v]
        if not labs:
            continue
        top = max(set(labs), key=labs.count)
        if labs.count(top) < 0.7 * len(labs):       # skip windows that straddle phases
            continue
        X.append(window_features(d["xyz"][s:s + n]))
        y.append(CLASSES.index(top))
    return np.array(X).reshape(-1, len(FEATURES)), np.array(y, int)


def train(X: np.ndarray, y: np.ndarray, epochs: int = 3000, lr: float = 0.3,
          l2: float = 1e-3) -> dict:
    """Fit class-balanced softmax regression; return a JSON-ready model dict."""
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-6
    Z = (X - mu) / sd
    k = len(CLASSES)
    W, b = np.zeros((Z.shape[1], k)), np.zeros(k)
    Y = np.eye(k)[y]
    counts = np.bincount(y, minlength=k).astype(float)
    wts = (len(y) / (k * np.maximum(counts, 1)))[y][:, None]
    for _ in range(epochs):
        logits = Z @ W + b
        logits -= logits.max(axis=1, keepdims=True)
        P = np.exp(logits)
        P /= P.sum(axis=1, keepdims=True)
        G = (P - Y) * wts / len(y)
        W -= lr * (Z.T @ G + l2 * W)
        b -= lr * G.sum(axis=0)
    return {"classes": list(CLASSES), "features": list(FEATURES), "mu": mu.tolist(),
            "sd": sd.tolist(), "W": W.tolist(), "b": b.tolist()}


def predict_proba(model: dict, X: np.ndarray) -> np.ndarray:
    """Class probabilities for feature rows ``X``."""
    Z = (np.atleast_2d(X) - np.array(model["mu"])) / np.array(model["sd"])
    logits = Z @ np.array(model["W"]) + np.array(model["b"])
    logits -= logits.max(axis=1, keepdims=True)
    P = np.exp(logits)
    return P / P.sum(axis=1, keepdims=True)


def predict_window(model: dict, xyz: np.ndarray) -> str:
    """Most likely class name for one window of accelerations."""
    return model["classes"][int(np.argmax(predict_proba(model, window_features(xyz))[0]))]


def load(path: str = MODEL_PATH) -> dict | None:
    """Load the saved model, or None if there is none yet."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def confusion(model: dict, X: np.ndarray, y: np.ndarray) -> tuple[float, np.ndarray]:
    """Return (accuracy, confusion matrix rows=true, cols=predicted)."""
    pred = predict_proba(model, X).argmax(axis=1)
    cm = np.zeros((len(CLASSES), len(CLASSES)), int)
    for t, p in zip(y, pred):
        cm[t, p] += 1
    return float((pred == y).mean()) if len(y) else 0.0, cm


def _load_many(paths: list[str]) -> tuple[np.ndarray, np.ndarray]:
    parts = [windows_from_file(p) for p in paths]
    X = np.vstack([p[0] for p in parts if len(p[0])]) if parts else np.zeros((0, 9))
    y = np.concatenate([p[1] for p in parts if len(p[1])]) if parts else np.zeros(0, int)
    return X, y


def main() -> None:
    """``train`` files... or ``eval`` files..."""
    if len(sys.argv) < 3 or sys.argv[1] not in ("train", "eval"):
        raise SystemExit(__doc__)
    paths = sys.argv[2:]
    if sys.argv[1] == "train":
        real = [p for p in paths if "/real/" in p]
        sim = [p for p in paths if p not in real]
        if real and len(real) >= 2:                  # leave-one-real-file-out check
            accs = []
            for hold in real:
                Xt, yt = _load_many(sim + [p for p in real if p != hold])
                Xh, yh = windows_from_file(hold)
                if len(yh):
                    accs.append(confusion(train(Xt, yt), Xh, yh)[0])
            print(f"Leave-one-trial-out accuracy on real trials: {np.mean(accs):.1%} "
                  f"({', '.join(f'{a:.0%}' for a in accs)})")
        X, y = _load_many(paths)
        model = train(X, y)
        acc, cm = confusion(model, X, y)
        model["train_windows"] = int(len(y))
        model["train_accuracy"] = acc
        with open(MODEL_PATH, "w") as f:
            json.dump(model, f)
        print(f"Trained on {len(y)} windows ({dict(zip(CLASSES, np.bincount(y, minlength=4).tolist()))}), "
              f"training accuracy {acc:.1%}. Saved {MODEL_PATH}")
    else:
        model = load()
        if model is None:
            raise SystemExit("No gait_model.json yet; run train first.")
        X, y = _load_many(paths)
        acc, cm = confusion(model, X, y)
        print(f"{len(y)} windows, accuracy {acc:.1%}")
        print("true \\ pred  " + "  ".join(f"{c:>6}" for c in CLASSES))
        for c, row in zip(CLASSES, cm):
            print(f"{c:>11}  " + "  ".join(f"{v:>6}" for v in row))


if __name__ == "__main__":
    main()
