#!/usr/bin/env python3
"""Seed a week of simulated tremor history for the monitoring prototype."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import numpy as np
    import pandas as pd
except ImportError:
    print(
        "numpy and pandas are required. Run: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)

# Story parameters from Step 2. Strength is in g, before --scale.
WINDOW_US = 1_280_000
WAKE_START, WAKE_END = time(7, 0), time(22, 30)
DOSE_CLOCKS = (time(7, 30), time(11, 30), time(15, 30), time(19, 30))
MISSED_DAYS_AGO, MISSED_AT = 3, time(15, 30)
LATE_DAYS_AGO, LATE_AT = 5, time(11, 30)
LATE_BY = timedelta(hours=1, minutes=45)
JITTER_MIN, ONSET_LO_MIN, ONSET_HI_MIN = 15, 20.0, 30.0
FULL_AT_MIN, PLATEAU_MIN, WEAR_MIN = 45.0, 150.0, 45.0
OFF_G, DAY_SPREAD, LOW_G, RMS_NOISE_G = 0.09, 0.10, 0.02, 0.002
HZ_LO, HZ_HI, HZ_NOISE, VOLUNTARY_P = 4.5, 5.5, 0.3, 0.10
BAND_OFF, BAND_ON, BAND_MOVE = (0.6, 0.95), (0.3, 0.6), (0.1, 0.3)
TREMOR_RMS_MIN, TREMOR_BAND_MIN = 0.015, 0.5
HISTORY_LAG = timedelta(minutes=10)

FEATURE_COLUMNS = ["time", "device_id", "peak_hz", "rms_g", "band_ratio", "tremor"]
DOSE_COLUMNS = ["time", "device_id", "note"]
FEATURE_COPY = (
    "COPY tremor_features (time, device_id, peak_hz, rms_g, band_ratio, tremor) "
    "FROM STDIN WITH (FORMAT csv)"
)
DOSE_COPY = "COPY med_events (time, device_id, note) FROM STDIN WITH (FORMAT csv)"
WIPE_FEATURES = "DELETE FROM tremor_features WHERE device_id = %s"
WIPE_DOSES = "DELETE FROM med_events WHERE device_id = %s"
REFRESH_SQL = "CALL refresh_continuous_aggregate('tremor_hourly', NULL, NULL)"
ENV_PATH = Path(__file__).resolve().parent / ".env"


@dataclass(frozen=True)
class _Dose:
    """One logged dose in local time, and how long until it starts to act."""

    when: datetime
    onset_min: float
    late: bool


@dataclass
class History:
    """Simulated windows and doses. Times in the frames are UTC."""

    features: pd.DataFrame
    doses: pd.DataFrame
    device: str
    tz: str
    scale: float
    seed: int
    missed_on: date | None
    late_at: datetime | None
    last_day: date


def fail(message: str) -> NoReturn:
    """Print an error and exit. The message must not contain a secret."""
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _redact(message: str, secret: str) -> str:
    """Return one line of an error with the connection URL removed."""
    cleaned = " ".join(message.split())
    if secret:
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def _history_end(now: datetime, zone: ZoneInfo) -> datetime:
    """Last instant of data: 10 minutes ago, and only during waking hours."""
    end = now.astimezone(zone) - HISTORY_LAG
    if end.time() < WAKE_START:
        yesterday = end.date() - timedelta(days=1)
        return datetime.combine(yesterday, WAKE_END, tzinfo=zone)
    if end.time() > WAKE_END:
        return datetime.combine(end.date(), WAKE_END, tzinfo=zone)
    return end


def _window_times(start: datetime, stop: datetime) -> pd.DatetimeIndex:
    """One timestamp every 1.28 s from start through stop."""
    span_us = (stop - start) // timedelta(microseconds=1)
    if span_us < 0:
        return pd.DatetimeIndex([], tz=start.tzinfo)
    offsets = np.arange(span_us // WINDOW_US + 1, dtype=np.int64) * WINDOW_US
    offsets = offsets[offsets <= span_us]
    origin = pd.Timestamp(start)
    return pd.DatetimeIndex(origin + pd.to_timedelta(offsets, unit="us"))


def _suppression(minutes: np.ndarray, onset_min: float) -> np.ndarray:
    """Dose effect from 0 to 1. Falls after onset, holds 2.5 h, wears off over 45 min."""
    plateau_end = FULL_AT_MIN + PLATEAU_MIN
    wear_end = plateau_end + WEAR_MIN
    effect = np.zeros(minutes.shape, dtype=np.float64)

    rising = (minutes >= onset_min) & (minutes < FULL_AT_MIN)
    rise = (minutes[rising] - onset_min) / (FULL_AT_MIN - onset_min)
    effect[rising] = rise * rise * (3.0 - 2.0 * rise)

    holding = (minutes >= FULL_AT_MIN) & (minutes < plateau_end)
    effect[holding] = 1.0

    wearing = (minutes >= plateau_end) & (minutes < wear_end)
    wear = (minutes[wearing] - plateau_end) / WEAR_MIN
    effect[wearing] = 1.0 - wear * wear * (3.0 - 2.0 * wear)
    return effect


def _simulate_day(
    day: date,
    start: datetime,
    stop: datetime,
    rng: np.random.Generator,
    device: str,
    scale: float,
    missed_on: date | None,
    late_on: date | None,
    zone: ZoneInfo,
) -> tuple[pd.DataFrame, list[_Dose]]:
    """Build one local day's windows and doses. Random draws stay in a fixed order."""
    swing = float(rng.uniform(-DAY_SPREAD, DAY_SPREAD))
    off = OFF_G * (1.0 + swing)
    day_hz = float(rng.uniform(HZ_LO, HZ_HI))

    doses: list[_Dose] = []
    for clock in DOSE_CLOCKS:
        # Drawn for every slot so a planted miss or delay does not shift the seed.
        jitter = int(rng.integers(-JITTER_MIN, JITTER_MIN + 1))
        onset = float(rng.uniform(ONSET_LO_MIN, ONSET_HI_MIN))
        scheduled = datetime.combine(day, clock, tzinfo=zone)
        if missed_on is not None and day == missed_on and clock == MISSED_AT:
            continue
        if late_on is not None and day == late_on and clock == LATE_AT:
            when = scheduled + LATE_BY
            late = True
        else:
            when = scheduled + timedelta(minutes=jitter)
            late = False
        if when < start or when > stop:
            continue
        doses.append(_Dose(when=when, onset_min=onset, late=late))

    times = _window_times(start, stop)
    n = len(times)
    if n == 0:
        return pd.DataFrame(columns=FEATURE_COLUMNS), doses

    suppression = np.zeros(n, dtype=np.float64)
    for dose in doses:
        minutes = (times - pd.Timestamp(dose.when)).total_seconds().to_numpy() / 60.0
        suppression = np.maximum(suppression, _suppression(minutes, dose.onset_min))

    base = off * (1.0 - suppression) + LOW_G * suppression
    noise = rng.normal(0.0, RMS_NOISE_G, size=n)
    noise = np.clip(noise, -3.0 * RMS_NOISE_G, 3.0 * RMS_NOISE_G)
    rms = np.round(np.maximum(base + noise, LOW_G) * scale, 6)

    peak = day_hz + rng.uniform(-HZ_NOISE, HZ_NOISE, size=n)
    peak = np.round(np.clip(peak, day_hz - HZ_NOISE, day_hz + HZ_NOISE), 6)

    # Off medication: 0.6-0.95. On medication, once the effect is at least half: 0.3-0.6.
    on_med = suppression >= 0.5
    lo = np.where(on_med, BAND_ON[0], BAND_OFF[0])
    hi = np.where(on_med, BAND_ON[1], BAND_OFF[1])
    band = rng.random(n) * (hi - lo) + lo
    voluntary = rng.random(n) < VOLUNTARY_P
    n_vol = int(voluntary.sum())
    if n_vol:
        band[voluntary] = rng.uniform(BAND_MOVE[0], BAND_MOVE[1], size=n_vol)
    band = np.round(band, 6)
    rest = ~voluntary
    band[rest] = np.clip(band[rest], lo[rest], hi[rest])
    if n_vol:
        band[voluntary] = np.clip(band[voluntary], BAND_MOVE[0], BAND_MOVE[1])

    tremor = (rms >= TREMOR_RMS_MIN) & (band >= TREMOR_BAND_MIN)
    frame = pd.DataFrame(
        {
            "time": times.tz_convert("UTC"),
            "device_id": device,
            "peak_hz": peak,
            "rms_g": rms,
            "band_ratio": band,
            "tremor": tremor,
        }
    )
    return frame, doses


