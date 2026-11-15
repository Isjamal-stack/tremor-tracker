"""Bluetooth LE peripheral "Tremor Tracker" on the Pi (runs gait_engine.Engine).

Characteristics (service 7e570001-...):
  status   notify/read  compact JSON, 2x per second (state, steps, cadence, FI, bpm...)
  accel    notify       raw accelerometer, 10 samples per packet in milli-g
  control  write        JSON commands: beep, metronome, cue, cue_bpm, fi, reset

  ~/gaitenv/bin/python gait_ble.py          # needs Bluetooth unblocked and powered

Monitoring prototype, not a medical device.
"""
from __future__ import annotations

import asyncio
import json
import logging

from bless import (BlessServer, GATTAttributePermissions, GATTCharacteristicProperties)

import gait_engine as ge
from gait_cue import MMA7660

log = logging.getLogger("gait_ble")


async def run() -> None:
    """Start the engine and serve it over BLE until interrupted."""
    try:
        from heart_rate import PulseMonitor
        pulse = PulseMonitor()
        pulse = pulse if pulse.sensor else None
    except Exception:
        pulse = None
    engine = ge.Engine(MMA7660(), pulse, ge.Buzzer(16))

    def on_read(characteristic, **kwargs) -> bytearray:
        return bytearray(ge.status_json(engine))

    def on_write(characteristic, value, **kwargs) -> None:
        if str(characteristic.uuid).lower() != ge.CONTROL_UUID:
            return
        try:
            cmd = json.loads(bytes(value).decode())
            log.info("command %s -> %s", cmd, engine.handle(cmd))
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("bad command %r: %s", bytes(value)[:60], exc)

    server = BlessServer(name=ge.DEVICE_NAME, loop=asyncio.get_running_loop())
    server.read_request_func = on_read
    server.write_request_func = on_write
    await server.add_new_service(ge.SERVICE_UUID)
    rw = GATTAttributePermissions.readable | GATTAttributePermissions.writeable
    await server.add_new_characteristic(
        ge.SERVICE_UUID, ge.STATUS_UUID,
        GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
        None, GATTAttributePermissions.readable)
    await server.add_new_characteristic(
        ge.SERVICE_UUID, ge.ACCEL_UUID, GATTCharacteristicProperties.notify,
        None, GATTAttributePermissions.readable)
    await server.add_new_characteristic(
        ge.SERVICE_UUID, ge.CONTROL_UUID,
        GATTCharacteristicProperties.write | GATTCharacteristicProperties.write_without_response,
        None, rw)
    await server.start()
    print(f'Advertising as "{ge.DEVICE_NAME}". Ctrl+C stops.', flush=True)
    engine.buzzer.beep_async(0.5)
    seq, tick = 0, 0
    try:
        while True:
            await asyncio.sleep(0.1)
            samples = engine.drain_samples()
            for i in range(0, len(samples), 10):
                server.get_characteristic(ge.ACCEL_UUID).value = bytearray(
                    ge.pack_samples(seq, samples[i:i + 10]))
                server.update_value(ge.SERVICE_UUID, ge.ACCEL_UUID)
                seq += 1
            tick += 1
            if tick % 5 == 0:
                server.get_characteristic(ge.STATUS_UUID).value = bytearray(ge.status_json(engine))
                server.update_value(ge.SERVICE_UUID, ge.STATUS_UUID)
            if tick % 50 == 0:
                print(" ", engine.status(), flush=True)
    finally:
        await server.stop()
        engine.close()


def main() -> None:
    """Entry point."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
