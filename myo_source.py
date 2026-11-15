#!/usr/bin/env python3
"""Myo armband source for the monitoring prototype.

Streams motion and muscle samples over the Mac's Bluetooth using dl-myo.
No USB dongle. On Python 3.13, install bleak==0.22.3 first, then
``pip install --no-deps dl-myo==1.1.8`` (dl-myo pins an older bleak with no
macOS wheel for this Python).
"""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from bleak import BleakClient, BleakError
from myo import EMGData, IMUData, Myo, MyoClient
from myo.constants import (
    ACCELEROMETER_SCALE,
    DEFAULT_IMU_SAMPLE_RATE,
    EMG_DEFAULT_STREAMING_RATE,
)
from myo.types import (
    ClassifierEvent,
    ClassifierMode,
    EMGDataSingle,
    EMGMode,
    FVData,
    IMUMode,
    MotionEvent,
    SleepMode,
    VibrationType,
)

# Thalmic myohw.h: MYOHW_ACCELEROMETER_SCALE is 2048. g = raw / 2048.
# dl-myo applies that in IMUData (raw int16 / ACCELEROMETER_SCALE).
_EXPECTED_ACCELEROMETER_SCALE = 2048.0
# Re-anchor the sample clock when it and the laptop clock disagree by this much.
MAX_CLOCK_DRIFT_S = 0.5

EmgSample = tuple[int, int, int, int, int, int, int, int]
MotionCallback = Callable[[datetime, float, float, float], None]
EmgCallback = Callable[[datetime, EmgSample], None]

_VIBRATIONS = {
    "short": VibrationType.SHORT,
    "long": VibrationType.LONG,
}


class MyoNotFoundError(Exception):
    """No Myo armband answered a Bluetooth scan."""


def _require_accelerometer_scale() -> None:
    """Stop if dl-myo is not using Thalmic's raw-to-g scale."""
    if ACCELEROMETER_SCALE != _EXPECTED_ACCELEROMETER_SCALE:
        raise RuntimeError(
            "dl-myo ACCELEROMETER_SCALE is "
            f"{ACCELEROMETER_SCALE}, not {_EXPECTED_ACCELEROMETER_SCALE:g} "
            "(MYOHW_ACCELEROMETER_SCALE in myohw.h)."
        )


class _SampleClock:
    """Stamp samples at a fixed rate, then re-sync if the laptop clock drifts.

    Bluetooth delivers samples in bursts, so arrival time is not sample time.
    Sample ``n`` is ``origin + n / rate_hz``. If that time and the laptop's
    UTC clock are more than ``max_drift_s`` apart, the origin jumps to now.
    """

    def __init__(self, rate_hz: float, max_drift_s: float = MAX_CLOCK_DRIFT_S) -> None:
        """Count samples at ``rate_hz`` and allow ``max_drift_s`` of clock error."""
        self.rate_hz = rate_hz
        self.max_drift_s = max_drift_s
        self._origin: datetime | None = None
        self._count = 0

    def stamp(self, now: datetime) -> datetime:
        """Return the UTC timestamp for the next sample."""
        if self._origin is None:
            self._origin = now
            self._count = 0
        scheduled = self._origin + timedelta(seconds=self._count / self.rate_hz)
        drift_s = (now - scheduled).total_seconds()
        if abs(drift_s) > self.max_drift_s:
            self._origin = now
            self._count = 0
            scheduled = now
        self._count += 1
        return scheduled