def _dose_frame(doses: list[_Dose], device: str) -> pd.DataFrame:
    """Turn local dose times into the med_events columns, stored in UTC."""
    if not doses:
        return pd.DataFrame(columns=DOSE_COLUMNS)
    return pd.DataFrame(
        {
            "time": [pd.Timestamp(dose.when).tz_convert("UTC") for dose in doses],
            "device_id": device,
            "note": "dose",
        }
    )


def generate_history(
    now: datetime,
    *,
    days: int = 7,
    device: str = "tremor",
    seed: int = 42,
    scale: float = 1.0,
    tz: str = "America/New_York",
) -> History:
    """Build waking-hour tremor windows and doses ending 10 minutes before ``now``.

    The result is deterministic for a given ``now``, ``seed``, ``days``, and
    ``scale``. This function does not read the environment or the database.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    if scale <= 0:
        raise ValueError("scale must be greater than 0")
    device = device.strip()
    if not device:
        raise ValueError("device must not be empty")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    zone = ZoneInfo(tz)
    end = _history_end(now, zone)
    last = end.date()
    first = last - timedelta(days=days - 1)
    missed_on = last - timedelta(days=MISSED_DAYS_AGO)
    late_on = last - timedelta(days=LATE_DAYS_AGO)
    if missed_on < first:
        missed_on = None
    if late_on < first:
        late_on = None

    rng = np.random.default_rng(seed)
    parts: list[pd.DataFrame] = []
    doses: list[_Dose] = []
    day = first
    while day <= last:
        start = datetime.combine(day, WAKE_START, tzinfo=zone)
        stop = datetime.combine(day, WAKE_END, tzinfo=zone)
        if stop > end:
            stop = end
        frame, day_doses = _simulate_day(
            day, start, stop, rng, device, scale, missed_on, late_on, zone
        )
        if not frame.empty:
            parts.append(frame)
        doses.extend(day_doses)
        day += timedelta(days=1)

    features = (
        pd.concat(parts, ignore_index=True)
        if parts
        else pd.DataFrame(columns=FEATURE_COLUMNS)
    )
    late_at = next((dose.when for dose in doses if dose.late), None)
    return History(
        features=features,
        doses=_dose_frame(doses, device),
        device=device,
        tz=tz,
        scale=scale,
        seed=seed,
        missed_on=missed_on,
        late_at=late_at,
        last_day=last,
    )


def format_summary(history: History, *, written: bool, wiped: bool) -> str:
    """Render row counts, the date range, daily strength, and the planted doses."""
    n_win = len(history.features)
    n_dose = len(history.doses)
    windows = f"{n_win:,} tremor window" + ("" if n_win == 1 else "s")
    doses = f"{n_dose:,} dose" + ("" if n_dose == 1 else "s")
    phrase = f"{windows} and {doses}"
    if not written:
        lines = ["Dry run: nothing written.", f"Would write {phrase}."]
    elif wiped:
        lines = [f"Deleted existing rows for this device, then wrote {phrase}."]
    else:
        lines = [f"Wrote {phrase}."]

    lines.append(f"Device: {history.device}")
    lines.append(f"Scale: {history.scale:g}")
    lines.append(f"Seed: {history.seed}")
    if history.features.empty:
        lines.append("Date range: no windows")
    else:
        local = history.features["time"].dt.tz_convert(history.tz)
        lines.append(
            f"Date range: {local.min():%Y-%m-%d %H:%M} to "
            f"{local.max():%Y-%m-%d %H:%M} {history.tz}"
        )
        lines.append("")
        lines.append("Average strength (g) per day:")
        daily = history.features.groupby(local.dt.date, sort=True)["rms_g"].mean()
        for day, avg in daily.items():
            lines.append(f"  {day.isoformat()}  {avg:.3f}")

    lines.append("")
    if history.missed_on is None:
        lines.append("Missed dose: outside the generated dates")
    else:
        lines.append(
            f"Missed dose: {history.missed_on.isoformat()} 15:30 "
            f"({MISSED_DAYS_AGO} days before {history.last_day.isoformat()})"
        )
    if history.late_at is None:
        lines.append("Late dose: outside the generated dates")
    else:
        lines.append(
            f"Late dose: {history.late_at:%Y-%m-%d %H:%M} "
            f"(11:30 was 1 h 45 min late; "
            f"{LATE_DAYS_AGO} days before {history.last_day.isoformat()})"
        )
    return "\n".join(lines)


def _load_env() -> None:
    """Load the project .env when python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if ENV_PATH.is_file():
        load_dotenv(ENV_PATH, override=True)


