"""MAX30102 pulse sensor (I2C 0x57) for the Pi wearable prototype.

Wiring (Pi Zero 2 W): VIN pin 1 (3.3 V), SDA pin 3, SCL pin 5, GND pin 6,
INT pin 11 (GPIO17, optional; we poll the FIFO instead).

The sensor runs at 100 samples/s with 4x averaging, so the FIFO fills at 25 Hz.
Heart rate comes from the infrared channel: remove the slow baseline, then take
the strongest frequency between 0.7 and 3.5 Hz (42-210 bpm) over the last 8 s.
SpO2 is not reported; it needs per-device calibration this prototype does not have.

Monitoring prototype only, not a medical device.

  python3 heart_rate.py            # print bpm once a second; finger on the sensor
"""
from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

ADDR = 0x57
PART_ID = 0x15
FINGER_IR = 20000          # IR counts below this mean nothing is on the sensor
FS_HZ = 25.0               # 100 Hz with 4-sample averaging


class MAX30102:
    """Minimal register driver: SpO2 mode (red + IR), FIFO polling."""

    def __init__(self, bus_no: int = 1, addr: int = ADDR, led: int = 0x24) -> None:
        """Reset and configure the sensor; raise RuntimeError if it is not a MAX30102."""
        from smbus2 import SMBus
        self.bus, self.addr = SMBus(bus_no), addr
        pid = self.bus.read_byte_data(addr, 0xFF)
        if pid != PART_ID:
            raise RuntimeError(f"device at 0x{addr:02X} has part id 0x{pid:02X}, not MAX30102")
        w = lambda r, v: self.bus.write_byte_data(addr, r, v)
        w(0x09, 0x40)                       # reset
        time.sleep(0.1)
        self.bus.read_byte_data(addr, 0x00)  # clear interrupt flags
        self.bus.read_byte_data(addr, 0x01)
        w(0x08, 0x50)                       # FIFO: 4-sample average, rollover on
        w(0x0A, 0x27)                       # 4096 nA range, 100 Hz, 18-bit
        w(0x0C, led)                        # red LED ~7 mA
        w(0x0D, led)                        # IR LED ~7 mA
        for r in (0x04, 0x05, 0x06):        # FIFO pointers
            w(r, 0)
        w(0x09, 0x03)                       # SpO2 mode

    def read_fifo(self) -> list[tuple[int, int]]:
        """Return every (red, ir) sample waiting in the FIFO."""
        n = (self.bus.read_byte_data(self.addr, 0x04) - self.bus.read_byte_data(self.addr, 0x06)) & 31
        out = []
        for _ in range(n):
            d = self.bus.read_i2c_block_data(self.addr, 0x07, 6)
            out.append((((d[0] << 16) | (d[1] << 8) | d[2]) & 0x3FFFF,
                        ((d[3] << 16) | (d[4] << 8) | d[5]) & 0x3FFFF))
        return out

    def temperature_c(self) -> float:
        """Die temperature in Celsius (the chip, not the body)."""
        self.bus.write_byte_data(self.addr, 0x21, 0x01)
        time.sleep(0.05)
        return self.bus.read_byte_data(self.addr, 0x1F) + self.bus.read_byte_data(self.addr, 0x20) * 0.0625

    def close(self) -> None:
        """Put the sensor in shutdown (LEDs off)."""
        try:
            self.bus.write_byte_data(self.addr, 0x09, 0x80)
        except OSError:
            pass


def estimate_bpm(ir: np.ndarray, fs: float = FS_HZ) -> tuple[float | None, float]:
    """Return (bpm or None, quality 0-1) from an IR window of a few seconds.

    Quality is the share of 0.7-3.5 Hz power at the winning peak +/- 0.15 Hz.
    """
    if len(ir) < fs * 4 or float(np.mean(ir)) < FINGER_IR:
        return None, 0.0
    k = max(3, int(fs * 0.75))
    ac = ir - np.convolve(ir, np.ones(k) / k, mode="same")
    ac = ac[k:-k]
    if len(ac) < fs * 3:
        return None, 0.0
    spec = np.abs(np.fft.rfft((ac - ac.mean()) * np.hanning(len(ac)), n=4096)) ** 2
    f = np.fft.rfftfreq(4096, 1 / fs)
    band = (f > 0.7) & (f < 3.5)
    if not band.any() or spec[band].sum() <= 0:
        return None, 0.0
    peak = f[band][int(np.argmax(spec[band]))]
    near = band & (np.abs(f - peak) < 0.15)
    return float(60 * peak), float(spec[near].sum() / spec[band].sum())


class PulseMonitor:
    """Background reader that keeps the latest bpm, quality and finger state."""

    def __init__(self, window_s: float = 8.0) -> None:
        """Open the sensor (if present) and start polling it in a thread."""
        self.sensor: MAX30102 | None = None
        self.error = ""
        try:
            self.sensor = MAX30102()
        except Exception as exc:          # sensor missing or bus error
            self.error = str(exc)
        self.ir: deque[float] = deque(maxlen=int(window_s * FS_HZ))
        self.bpm: float | None = None
        self.quality = 0.0
        self.finger = False
        self._stop = threading.Event()
        if self.sensor:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            try:
                for _, ir in self.sensor.read_fifo():
                    self.ir.append(float(ir))
            except OSError as exc:
                self.error = str(exc)
            now = time.monotonic()
            if now - last >= 1.0 and self.ir:
                last = now
                arr = np.array(self.ir)
                self.finger = float(arr[-10:].mean()) >= FINGER_IR
                bpm, q = estimate_bpm(arr)
                self.bpm, self.quality = (bpm, q) if q >= 0.25 else (None, q)
            time.sleep(0.04)

    def status(self) -> dict:
        """Snapshot for the dashboard / Bluetooth status message."""
        return {"hr_ok": self.sensor is not None, "finger": self.finger,
                "bpm": round(self.bpm) if self.bpm else None,
                "hr_quality": round(self.quality, 2)}

    def close(self) -> None:
        """Stop polling and power the sensor down."""
        self._stop.set()
        if self.sensor:
            self.sensor.close()


def main() -> None:
    """Print heart rate once a second until Ctrl+C."""
    mon = PulseMonitor()
    if not mon.sensor:
        raise SystemExit(f"MAX30102 not available: {mon.error}")
    print(f"MAX30102 ready, die {mon.sensor.temperature_c():.1f} C. Finger on the sensor. Ctrl+C stops.")
    try:
        while True:
            time.sleep(1)
            s = mon.status()
            bpm = f"{s['bpm']} bpm" if s["bpm"] else "--"
            print(f"\r finger {'yes' if s['finger'] else 'no '}  heart rate {bpm:<8} "
                  f"quality {s['hr_quality']:.2f}   ", end="", flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        mon.close()


if __name__ == "__main__":
    main()