class _Band(MyoClient):
    """dl-myo client that forwards IMU and raw EMG to callbacks."""

    def __init__(self, on_motion: MotionCallback | None, on_emg: EmgCallback | None) -> None:
        """Keep raw EMG and IMU separate and forward each sample to callbacks."""
        super().__init__(aggregate_all=False, aggregate_emg=False)
        self._on_motion = on_motion
        self._on_emg = on_emg
        self._motion_clock = _SampleClock(float(DEFAULT_IMU_SAMPLE_RATE))
        self._emg_clock = _SampleClock(float(EMG_DEFAULT_STREAMING_RATE))
        self._dropped = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        """Connect and notice if the band drops the link."""
        if self.m is None:
            raise ValueError("Device not discovered.")
        self._loop = asyncio.get_running_loop()
        self._dropped = asyncio.Event()
        self._client = BleakClient(self.device, disconnected_callback=self._on_disconnect)
        await self._client.connect()

    def _on_disconnect(self, _client: BleakClient) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._dropped.set)

    def streaming(self) -> bool:
        """True when the BLE link is up."""
        client = self._client
        return client is not None and bool(client.is_connected)

    async def enable_stream(self) -> None:
        """Never sleep, then turn on raw EMG and IMU data notifications."""
        await self.set_sleep_mode(SleepMode.NEVER_SLEEP)
        self.classifier_mode = ClassifierMode.DISABLED
        self.emg_mode = EMGMode.SEND_RAW
        self.imu_mode = IMUMode.SEND_DATA
        await self.set_mode(
            classifier_mode=self.classifier_mode,
            emg_mode=self.emg_mode,
            imu_mode=self.imu_mode,
        )
        for handle in self._get_emg_handles() + self._get_imu_handles():
            await self.start_notify(handle, self.notify_callback)

    async def wait_until_dropped(self, stop: threading.Event) -> None:
        """Return when the link drops or ``stop`` is set."""
        while not self._dropped.is_set() and not stop.is_set():
            try:
                await asyncio.wait_for(self._dropped.wait(), timeout=0.25)
            except TimeoutError:
                client = self._client
                if client is not None and not client.is_connected:
                    return

    async def on_imu_data(self, imu: IMUData) -> None:
        """Deliver one motion sample. Acceleration is already in g."""
        # IMUData.accelerometer is raw int16 / ACCELEROMETER_SCALE (2048).
        ax, ay, az = (float(axis) for axis in imu.accelerometer)
        when = self._motion_clock.stamp(_utc_now())
        self._emit_motion(when, ax, ay, az)

    async def on_emg_data(self, emg: EMGData) -> None:
        """Deliver both 8-channel samples carried in one EMG notification."""
        now = _utc_now()
        for sample in (emg.sample1, emg.sample2):
            when = self._emg_clock.stamp(now)
            self._emit_emg(when, _emg_sample(sample))

    async def on_emg_data_aggregated(self, eds: EMGDataSingle) -> None:
        """Required by MyoClient. Raw mode uses on_emg_data instead."""
        return None

    async def on_fv_data(self, fvd: FVData) -> None:
        """Required by MyoClient. Filtered EMG notifications are not requested."""
        return None

    async def on_aggregated_data(self, ad: object) -> None:
        """Required by MyoClient. FV and IMU stay on their own callbacks."""
        return None

    async def on_classifier_event(self, ce: ClassifierEvent) -> None:
        """Required by MyoClient. The on-device pose classifier stays off."""
        return None

    async def on_motion_event(self, me: MotionEvent) -> None:
        """Required by MyoClient. IMU tap events are not requested."""
        return None

    def _emit_motion(self, when: datetime, ax: float, ay: float, az: float) -> None:
        if self._on_motion is None:
            return
        try:
            self._on_motion(when, ax, ay, az)
        except Exception as exc:
            print(f"Motion callback failed: {exc}", file=sys.stderr)

    def _emit_emg(self, when: datetime, sample: EmgSample) -> None:
        if self._on_emg is None:
            return
        try:
            self._on_emg(when, sample)
        except Exception as exc:
            print(f"Muscle callback failed: {exc}", file=sys.stderr)


