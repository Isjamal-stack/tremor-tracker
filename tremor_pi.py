#!/usr/bin/env python3
"""
Tremor Tracker - runs on the Raspberry Pi (Zero 2W, 4 or 5).

Reads a Grove 3-axis accelerometer over I2C, measures tremor frequency and
strength, and writes raw samples, tremor features and medication-button
presses to Tiger Data.

Supported accelerometer chips (auto-detected):
  ADXL345  (I2C 0x53 or 0x1D)   - good
  LIS3DH   (I2C 0x19 or 0x18)   - good (Grove "LIS3DHTR")
  MMA7660  (I2C 0x4C)           - works, but coarse: catches big tremors only

Quick start on the Pi:
  python3 tremor_pi.py --selftest        # check the math works (no sensor needed)
  python3 tremor_pi.py --dry-run         # read the sensor, print results, no database
  export TIGER_URL='postgres://tsdbadmin:...@....tsdb.cloud.timescale.com:PORT/tsdb?sslmode=require'
  python3 tremor_pi.py                   # full run, writes to Tiger Data

Useful flags:
  --device NAME      device id stored in the database (default: the Pi's hostname)
  --button-pin N     BCM GPIO number for the Grove button (default 17 = physical pin 11)
  --no-button        run without the medication button
  --log-csv FILE     also save raw samples to a CSV (handy for tuning on your laptop)
  --min-rms G        tremor threshold on strength, in g (default 0.015)
  --min-ratio R      tremor threshold on how much motion is in the 3-12 Hz band (default 0.5)
"""

import argparse
import collections
import csv
import datetime as dt
import math
import queue
import signal
import socket
import struct
import sys
import threading
import time

import numpy as np

# ----------------------------------------------------------------------------
# Signal processing
# ----------------------------------------------------------------------------

TREMOR_BAND = (3.0, 12.0)    # Hz. Parkinsonian ~4-6 Hz, essential tremor ~4-12 Hz
FAST_FLOOR = 1.5             # Hz. Slower than this = deliberate arm movement, ignored


