"""Tiger Data reads for the tremor monitoring prototype.

Times are stored in UTC and converted for display with ``AT TIME ZONE``.
The default zone is America/New_York and the default device id is ``tremor``.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import psycopg2
from psycopg2.extensions import connection as PgConnection

_ENV_PATH = Path(__file__).resolve().parent / ".env"
_DEFAULT_DEVICE = "tremor"
_DEFAULT_TZ = "America/New_York"
_WEEK_DAYS = 7
_LOCK = threading.RLock()
_conn: PgConnection | None = None

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None and _ENV_PATH.is_file():
    load_dotenv(_ENV_PATH, override=True)


def default_device() -> str:
    """Device id from ``TREMOR_DEVICE``, otherwise ``tremor``."""
    return os.environ.get("TREMOR_DEVICE", _DEFAULT_DEVICE).strip() or _DEFAULT_DEVICE


def default_tz() -> str:
    """Display zone from ``TREMOR_TZ``, otherwise ``America/New_York``."""
    return os.environ.get("TREMOR_TZ", _DEFAULT_TZ).strip() or _DEFAULT_TZ


def get_conn() -> PgConnection:
    """Return a live Tiger Data connection, reconnecting if the cached one dropped."""
    with _LOCK:
        return _live_unlocked()


def latest_reading(device_id: str | None = None, tz: str | None = None) -> pd.DataFrame:
    """Return the newest window: time, local_time, device_id, peak_hz, rms_g, band_ratio, tremor."""
    frame = _query(
        """
        SELECT time,
               time AT TIME ZONE %(tz)s AS local_time,
               device_id, peak_hz, rms_g, band_ratio, tremor
        FROM tremor_features
        WHERE device_id = %(device)s
        ORDER BY time DESC
        LIMIT 1
        """,
        {"tz": _tz(tz), "device": _device(device_id)},
    )
    return _floats(_times(frame), "peak_hz", "rms_g", "band_ratio")


def recent_features(
    minutes: float, device_id: str | None = None, tz: str | None = None
) -> pd.DataFrame:
    """Return windows from the last ``minutes`` minutes, oldest first."""
    frame = _query(
        """
        SELECT time, time AT TIME ZONE %(tz)s AS local_time,
               peak_hz, rms_g, band_ratio, tremor
        FROM tremor_features
        WHERE device_id = %(device)s
          AND time >= now() - (%(minutes)s * INTERVAL '1 minute')
        ORDER BY time
        """,
        {
            "tz": _tz(tz),
            "device": _device(device_id),
            "minutes": _positive_float(minutes, "minutes"),
        },
    )
    return _floats(_times(frame), "peak_hz", "rms_g", "band_ratio")


def today_timeline(device_id: str | None = None, tz: str | None = None) -> pd.DataFrame:
    """Return 1-minute averages from local midnight: local_time, avg_rms_g, avg_peak_hz, frac_tremor."""
    zone = _tz(tz)
    frame = _query(
        """
        SELECT time_bucket(INTERVAL '1 minute', time AT TIME ZONE %(tz)s) AS local_time,
               avg(rms_g)::float8 AS avg_rms_g,
               avg(peak_hz)::float8 AS avg_peak_hz,
               avg(CASE WHEN tremor THEN 1.0 ELSE 0.0 END)::float8 AS frac_tremor
        FROM tremor_features
        WHERE device_id = %(device)s
          AND time >= (date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s)
        GROUP BY 1
        ORDER BY 1
        """,
        {"tz": zone, "device": _device(device_id)},
    )
    return _floats(_times(frame), "avg_rms_g", "avg_peak_hz", "frac_tremor")


def week_heatmap(device_id: str | None = None, tz: str | None = None) -> pd.DataFrame:
    """Return 7 local days from tremor_hourly: day, hour, avg_rms_g, frac_tremor, avg_peak_hz, windows."""
    frame = _query(
        """
        SELECT (bucket AT TIME ZONE %(tz)s)::date AS day,
               EXTRACT(HOUR FROM (bucket AT TIME ZONE %(tz)s))::int AS hour,
               avg_rms_g::float8 AS avg_rms_g,
               frac_tremor::float8 AS frac_tremor,
               avg_peak_hz::float8 AS avg_peak_hz,
               windows::int AS windows
        FROM tremor_hourly
        WHERE device_id = %(device)s
          AND bucket >= (
                date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s
              ) - (%(days_back)s * INTERVAL '1 day')
        ORDER BY day, hour
        """,
        {"tz": _tz(tz), "device": _device(device_id), "days_back": _WEEK_DAYS - 1},
    )
    frame = _floats(_times(frame), "avg_rms_g", "frac_tremor", "avg_peak_hz")
    if not frame.empty:
        frame["hour"] = frame["hour"].astype(int)
        frame["windows"] = frame["windows"].astype(int)
    return frame


def doses(
    start: datetime,
    end: datetime,
    device_id: str | None = None,
    tz: str | None = None,
) -> pd.DataFrame:
    """Return doses in ``[start, end)``: time, local_time, device_id, note."""
    frame = _query(
        """
        SELECT time, time AT TIME ZONE %(tz)s AS local_time, device_id, note
        FROM med_events
        WHERE device_id = %(device)s
          AND time >= %(start)s
          AND time < %(end)s
        ORDER BY time
        """,
        {
            "tz": _tz(tz),
            "device": _device(device_id),
            "start": _aware(start, "start"),
            "end": _aware(end, "end"),
        },
    )
    return _times(frame)


def dose_response(days: int, device_id: str | None = None, tz: str | None = None) -> pd.DataFrame:
    """Average rms_g for the 2 hours before each dose and from 30 min to 2.5 h after.

    Columns: dose_time, local_time, note, before_rms_g, after_rms_g.
    """
    frame = _query(
        """
        SELECT m.time AS dose_time,
               m.time AT TIME ZONE %(tz)s AS local_time,
               m.note,
               before_side.before_rms_g,
               after_side.after_rms_g
        FROM med_events m
        LEFT JOIN LATERAL (
            SELECT avg(f.rms_g)::float8 AS before_rms_g
            FROM tremor_features f
            WHERE f.device_id = m.device_id
              AND f.time BETWEEN m.time - INTERVAL '2 hours' AND m.time
        ) AS before_side ON TRUE
        LEFT JOIN LATERAL (
            SELECT avg(f.rms_g)::float8 AS after_rms_g
            FROM tremor_features f
            WHERE f.device_id = m.device_id
              AND f.time BETWEEN m.time + INTERVAL '30 minutes'
                             AND m.time + INTERVAL '2 hours 30 minutes'
        ) AS after_side ON TRUE
        WHERE m.device_id = %(device)s
          AND m.time >= now() - (%(days)s * INTERVAL '1 day')
        ORDER BY m.time
        """,
        {"tz": _tz(tz), "device": _device(device_id), "days": _positive_int(days, "days")},
    )
    return _floats(_times(frame), "before_rms_g", "after_rms_g")


def tiger_stats() -> pd.DataFrame:
    """One row: row counts, chunk counts, and accel_raw compression before, after, and ratio.

    Row counts use approximate_row_count. Chunk counts use show_chunks. Byte
    columns and the ratio are null when accel_raw has no compression stats.
    """
    counts = _query(
        """
        SELECT approximate_row_count(CAST(%(raw)s AS regclass)) AS raw_rows,
               approximate_row_count(CAST(%(features)s AS regclass)) AS feature_rows,
               (SELECT count(*) FROM show_chunks(CAST(%(raw)s AS regclass))) AS raw_chunks,
               (SELECT count(*) FROM show_chunks(CAST(%(features)s AS regclass))) AS feature_chunks
        """,
        {"raw": "accel_raw", "features": "tremor_features"},
    )
    compressed = _query(
        """
        SELECT SUM(before_compression_total_bytes) AS before_bytes,
               SUM(after_compression_total_bytes) AS after_bytes,
               COUNT(*) AS stat_rows
        FROM hypertable_compression_stats(CAST(%(raw)s AS regclass))
        """,
        {"raw": "accel_raw"},
    )
    raw_chunks = int(counts.loc[0, "raw_chunks"] or 0)
    feature_chunks = int(counts.loc[0, "feature_chunks"] or 0)
    stat_rows = int(compressed.loc[0, "stat_rows"] or 0)
    before = _optional_int(compressed.loc[0, "before_bytes"]) if stat_rows else None
    after = _optional_int(compressed.loc[0, "after_bytes"]) if stat_rows else None
    ratio = float(before) / float(after) if before and after else None
    return pd.DataFrame(
        [
            {
                "raw_rows": int(counts.loc[0, "raw_rows"] or 0),
                "feature_rows": int(counts.loc[0, "feature_rows"] or 0),
                "raw_chunks": raw_chunks,
                "feature_chunks": feature_chunks,
                "chunk_count": raw_chunks + feature_chunks,
                "before_bytes": before,
                "after_bytes": after,
                "compression_ratio": ratio,
            }
        ]
    )


def _live_unlocked() -> PgConnection:
    """Return the cached connection, opening a new one when the ping fails."""
    global _conn
    if _conn is not None and _ping(_conn):
        return _conn
    _drop(_conn)
    url = os.environ.get("TIGER_URL", "").strip()
    if not url:
        raise RuntimeError(
            "Missing TIGER_URL. Add it to .env (see .env.example) or export it."
        )
    try:
        opened = psycopg2.connect(url, connect_timeout=15)
        opened.autocommit = True
    except psycopg2.Error as exc:
        raise RuntimeError(
            "Could not connect to Tiger Data. Check TIGER_URL "
            "(it should end in sslmode=require). (" + _redact(str(exc)) + ")"
        ) from None
    _conn = opened
    return opened


def _ping(conn: PgConnection) -> bool:
    if conn.closed:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except psycopg2.Error:
        return False


def _drop(conn: PgConnection | None) -> None:
    global _conn
    if conn is not None and _conn is conn:
        _conn = None
    if conn is None:
        return
    try:
        conn.close()
    except psycopg2.Error:
        pass


def _query(sql: str, params: Mapping[str, Any]) -> pd.DataFrame:
    """Run one parameterized read. Reconnect once if the connection drops."""
    last = "Query failed."
    for _attempt in range(2):
        with _LOCK:
            conn = _live_unlocked()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    columns = [desc[0] for desc in cur.description]
                    rows = cur.fetchall()
                return pd.DataFrame(rows, columns=columns)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                last = _redact(str(exc))
                _drop(conn)
            except psycopg2.Error as exc:
                raise RuntimeError(_redact(str(exc))) from None
    raise RuntimeError(last)


def _redact(message: str) -> str:
    """Remove the database URL, host, user, password, and API key from an error."""
    cleaned = " ".join(message.split())
    secrets: list[str] = []
    for name in ("TIGER_URL", "GEMINI_API_KEY"):
        secret = os.environ.get(name, "").strip()
        if secret:
            secrets.append(secret)
    raw_url = os.environ.get("TIGER_URL", "").strip()
    if raw_url:
        parsed = urlparse(raw_url)
        secrets.extend(part for part in (parsed.hostname, parsed.username, parsed.password) if part)
    for secret in secrets:
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def _device(device_id: str | None) -> str:
    """Use the argument, or the configured default device id."""
    chosen = default_device() if device_id is None else device_id.strip()
    if not chosen:
        raise ValueError("device_id must not be empty")
    return chosen


def _tz(tz: str | None) -> str:
    """Use the argument, or the configured zone, and reject unknown names."""
    chosen = default_tz() if tz is None else tz.strip()
    if not chosen:
        raise ValueError("tz must not be empty")
    try:
        ZoneInfo(chosen)
    except ZoneInfoNotFoundError:
        raise ValueError(f"Unknown time zone: {chosen}") from None
    return chosen


def _aware(value: datetime, name: str) -> datetime:
    """Require a timezone-aware instant."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _positive_float(value: float, name: str) -> float:
    """Reject zero, negative, and non-numeric spans."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if number <= 0 or number != number:
        raise ValueError(f"{name} must be greater than 0")
    return number


def _positive_int(value: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if number < 1:
        raise ValueError(f"{name} must be at least 1")
    return number


def _optional_int(value: object) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


def _times(frame: pd.DataFrame) -> pd.DataFrame:
    """Parse UTC instants and local wall times into datetimes."""
    for column in ("time", "dose_time"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], utc=True)
    for column in ("local_time", "day"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column])
    return frame


def _floats(frame: pd.DataFrame, *columns: str) -> pd.DataFrame:
    """Cast selected columns to float so charts do not see Decimal."""
    for column in columns:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column])
    return frame
