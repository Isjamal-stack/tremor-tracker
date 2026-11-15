"""Laptop bridge: Bluetooth "Tremor Tracker" -> local files the dashboard reads.

  python gait_bridge.py            # connect to the Pi over Bluetooth
  python gait_bridge.py --sim      # no hardware: run the same engine on simulated walking

Writes, all on this laptop:
  data/live/live.json             latest status + last 10 s of magnitude (for live charts)
  data/history/YYYY-MM-DD.csv     one row per second: the tracker history
Reads data/live/commands.jsonl    commands from the dashboard, forwarded to the device

Monitoring prototype, not a medical device.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import time
from collections import deque
from datetime import datetime, timezone

import gait_engine as ge

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST_DIR = os.path.join(ROOT, "data", "history")
LIVE_JSON = os.path.join(ge.LIVE_DIR, "live.json")
CMD_FILE = os.path.join(ge.LIVE_DIR, "commands.jsonl")
HIST_COLS = ["time_utc", "source", "state", "steps", "cadence_spm", "stride_cv_pct",
             "freeze_index", "fog_events", "cue_on", "bpm"]


class Sink:
    """Collects status and samples and writes the live file and the history."""

    def __init__(self, source: str) -> None:
        """Prepare folders and the rolling buffers."""
        os.makedirs(ge.LIVE_DIR, exist_ok=True)
        os.makedirs(HIST_DIR, exist_ok=True)
        self.source = source
        self.status: dict = {}
        self.svm: deque[float] = deque(maxlen=250)        # 10 s at 25 Hz
        self._dec = 0
        self.last_hist = 0.0
        self.cmd_pos = os.path.getsize(CMD_FILE) if os.path.exists(CMD_FILE) else 0

    def add_samples(self, samples: list[tuple[float, float, float]]) -> None:
        """Keep every 4th magnitude (100 Hz -> 25 Hz) for the live chart."""
        for v in ge.svm_of(samples):
            self._dec += 1
            if self._dec % 4 == 0:
                self.svm.append(round(v, 3))

    def write(self) -> None:
        """Write live.json atomically and append a history row once per second."""
        live = {"t": time.time(), "source": self.source, "status": self.status,
                "svm": list(self.svm)}
        tmp = LIVE_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(live, f)
        os.replace(tmp, LIVE_JSON)
        now = time.time()
        if self.status and now - self.last_hist >= 1.0:
            self.last_hist = now
            s = self.status
            path = os.path.join(HIST_DIR, datetime.now().strftime("%Y-%m-%d") + ".csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(HIST_COLS)
                w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), self.source,
                            s.get("s"), s.get("n"), s.get("c"), s.get("v"), s.get("f"),
                            s.get("k"), s.get("q"), s.get("b") or ""])

    def new_commands(self) -> list[dict]:
        """Return commands appended to commands.jsonl since the last call."""
        if not os.path.exists(CMD_FILE):
            return []
        with open(CMD_FILE) as f:
            f.seek(self.cmd_pos)
            lines = f.readlines()
            self.cmd_pos = f.tell()
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out


async def run_sim(sink: Sink) -> None:
    """Run the engine locally on simulated walking (no Bluetooth)."""
    engine = ge.Engine(ge.SimSensor(), ge.SimPulse(), None)
    print("Simulating the device locally. Ctrl+C stops.")
    while True:
        await asyncio.sleep(0.25)
        for cmd in sink.new_commands():
            print("command", cmd, "->", engine.handle(cmd))
        sink.add_samples([(x / 1000, y / 1000, z / 1000) for x, y, z in engine.drain_samples()])
        sink.status = engine.status()
        sink.write()


async def run_ble(sink: Sink, name: str) -> None:
    """Find the device by name, subscribe, and forward commands; reconnect on loss."""
    from bleak import BleakClient, BleakScanner
    while True:
        print(f'Scanning for "{name}"...')
        dev = await BleakScanner.find_device_by_filter(
            lambda d, ad: (d.name or ad.local_name or "") == name or
            ge.SERVICE_UUID in [u.lower() for u in ad.service_uuids], timeout=15)
        if dev is None:
            sink.status = {}
            sink.write()
            continue
        try:
            async with BleakClient(dev) as client:
                print(f"Connected to {dev.name} ({dev.address})")

                def on_status(_, data: bytearray) -> None:
                    try:
                        sink.status = json.loads(bytes(data).decode())
                    except ValueError:
                        pass

                def on_accel(_, data: bytearray) -> None:
                    sink.add_samples(ge.unpack_samples(bytes(data))[1])

                await client.start_notify(ge.STATUS_UUID, on_status)
                await client.start_notify(ge.ACCEL_UUID, on_accel)
                while client.is_connected:
                    await asyncio.sleep(0.25)
                    for cmd in sink.new_commands():
                        await client.write_gatt_char(ge.CONTROL_UUID, json.dumps(cmd).encode(),
                                                     response=True)
                        print("sent", cmd)
                    sink.write()
        except Exception as exc:                     # link dropped: retry
            print(f"Bluetooth error: {exc}; retrying")
            await asyncio.sleep(2)


def main() -> None:
    """Parse arguments and run the bridge."""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sim", action="store_true", help="simulate the device on this laptop")
    p.add_argument("--name", default=ge.DEVICE_NAME)
    args = p.parse_args()
    sink = Sink("simulated" if args.sim else "bluetooth")
    try:
        asyncio.run(run_sim(sink) if args.sim else run_ble(sink, args.name))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