def tremor_features(xyz, fs, band=TREMOR_BAND):
    """
    xyz: (N, 3) array of acceleration in g. fs: sample rate in Hz.

    Returns dict with:
      peak_hz    - dominant frequency inside the tremor band
      rms_g      - RMS acceleration of the in-band (tremor) component, in g
      band_ratio - of all the fast (>1.5 Hz) motion, the fraction inside the band.
                   Sensor noise alone gives ~0.2; a real tremor gives ~0.8+.
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

    # Peak frequency with parabolic interpolation for sub-bin precision
    band_idx = np.flatnonzero(in_band)
    k = band_idx[np.argmax(power[band_idx])]
    peak_hz = float(freqs[k])
    if 0 < k < len(power) - 1:
        a, b, c = np.log(power[k - 1:k + 2] + 1e-20)
        denom = a - 2 * b + c
        if denom != 0:
            peak_hz += float(0.5 * (a - c) / denom) * (freqs[1] - freqs[0])

    # Strength: RMS of the in-band component via Parseval on the windowed spectrum
    # (one-sided spectrum, so x2; normalised by the window's energy)
    rms_g = float(np.sqrt(2.0 * power[in_band].sum() / (n * np.sum(w ** 2))))

    return {"peak_hz": peak_hz, "rms_g": rms_g, "band_ratio": band_ratio}


def is_tremor(f, min_rms, min_ratio):
    return f["rms_g"] >= min_rms and f["band_ratio"] >= min_ratio


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

def ts(t):
    return dt.datetime.fromtimestamp(t, tz=dt.timezone.utc)


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
            d = self.device
            with self.conn.cursor() as cur:
                if self.raw:
                    self.execute_values(
                        cur, "INSERT INTO accel_raw (time, device_id, ax, ay, az) VALUES %s",
                        [(ts(t), d, x, y, z) for t, x, y, z in self.raw], page_size=1000)
                if self.feat:
                    self.execute_values(
                        cur, "INSERT INTO tremor_features "
                             "(time, device_id, peak_hz, rms_g, band_ratio, tremor) VALUES %s",
                        [(ts(t), d, p, r, b, tr) for t, p, r, b, tr in self.feat])
                if self.med:
                    self.execute_values(
                        cur, "INSERT INTO med_events (time, device_id, note) VALUES %s",
                        [(ts(t), d, note) for t, note in self.med])
            self.conn.commit()
            self.raw, self.feat, self.med = [], [], []
        except Exception as e:
            print(f"[db] write failed, will retry: {e}", file=sys.stderr)
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
              f"ratio {f['band_ratio']:.2f}  tremor={tr}")
        if f0:
            ok &= abs(f["peak_hz"] - f0) < 0.3 and tr
        else:
            ok &= not tr
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Tremor tracker (Raspberry Pi)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="no database, just print")
    ap.add_argument("--url", default=None, help="Tiger Data URL (or set TIGER_URL)")
    ap.add_argument("--device", default=socket.gethostname())
    ap.add_argument("--rate", type=float, default=100.0, help="sample rate, Hz")
    ap.add_argument("--window", type=int, default=256, help="samples per analysis window")
    ap.add_argument("--hop", type=int, default=128, help="samples between analyses")
    ap.add_argument("--min-rms", type=float, default=0.015)
    ap.add_argument("--min-ratio", type=float, default=0.5)
    ap.add_argument("--button-pin", type=int, default=17)
    ap.add_argument("--no-button", action="store_true")
    ap.add_argument("--log-csv", default=None)
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    if SMBus is None:
        sys.exit("Missing I2C library: sudo apt install -y python3-smbus")

    import os
    url = args.url or os.environ.get("TIGER_URL")
    writer = None
    if not args.dry_run:
        if not url:
            sys.exit("Set TIGER_URL (or pass --url), or use --dry-run.")
        writer = TigerWriter(url, args.device)
        writer.start()

    bus = SMBus(1)
    sensor = detect_sensor(bus)
    print(f"Sensor: {sensor.name} at {hex(sensor.addr)} | device '{args.device}' | "
          f"{'DRY RUN' if args.dry_run else 'writing to Tiger Data'}")

    # Medication button (Grove button outputs HIGH when pressed)
    button = None
    if not args.no_button:
        try:
            from gpiozero import Button

            def on_press():
                now = time.time()
                print(f"\n💊 medication logged at {time.strftime('%H:%M:%S')}\n")
                if writer:
                    writer.put("med", (now, "dose"))

            button = Button(args.button_pin, pull_up=False, bounce_time=0.05)
            button.when_pressed = on_press
            print(f"Medication button on GPIO{args.button_pin}")
        except Exception as e:
            print(f"(button disabled: {e})")

    csv_file = csv_w = None
    if args.log_csv:
        csv_file = open(args.log_csv, "w", newline="")
        csv_w = csv.writer(csv_file)
        csv_w.writerow(["unix_time", "ax_g", "ay_g", "az_g"])

    running = True

    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    window = collections.deque(maxlen=args.window)   # (mono_time, ax, ay, az)
    raw_batch = []
    since_last = 0
    period = 1.0 / args.rate
    next_t = time.perf_counter()
    last_batch_send = time.time()
    errors = 0

    print("Sampling... (Ctrl+C to stop)\n")
    while running:
        try:
            ax, ay, az = sensor.read()
            errors = 0
        except OSError as e:
            errors += 1
            if errors > 50:
                sys.exit(f"Lost the sensor ({e}). Check the wiring.")
            time.sleep(0.01)
            continue

        now_wall, now_mono = time.time(), time.perf_counter()
        window.append((now_mono, ax, ay, az))
        raw_batch.append((now_wall, ax, ay, az))
        since_last += 1

        if csv_w:
            csv_w.writerow([f"{now_wall:.4f}", f"{ax:.5f}", f"{ay:.5f}", f"{az:.5f}"])

        # Send raw samples to the writer about once a second
        if writer and now_wall - last_batch_send >= 1.0:
            for row in raw_batch:
                writer.put("raw", row)
            raw_batch, last_batch_send = [], now_wall
        elif not writer and len(raw_batch) > 1000:
            raw_batch = []

        # Analyse every `hop` samples once the window is full
        if len(window) == args.window and since_last >= args.hop:
            since_last = 0
            arr = np.array(window)
            span = arr[-1, 0] - arr[0, 0]
            fs = (len(arr) - 1) / span if span > 0 else args.rate   # real rate, not assumed
            f = tremor_features(arr[:, 1:], fs)
            tr = is_tremor(f, args.min_rms, args.min_ratio)
            bar = "█" * min(40, int(f["rms_g"] * 400))
            print(f"{time.strftime('%H:%M:%S')}  {f['peak_hz']:5.2f} Hz  "
                  f"{f['rms_g']:.3f} g  ratio {f['band_ratio']:.2f}  fs {fs:5.1f}  "
                  f"{'TREMOR ' if tr else '       '}{bar}")
            if writer:
                writer.put("feat", (now_wall, f["peak_hz"], f["rms_g"], f["band_ratio"], tr))

        # Keep a steady sample rate
        next_t += period
        delay = next_t - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.perf_counter()       # fell behind; resync

    print("\nStopping...")
    if writer:
        for row in raw_batch:
            writer.put("raw", row)
        writer.close()
    if csv_file:
        csv_file.close()
    if button:
        button.close()
    bus.close()


if __name__ == "__main__":
    main()