class MyoSource:
    """Background Myo stream with start, stop, and vibrate.

    Motion callback: ``(time, ax, ay, az)`` in g. ``time`` is an aware UTC
    datetime. Muscle callback: ``(time, emg)`` with 8 raw channel values.
    Times are counted at 50 Hz (IMU) and 200 Hz (EMG) from the first sample
    of each connection, and re-synced to the laptop clock past 0.5 s of drift.

    If the band drops after the first connection, this scans again and prints
    a reconnect message. ``start`` raises ``MyoNotFoundError`` when the first
    scan finds nothing.
    """

    def __init__(
        self,
        on_motion: MotionCallback | None = None,
        on_emg: EmgCallback | None = None,
        address: str | None = None,
    ) -> None:
        """Store callbacks. ``address`` is an optional Bluetooth address."""
        _require_accelerometer_scale()
        self._on_motion = on_motion
        self._on_emg = on_emg
        self._address = address
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._band: _Band | None = None
        self._connections = 0

    def start(self, timeout: float = 25.0) -> None:
        """Scan, connect, and stream. Discovery itself takes about 10 seconds.

        Raises:
            MyoNotFoundError: the first scan did not see a Myo.
            RuntimeError: start was called while a session is already running.
            TimeoutError: the band did not start streaming in ``timeout`` seconds.
        """
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("MyoSource is already running. Call stop() first.")
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self._connections = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._thread_main, name="myo", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise TimeoutError("Timed out waiting for the Myo to start streaming.")
        if self._error is not None:
            error = self._error
            self.stop()
            raise error

    def stop(self) -> None:
        """Stop streaming, disconnect, and wait for the background thread."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=15)
        self._thread = None
        self._loop = None
        self._band = None

    def vibrate(self, kind: str) -> None:
        """Buzz the armband. ``kind`` is ``"short"`` or ``"long"``."""
        try:
            vibration = _VIBRATIONS[kind]
        except KeyError:
            raise ValueError('kind must be "short" or "long"') from None
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("Myo is not connected.")
        future = asyncio.run_coroutine_threadsafe(self._vibrate(vibration), loop)
        try:
            future.result(timeout=5)
        except Exception as exc:
            raise RuntimeError(f"Could not vibrate the Myo: {exc}") from exc

    async def _vibrate(self, vibration: VibrationType) -> None:
        band = self._band
        if band is None or not band.streaming():
            raise RuntimeError("Myo is not connected.")
        await band.vibrate(vibration)

    def _thread_main(self) -> None:
        loop = self._loop
        if loop is None:
            return
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._session_loop())
        finally:
            if not loop.is_closed():
                loop.close()

    async def _session_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self._connect_and_stream()
                except MyoNotFoundError as exc:
                    if self._connections == 0:
                        self._error = exc
                        return
                    print("No Myo armband found. Reconnecting...")
                    await self._pause(2.0)
                    continue
                except Exception as exc:
                    if self._connections == 0:
                        self._error = exc
                        return
                    print(f"Myo connection failed ({exc}). Reconnecting...")
                    await self._pause(2.0)
                    continue
                if self._stop.is_set():
                    break
                print("Myo band dropped. Reconnecting...")
                await self._pause(1.0)
        finally:
            await self._close_band()
            self._ready.set()

    async def _connect_and_stream(self) -> None:
        if self._stop.is_set():
            return
        band = _Band(self._on_motion, self._on_emg)
        band.m = await self._discover()
        if band.m is None:
            raise MyoNotFoundError("No Myo armband found.")
        if self._stop.is_set():
            return
        self._band = band
        try:
            await band.connect()
            await band.enable_stream()
            self._connections += 1
            name = band.device.name or "Myo"
            address = band.device.address
            if self._connections == 1:
                print(
                    f"Connected to {name} ({address}). "
                    "Raw EMG (~200 Hz) and IMU (~50 Hz) are on. Sleep mode is never."
                )
            else:
                print(f"Reconnected to {name} ({address}). Streaming again.")
            self._ready.set()
            await band.wait_until_dropped(self._stop)
        finally:
            await self._close_band()

    async def _discover(self) -> Myo | None:
        """Scan for the band. Retry when CoreBluetooth is still powering on."""
        last_error: BleakError | None = None
        for _attempt in range(3):
            try:
                if self._address:
                    return await Myo.with_mac(self._address)
                return await Myo.with_uuid()
            except BleakError as exc:
                if "turned off" not in str(exc).lower():
                    raise
                last_error = exc
                await self._pause(1.0)
        if last_error is not None:
            raise last_error
        return None

    async def _close_band(self) -> None:
        band = self._band
        self._band = None
        if band is None:
            return
        try:
            await band.disconnect()
        except Exception:
            band._client = None

    async def _pause(self, seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while not self._stop.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _emg_sample(sample: tuple[int, ...]) -> EmgSample:
    if len(sample) != 8:
        raise ValueError(f"EMG sample has {len(sample)} channels, expected 8.")
    c0, c1, c2, c3, c4, c5, c6, c7 = (int(channel) for channel in sample)
    return (c0, c1, c2, c3, c4, c5, c6, c7)


_require_accelerometer_scale()
