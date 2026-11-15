#!/usr/bin/env python3
"""Scan for a Myo, buzz once, and print 10 seconds of live rates.

Run from the project folder:

    .venv/bin/python myo_test.py
"""

from __future__ import annotations

import math
import sys
import threading
import time
from datetime import datetime

from bleak import BleakError

from myo_source import EmgSample, MyoNotFoundError, MyoSource

SECONDS = 10.0


class _Stats:
    """Counters shared by the Myo thread and the printer."""

    def __init__(self) -> None:
        """Start with empty motion and muscle totals."""
        self.lock = threading.Lock()
        self.motion_n = 0
        self.emg_n = 0
        self.ax = 0.0
        self.ay = 0.0
        self.az = 0.0
        self.have_motion = False
        self.emg_square_sum = 0.0
        self.emg_value_n = 0

    def on_motion(self, _time: datetime, ax: float, ay: float, az: float) -> None:
        """Record one accelerometer sample."""
        with self.lock:
            self.motion_n += 1
            self.ax, self.ay, self.az = ax, ay, az
            self.have_motion = True

    def on_emg(self, _time: datetime, emg: EmgSample) -> None:
        """Record one 8-channel muscle sample."""
        with self.lock:
            self.emg_n += 1
            self.emg_square_sum += sum(float(channel) * float(channel) for channel in emg)
            self.emg_value_n += len(emg)

    def snapshot(self, reset_muscle: bool) -> tuple[int, int, float | None, float | None]:
        """Return motion count, EMG count, acceleration magnitude, and muscle RMS."""
        with self.lock:
            magnitude = None
            if self.have_motion:
                magnitude = math.sqrt(self.ax * self.ax + self.ay * self.ay + self.az * self.az)
            muscle = None
            if self.emg_value_n:
                muscle = math.sqrt(self.emg_square_sum / self.emg_value_n)
            motion_n, emg_n = self.motion_n, self.emg_n
            if reset_muscle:
                self.emg_square_sum = 0.0
                self.emg_value_n = 0
            return motion_n, emg_n, magnitude, muscle


def print_checklist() -> None:
    """Print what to check when no Myo can be reached."""
    print("No Myo armband is streaming. Check:")
    print("- Is it charged?")
    print("- Is it connected to another device?")
    print("- Is Bluetooth on?")
    print(
        "- Does this terminal, or Cursor, have Bluetooth permission "
        "in macOS System Settings > Privacy & Security > Bluetooth?"
    )


def _format_line(
    elapsed: float,
    motion_n: int,
    emg_n: int,
    magnitude: float | None,
    muscle: float | None,
) -> str:
    """Format one status line. Rates are counts divided by elapsed seconds."""
    motion_hz = motion_n / elapsed
    emg_hz = emg_n / elapsed
    accel = "   n/a" if magnitude is None else f"{magnitude:6.3f}"
    muscle_text = "   n/a" if muscle is None else f"{muscle:6.1f}"
    return (
        f"t={elapsed:4.1f}s  motion={motion_hz:6.1f} Hz ({motion_n} samples)  "
        f"emg={emg_hz:6.1f} Hz ({emg_n} samples)  "
        f"|a|={accel} g  muscle_rms={muscle_text}"
    )


def main() -> int:
    """Connect, vibrate once, and print rates for 10 seconds."""
    stats = _Stats()
    source = MyoSource(on_motion=stats.on_motion, on_emg=stats.on_emg)
    print("Scanning for a Myo armband...")
    try:
        source.start()
        print("One short buzz, then 10 seconds of measurements.")
        print("|a| is the latest acceleration magnitude. A still band reads about 1 g.")
        print("muscle_rms is the RMS of raw EMG samples since the previous line.")
        source.vibrate("short")
        started = time.perf_counter()
        for _ in range(int(SECONDS)):
            time.sleep(1.0)
            elapsed = time.perf_counter() - started
            motion_n, emg_n, magnitude, muscle = stats.snapshot(reset_muscle=True)
            print(_format_line(elapsed, motion_n, emg_n, magnitude, muscle))
    except MyoNotFoundError as exc:
        print(exc)
        print_checklist()
        return 1
    except (BleakError, TimeoutError, OSError) as exc:
        print(f"Could not connect to a Myo: {exc}")
        print_checklist()
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        source.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