def _csv_frame(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Format a frame the way COPY CSV expects it."""
    out = frame.loc[:, columns].copy()
    out["time"] = (
        pd.to_datetime(out["time"], utc=True).dt.strftime("%Y-%m-%d %H:%M:%S+00:00")
    )
    if "tremor" in out.columns:
        flags = out["tremor"].to_numpy(dtype=bool)
        out["tremor"] = np.where(flags, "true", "false")
    return out


def _copy_table(
    conn: object,
    copy_sql: str,
    frame: pd.DataFrame,
    columns: list[str],
    wipe_sql: str,
    device: str,
    wipe: bool,
) -> None:
    """Delete this device's rows when asked, then COPY the frame, and commit."""
    if not wipe and frame.empty:
        return
    buffer = StringIO()
    if not frame.empty:
        _csv_frame(frame, columns).to_csv(
            buffer,
            index=False,
            header=False,
            columns=columns,
            float_format="%.6f",
            lineterminator="\n",
        )
        buffer.seek(0)
    try:
        with conn.cursor() as cur:
            if wipe:
                cur.execute(wipe_sql, (device,))
            if not frame.empty:
                cur.copy_expert(copy_sql, buffer)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _refresh(url: str) -> None:
    """Refresh tremor_hourly. Timescale rejects this call inside a transaction."""
    import psycopg2

    try:
        conn = psycopg2.connect(url, connect_timeout=15)
    except psycopg2.Error as exc:
        fail(
            "Rows were written, but reconnecting to refresh tremor_hourly failed. "
            f"({_redact(str(exc), url)})"
        )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(REFRESH_SQL)
    except psycopg2.Error as exc:
        fail(
            "Rows were written, but refreshing tremor_hourly failed. "
            "On an autocommit connection, run: "
            "CALL refresh_continuous_aggregate('tremor_hourly', NULL, NULL); "
            f"({_redact(str(exc), url)})"
        )
    finally:
        conn.close()


def write_history(url: str, history: History, *, wipe: bool) -> None:
    """COPY both tables, one transaction each, then refresh the hourly aggregate."""
    try:
        import psycopg2
    except ImportError:
        fail("psycopg2 is not installed. Run: pip install -r requirements.txt")

    try:
        conn = psycopg2.connect(url, connect_timeout=15)
    except psycopg2.Error as exc:
        fail(
            "Could not connect to Tiger Data. Check TIGER_URL "
            f"(it should end in sslmode=require). ({_redact(str(exc), url)})"
        )

    try:
        try:
            _copy_table(
                conn,
                FEATURE_COPY,
                history.features,
                FEATURE_COLUMNS,
                WIPE_FEATURES,
                history.device,
                wipe,
            )
        except psycopg2.Error as exc:
            detail = _redact(str(exc), url)
            hint = " Run schema.sql first." if "does not exist" in detail else ""
            fail(f"Writing tremor_features failed. ({detail}){hint}")
        try:
            _copy_table(
                conn,
                DOSE_COPY,
                history.doses,
                DOSE_COLUMNS,
                WIPE_DOSES,
                history.device,
                wipe,
            )
        except psycopg2.Error as exc:
            detail = _redact(str(exc), url)
            fail(
                "tremor_features was committed, but writing med_events failed. "
                f"Re-run with --wipe. ({detail})"
            )
    finally:
        conn.close()
    _refresh(url)


def _parse_args() -> argparse.Namespace:
    """Parse the seed flags from Step 2."""
    parser = argparse.ArgumentParser(
        description="Seed simulated tremor history for the monitoring prototype."
    )
    parser.add_argument("--days", type=int, default=7, help="local days to generate")
    parser.add_argument("--device", default=os.environ.get("TREMOR_DEVICE", "tremor"))
    parser.add_argument("--wipe", action="store_true", help="delete this device's rows first")
    parser.add_argument("--seed", type=int, default=42, help="random seed")
    parser.add_argument("--dry-run", action="store_true", help="print the summary and write nothing")
    parser.add_argument("--scale", type=float, default=1.0, help="multiply every strength; 1 for the Myo, 3 for the Pi")
    return parser.parse_args()


def main() -> None:
    """Generate the week, print the summary, and write unless this is a dry run."""
    _load_env()
    args = _parse_args()
    tz = os.environ.get("TREMOR_TZ", "America/New_York").strip() or "America/New_York"
    try:
        history = generate_history(
            datetime.now(timezone.utc),
            days=args.days,
            device=args.device,
            seed=args.seed,
            scale=args.scale,
            tz=tz,
        )
    except ZoneInfoNotFoundError:
        fail(
            f"Unknown time zone {tz}. "
            "Set TREMOR_TZ to an IANA name such as America/New_York."
        )
    except ValueError as exc:
        fail(str(exc))

    if not args.dry_run:
        url = os.environ.get("TIGER_URL", "").strip()
        if not url:
            fail("Missing TIGER_URL. Add it to .env (see .env.example) or export it.")
        write_history(url, history, wipe=args.wipe)

    print(
        format_summary(
            history,
            written=not args.dry_run,
            wiped=args.wipe and not args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
