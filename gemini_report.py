#!/usr/bin/env python3
"""One-page doctor-visit summary for the tremor monitoring prototype.

Reads the last few days through db.py and asks Gemini to describe patterns.
It does not diagnose and does not suggest medication changes.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
from psycopg2.extensions import connection as PgConnection

import db

# google-genai HttpOptions.timeout is milliseconds, not seconds.
_TIMEOUT_MS = 60_000
_TEMPERATURE = 0.3
_DEFAULT_MODEL = "gemini-3.8-flash"
_FALLBACK_MODELS = ("gemini-3.7-flash", "gemini-2.5-flash")
_REPORT_PATH = Path("report.md")

_SYSTEM = """
You write for the person who wears the wristband and for their family.
This is a monitoring prototype. Say that in the summary.

Describe patterns only. No diagnosis, and no advice on medication or doses.
Write "the data suggests", never "you have".

Write at about an 8th-grade reading level.
No code formatting, backticks, or variable names.
Use percentages, times, and rounded numbers. For example: "tremor dropped by about three quarters within an hour of the dose."
Say "tremor strength", "share of the hour with tremor", and "tremor speed (shakes per second)".
Do not mention grams, g, or Hz.

Keep the whole answer under 300 words.

Use these headings, in this order, and do not add a title or a table:
## Summary
Three sentences.
## Daily pattern
## Medication timing
Mention any missed or late dose from the notes. Do not suggest what to do about it.
## Notable events
## Questions to ask your doctor
Three to five questions. Questions only, with no advice.

Use only the facts in the message. Do not invent measurements, doses, or days.
""".strip()

_LATE_MINUTES = 45
_COLUMN_WORDS = (
    ("avg_rms_g", "tremor strength"),
    ("frac_tremor", "share of the hour with tremor"),
    ("avg_peak_hz", "tremor speed"),
    ("band_ratio", "the pattern"),
    ("peak_hz", "tremor speed"),
    ("rms_g", "tremor strength"),
)


def build_report(
    conn: PgConnection,
    device: str,
    days: int,
    tz: str | None = None,
) -> str:
    """Return a Markdown visit summary for ``device`` over the last ``days`` days.

    ``conn`` is the open Tiger Data connection from ``db.get_conn()``. The
    hourly table, dose times, and before/after rows are read with db.py.
    Gemini is called with the guardrail system instruction. If ``GEMINI_MODEL``
    is not found, the call tries gemini-3.7-flash and then gemini-2.5-flash.
    """
    _require_open(conn)
    chosen_days = _days(days)
    chosen_device = _device(device)
    zone = tz.strip() if isinstance(tz, str) and tz.strip() else db.default_tz()
    notes, average_drop, compared = _collect(chosen_device, chosen_days, zone)
    if not notes:
        start, end = _requested_span(chosen_days, zone)
        return (
            f"# {_title(start, end)}\n\n"
            "There are no tremor readings or logged doses in this window. "
            "This is a monitoring prototype.\n"
        )
    narrative = _clean_narrative(_generate(_prompt(notes, average_drop, compared)))
    return f"# {_title(notes[0].day, notes[-1].day)}\n\n{narrative}\n\n{_clinician_table(notes)}\n"


def main() -> None:
    """Write report.md for the last --days days and print the Markdown."""
    parser = argparse.ArgumentParser(
        description="Doctor-visit summary from tremor history. Monitoring prototype."
    )
    parser.add_argument("--days", type=int, default=7, help="how many days to include")
    parser.add_argument(
        "--device",
        default=db.default_device(),
        help="device id (default: TREMOR_DEVICE or tremor)",
    )
    args = parser.parse_args()
    try:
        report = build_report(db.get_conn(), args.device, args.days)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    text = report if report.endswith("\n") else report + "\n"
    _REPORT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {_REPORT_PATH.resolve()}", file=sys.stderr)
    print(report, flush=True)


@dataclass(frozen=True)
class _Gap:
    """A dose that was missing, or logged well off the usual time."""

    kind: str
    usual: str
    logged: str
    lag: str


@dataclass(frozen=True)
class _DayNotes:
    """Plain-language facts for one local day, plus the clinician numbers."""

    day: date
    has_hours: bool
    high_hours: tuple[int, ...]
    moderate_hours: tuple[int, ...]
    low_hours: tuple[int, ...]
    strong_share: int | None
    quiet_share: int | None
    speed_low: int | None
    speed_high: int | None
    dose_phrases: tuple[str, ...]
    gaps: tuple[_Gap, ...]
    doses_logged: str
    avg_strength: float | None
    avg_hz: float | None


def _collect(
    device: str, days: int, tz: str
) -> tuple[list[_DayNotes], float | None, int]:
    """Read db.py and turn the window into per-day notes. No raw column values."""
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    hourly = _prepare_hourly(_slice_days(db.week_heatmap(device, tz), days))
    doses = db.doses(start, end, device, tz)
    percents = _percents(db.dose_response(days, device, tz))
    dose_rows = _dose_rows(doses, percents)
    if not hourly.empty:
        keep = set(hourly["day"])
        dose_rows = [row for row in dose_rows if row["day"] in keep]
        percents = {stamp: pct for stamp, pct in percents.items() if stamp.date() in keep}
    by_day: dict[date, list[dict[str, object]]] = {}
    for row in dose_rows:
        day = row["day"]
        if isinstance(day, date):
            by_day.setdefault(day, []).append(row)
    minute_lists = {
        day: [_minutes(row["local"]) for row in rows] for day, rows in by_day.items()
    }
    slots = _slots(minute_lists)
    cuts = _week_cuts(hourly)
    if hourly.empty:
        dates = sorted(by_day)
    else:
        dates = sorted(set(hourly["day"]) | set(by_day))
    notes = [
        _day_notes(day, _hours_for(hourly, day), by_day.get(day, []), slots, cuts)
        for day in dates
    ]
    changes = [pct for pct in percents.values() if pct is not None]
    average_drop = -sum(changes) / len(changes) if changes else None
    return notes, average_drop, len(changes)


def _prompt(notes: list[_DayNotes], average_drop: float | None, compared: int) -> str:
    """Patient-facing notes. Labels only: no column names and no raw gram values."""
    lines = [
        "These notes are from a monitoring prototype that measures rest tremor at the wrist.",
        "Times are local.",
        "Tremor strength is low, moderate, or high compared with this person's own week: "
        "low is the bottom third, moderate is the middle third, and high is the top third.",
        "A strong hour is a high hour.",
        "Share of the hour with tremor is how much of that hour the shaking continued.",
        "Tremor speed (shakes per second) is how fast the shaking was.",
        _average_line(average_drop, compared),
        "",
    ]
    for note in notes:
        lines.append(_day_text(note))
        lines.append("")
    lines.append(
        "Write the summary from these notes only. Do not list every hour. "
        "Describe the shape of the day in a sentence or two."
    )
    return "\n".join(lines).strip()


def _day_text(note: _DayNotes) -> str:
    """One day's notes in sentences a patient can follow."""
    lines = [f"{note.day.strftime('%A')}, {note.day.strftime('%B')} {note.day.day}"]
    if note.has_hours:
        if note.high_hours:
            clocks = ", ".join(_hour_name(hour) for hour in note.high_hours)
            lines.append(
                f"Hours of strong tremor: {len(note.high_hours)}. "
                f"High tremor strength: {clocks}."
            )
        else:
            lines.append("Hours of strong tremor: none.")
        for label, hours in (("Moderate", note.moderate_hours), ("Low", note.low_hours)):
            if hours:
                clocks = ", ".join(_hour_name(hour) for hour in hours)
                lines.append(f"{label} tremor strength: {clocks}.")
        speed = _speed_phrase(note.speed_low, note.speed_high)
        if speed:
            lines.append(speed)
        if note.strong_share is not None:
            lines.append(
                "Share of the hour with tremor during strong hours was about "
                f"{note.strong_share}%."
            )
        if note.quiet_share is not None:
            lines.append(
                "Share of the hour with tremor during low hours was about "
                f"{note.quiet_share}%."
            )
    else:
        lines.append("No hourly readings that day.")
    if note.dose_phrases:
        lines.append("Logged doses: " + " ".join(note.dose_phrases))
    else:
        lines.append("No doses were logged.")
    lines.extend(_gap_sentence(gap) for gap in note.gaps)
    return "\n".join(lines)


def _clinician_table(notes: list[_DayNotes]) -> str:
    """Markdown table of one row per day. Built here, not by the model."""
    header = (
        "## For your clinician\n\n"
        "| Day | Doses logged | Missed or late doses | Average strength (g) | "
        "Average tremor frequency (Hz) |\n"
        "| --- | --- | --- | --- | --- |"
    )
    rows = []
    for note in notes:
        cells = (
            f"{note.day.strftime('%b')} {note.day.day}",
            note.doses_logged,
            _gap_cell(note.gaps),
            _measure(note.avg_strength, 3),
            _measure(note.avg_hz, 2),
        )
        rows.append("| " + " | ".join(cells) + " |")
    return header + "\n" + "\n".join(rows)


def _generate(prompt: str) -> str:
    """Call Gemini, walking the model list only when a model is not found."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Missing GEMINI_API_KEY. Add it to .env (see .env.example) or export it."
        )
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise RuntimeError(
            "google-genai is not installed. Run: pip install -r requirements.txt"
        ) from None

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=_TIMEOUT_MS),
    )
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM,
        temperature=_TEMPERATURE,
    )
    tried: list[str] = []
    for model in _model_order():
        tried.append(model)
        try:
            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )
        except Exception as exc:
            if _model_missing(exc):
                continue
            raise RuntimeError(_friendly(exc, api_key, model)) from None
        text = (getattr(response, "text", None) or "").strip()
        if text:
            return text
        raise RuntimeError(f"Gemini ({model}) returned an empty report. Try again.")
    names = ", ".join(tried)
    raise RuntimeError(
        f"None of the Gemini models were found ({names}). Check GEMINI_MODEL."
    )


def _model_order() -> list[str]:
    """GEMINI_MODEL first, then the two fallbacks, with duplicates removed."""
    primary = os.environ.get("GEMINI_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    order: list[str] = []
    for name in (primary, *_FALLBACK_MODELS):
        if name not in order:
            order.append(name)
    return order


def _model_missing(exc: BaseException) -> bool:
    """True when Gemini rejected the model name, so the next fallback can run."""
    try:
        code = int(getattr(exc, "code", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    status = str(getattr(exc, "status", "") or "").upper()
    text = str(exc).lower()
    if code == 404 or status == "NOT_FOUND":
        return True
    if "not found" in text and "model" in text:
        return True
    if code == 400 and any(
        phrase in text
        for phrase in ("not found", "not supported", "invalid model", "does not exist")
    ):
        return True
    return False


def _friendly(exc: BaseException, api_key: str, model: str) -> str:
    """A short error for the CLI and the dashboard, with secrets removed."""
    raw = _redact(str(exc), api_key)
    lowered = raw.lower()
    try:
        code = int(getattr(exc, "code", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 429 or "resource_exhausted" in lowered or "quota" in lowered:
        return "Gemini's quota or rate limit was reached. Wait a minute and try again."
    if code == 503 or "high demand" in lowered or "unavailable" in lowered:
        return f"Gemini ({model}) is busy right now. Try again in a moment."
    if (
        code == 504
        or "timeout" in lowered
        or "timed out" in lowered
        or "deadline" in lowered
    ):
        return f"The Gemini request to {model} timed out. Try again in a moment."
    if code in (401, 403) or "api key" in lowered or "unauthenticated" in lowered:
        return "Gemini rejected the API key. Check GEMINI_API_KEY in .env."
    return f"Could not write the report. ({raw})"


def _redact(message: str, api_key: str) -> str:
    """One line of an error with the API key and database URL removed."""
    cleaned = " ".join(message.split())
    secrets = [
        api_key,
        os.environ.get("TIGER_URL", "").strip(),
        os.environ.get("TIGER_READER_URL", "").strip(),
        os.environ.get("TIGER_WRITER_URL", "").strip(),
        os.environ.get("GEMINI_API_KEY", "").strip(),
    ]
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    if len(cleaned) > 300:
        cleaned = cleaned[:300] + "..."
    return cleaned


def _require_open(conn: PgConnection) -> None:
    """Raise when the caller's connection cannot run a read."""
    if conn is None or conn.closed:
        raise RuntimeError(
            "The database connection is closed. Check the database URL in .env and try again."
        )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except psycopg2.Error:
        raise RuntimeError(
            "The database connection dropped. Check the database URL in .env and try again."
        ) from None


def _slice_days(frame: pd.DataFrame, days: int) -> pd.DataFrame:
    """Keep the last ``days`` local dates present in the hourly rollup."""
    if frame.empty:
        return frame
    local_days = pd.to_datetime(frame["day"]).dt.normalize()
    cutoff = local_days.max() - pd.Timedelta(days=days - 1)
    return frame.loc[local_days >= cutoff].copy()


def _prepare_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Copy the rollup with a local date and plain numeric columns."""
    if frame.empty:
        return frame
    out = frame.copy()
    out["day"] = pd.to_datetime(out["day"]).dt.date
    out["hour"] = out["hour"].astype(int)
    out["strength"] = pd.to_numeric(out["avg_rms_g"])
    out["share"] = pd.to_numeric(out["frac_tremor"])
    out["speed"] = pd.to_numeric(out["avg_peak_hz"])
    return out


def _percents(frame: pd.DataFrame) -> dict[pd.Timestamp, float | None]:
    """Percent change in tremor strength, after versus before, keyed by minute."""
    found: dict[pd.Timestamp, float | None] = {}
    if frame.empty:
        return found
    for row in frame.itertuples(index=False):
        stamp = pd.Timestamp(row.local_time).floor("min")
        before, after = row.before_rms_g, row.after_rms_g
        if pd.isna(before) or pd.isna(after) or float(before) <= 0:
            found[stamp] = None
        else:
            found[stamp] = (float(after) - float(before)) / float(before) * 100.0
    return found


def _dose_rows(frame: pd.DataFrame, percents: dict[pd.Timestamp, float | None]) -> list[dict[str, object]]:
    """Logged doses with a clock time and the percent change, oldest first."""
    if frame.empty:
        return []
    ordered = frame.copy()
    ordered["local_time"] = pd.to_datetime(ordered["local_time"])
    ordered = ordered.sort_values("local_time")
    rows: list[dict[str, object]] = []
    for row in ordered.itertuples(index=False):
        local = pd.Timestamp(row.local_time)
        rows.append(
            {
                "day": local.date(),
                "local": local,
                "percent": percents.get(local.floor("min")),
            }
        )
    return rows


def _slots(by_day: dict[date, list[int]]) -> list[int]:
    """Usual dose times, in minutes from midnight, from days with the usual count."""
    counts = [len(minutes) for minutes in by_day.values() if minutes]
    typical = _typical_count(counts)
    complete = [sorted(minutes) for minutes in by_day.values() if len(minutes) == typical]
    if typical < 1 or len(complete) < 2:
        return []
    medians: list[int] = []
    for index in range(typical):
        sample = [day[index] for day in complete]
        medians.append(int(round(float(pd.Series(sample).median()))))
    return medians


def _typical_count(counts: list[int]) -> int:
    """Most common doses-per-day, preferring the higher number when tied."""
    if not counts:
        return 0
    tally: dict[int, int] = {}
    for count in counts:
        tally[count] = tally.get(count, 0) + 1
    return max(tally, key=lambda number: (tally[number], number))


def _assign(dose_minutes: list[int], slots: list[int]) -> tuple[dict[int, int], list[int]]:
    """Match each dose to a distinct usual time, and list usual times with no dose."""
    if not slots or not dose_minutes:
        return {}, list(range(len(slots)))
    best: tuple[int, list[tuple[int, int]]] | None = None
    if len(dose_minutes) <= len(slots):
        groups = permutations(range(len(slots)), len(dose_minutes))
        for chosen in groups:
            pairs = [(index, slot) for index, slot in enumerate(chosen)]
            cost = sum(abs(dose_minutes[index] - slots[slot]) for index, slot in pairs)
            if best is None or cost < best[0]:
                best = (cost, pairs)
    else:
        for chosen in permutations(range(len(dose_minutes)), len(slots)):
            pairs = [(dose_index, slot) for slot, dose_index in enumerate(chosen)]
            cost = sum(abs(dose_minutes[index] - slots[slot]) for index, slot in pairs)
            if best is None or cost < best[0]:
                best = (cost, pairs)
    mapping = dict(best[1]) if best else {}
    missed = [slot for slot in range(len(slots)) if slot not in mapping.values()]
    return mapping, missed


def _day_notes(
    day: date,
    hours: pd.DataFrame,
    doses: list[dict[str, object]],
    slots: list[int],
    cuts: tuple[float, float] | None,
) -> _DayNotes:
    """Labels, percents, and gaps for one day."""
    labels = _labeled_hours(hours, cuts)
    high = tuple(hour for hour, label in labels if label == "high")
    moderate = tuple(hour for hour, label in labels if label == "moderate")
    low = tuple(hour for hour, label in labels if label == "low")
    dose_minutes = [_minutes(row["local"]) for row in doses]
    mapping, missed_slots = _assign(dose_minutes, slots)
    gaps = _gaps(doses, dose_minutes, slots, mapping, missed_slots)
    phrases = tuple(
        _dose_phrase(_clock(pd.Timestamp(row["local"])), row["percent"])  # type: ignore[arg-type]
        for row in doses
    )
    clocks = [_clock(pd.Timestamp(row["local"])) for row in doses]
    return _DayNotes(
        day=day,
        has_hours=not hours.empty,
        high_hours=high,
        moderate_hours=moderate,
        low_hours=low,
        strong_share=_share(hours, labels, "high"),
        quiet_share=_share(hours, labels, "low"),
        speed_low=_speed_bound(hours, low=True),
        speed_high=_speed_bound(hours, low=False),
        dose_phrases=phrases,
        gaps=tuple(gaps),
        doses_logged=", ".join(clocks) if clocks else "None",
        avg_strength=_mean(hours, "strength") if not hours.empty else None,
        avg_hz=_mean(hours, "speed") if not hours.empty else None,
    )


def _labeled_hours(
    hours: pd.DataFrame, cuts: tuple[float, float] | None
) -> list[tuple[int, str]]:
    """Mark each hour low, moderate, or high using the week's thirds."""
    if hours.empty or cuts is None:
        return []
    low_cut, high_cut = cuts
    labeled = []
    for row in hours.sort_values("hour").itertuples(index=False):
        if pd.isna(row.strength):
            continue
        labeled.append((int(row.hour), _band(float(row.strength), low_cut, high_cut)))
    return labeled


def _week_cuts(hourly: pd.DataFrame) -> tuple[float, float] | None:
    """Bottom and top third cut points for this person's own week."""
    if hourly.empty:
        return None
    strength = hourly["strength"].dropna()
    if strength.empty:
        return None
    return float(strength.quantile(1 / 3)), float(strength.quantile(2 / 3))


def _hours_for(hourly: pd.DataFrame, day: date) -> pd.DataFrame:
    """Rows for one local date, or an empty frame when that day has none."""
    if hourly.empty:
        return hourly
    return hourly[hourly["day"] == day]


def _gaps(
    doses: list[dict[str, object]],
    dose_minutes: list[int],
    slots: list[int],
    mapping: dict[int, int],
    missed_slots: list[int],
) -> list[_Gap]:
    """Missed usual times, and doses logged at least 45 minutes off that time."""
    gaps = [_Gap("missed", _clock_minutes(slots[slot]), "", "") for slot in missed_slots]
    for index, slot in sorted(mapping.items()):
        delta = dose_minutes[index] - slots[slot]
        if abs(delta) < _LATE_MINUTES:
            continue
        logged = _clock(pd.Timestamp(doses[index]["local"]))  # type: ignore[arg-type]
        kind = "late" if delta > 0 else "early"
        gaps.append(_Gap(kind, _clock_minutes(slots[slot]), logged, _lag(abs(delta))))
    return gaps


def _dose_phrase(clock: str, percent: object) -> str:
    """One logged dose and its percent change in tremor strength."""
    if percent is None or pd.isna(percent):
        return f"{clock}: no before-and-after comparison."
    rounded = int(round(float(percent)))  # type: ignore[arg-type]
    if rounded < 0:
        return f"{clock}: tremor strength fell by about {abs(rounded)}%."
    if rounded > 0:
        return f"{clock}: tremor strength rose by about {rounded}%."
    return f"{clock}: tremor strength stayed about the same."


def _share(hours: pd.DataFrame, labels: list[tuple[int, str]], band: str) -> int | None:
    """Median share of the hour with tremor, as a whole percent, for one band."""
    if hours.empty:
        return None
    wanted = {hour for hour, label in labels if label == band}
    if not wanted:
        return None
    chosen = hours[hours["hour"].isin(wanted)]["share"].dropna()
    if chosen.empty:
        return None
    return int(round(float(chosen.median()) * 100))


def _speed_bound(hours: pd.DataFrame, *, low: bool) -> int | None:
    """Slowest or fastest tremor speed that day, in whole shakes per second."""
    if hours.empty:
        return None
    speed = hours["speed"].dropna()
    if speed.empty:
        return None
    value = float(speed.min() if low else speed.max())
    return int(round(value))


def _mean(hours: pd.DataFrame, column: str) -> float | None:
    """Average of one numeric column, or None when the day has no values."""
    series = hours[column].dropna()
    if series.empty:
        return None
    return float(series.mean())


def _band(value: float, low_cut: float, high_cut: float) -> str:
    """Bottom third is low, middle third is moderate, top third is high."""
    if not low_cut < high_cut:
        return "moderate"
    if value <= low_cut:
        return "low"
    if value <= high_cut:
        return "moderate"
    return "high"


def _gap_sentence(gap: _Gap) -> str:
    """Plain sentence for a missed, late, or early dose."""
    if gap.kind == "missed":
        return f"No dose was logged around {gap.usual}."
    later = "later" if gap.kind == "late" else "earlier"
    return (
        f"The dose usually logged around {gap.usual} was logged at {gap.logged}, "
        f"about {gap.lag} {later}."
    )


def _gap_cell(gaps: tuple[_Gap, ...]) -> str:
    """Compact clinician-table cell for missed or late doses."""
    if not gaps:
        return "None"
    parts: list[str] = []
    for gap in gaps:
        if gap.kind == "missed":
            parts.append(f"Missed {gap.usual}")
        elif gap.kind == "late":
            parts.append(f"{gap.logged}, about {gap.lag} late")
        else:
            parts.append(f"{gap.logged}, about {gap.lag} early")
    return "; ".join(parts)


def _average_line(drop: float | None, count: int) -> str:
    """The average percent drop, already calculated, in one sentence."""
    if drop is None or count < 1:
        return "There is no before-and-after comparison for the logged doses."
    rounded = int(round(drop))
    if rounded > 0:
        return f"Average drop in tremor strength across {count} doses: about {rounded}%."
    if rounded < 0:
        return (
            f"Average change in tremor strength across {count} doses: "
            f"rose by about {abs(rounded)}%."
        )
    return f"Average change in tremor strength across {count} doses: about the same."


def _speed_phrase(low: int | None, high: int | None) -> str | None:
    """Tremor speed for the day, in whole shakes per second."""
    if low is None:
        return None
    if high is None or high == low:
        return f"Tremor speed (shakes per second) was about {low}."
    return f"Tremor speed (shakes per second) was about {low} to {high}."


def _measure(value: float | None, digits: int) -> str:
    """A clinician number, or n/a when that day has no readings."""
    if value is None or pd.isna(value):
        return "n/a"
    return f"{value:.{digits}f}"


def _title(first: date, last: date) -> str:
    """Report title with the local date range and no device id."""
    if first == last:
        span = f"{first.strftime('%B')} {first.day}, {first.year}"
    elif first.year == last.year and first.month == last.month:
        span = f"{first.strftime('%B')} {first.day}–{last.day}, {first.year}"
    elif first.year == last.year:
        span = f"{first.strftime('%B')} {first.day}–{last.strftime('%B')} {last.day}, {first.year}"
    else:
        span = (
            f"{first.strftime('%B')} {first.day}, {first.year}–"
            f"{last.strftime('%B')} {last.day}, {last.year}"
        )
    return f"Tremor summary: {span}"


def _requested_span(days: int, tz: str) -> tuple[date, date]:
    """Local dates the report would have covered when the window is empty."""
    today = datetime.now(ZoneInfo(tz)).date()
    return today - timedelta(days=days - 1), today


def _clean_narrative(text: str) -> str:
    """Drop a model title, code fences, and any column names that slip through."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    body = cleaned.splitlines()
    if body and body[0].startswith("# "):
        body = body[1:]
        while body and not body[0].strip():
            body = body[1:]
        cleaned = "\n".join(body).strip()
    cleaned = cleaned.replace("`", "")
    for name, plain in _COLUMN_WORDS:
        cleaned = re.sub(rf"\b{name}\b", plain, cleaned)
    return cleaned


def _minutes(value: object) -> int:
    """Minutes from local midnight."""
    stamp = pd.Timestamp(value)  # type: ignore[arg-type]
    return int(stamp.hour) * 60 + int(stamp.minute)


def _clock(stamp: pd.Timestamp) -> str:
    """12-hour clock time, such as 7:28 am."""
    hour = int(stamp.hour)
    suffix = "am" if hour < 12 else "pm"
    return f"{hour % 12 or 12}:{int(stamp.minute):02d} {suffix}"


def _clock_minutes(minutes: int) -> str:
    """12-hour clock time from minutes past midnight."""
    hour, minute = divmod(int(minutes) % (24 * 60), 60)
    suffix = "am" if hour < 12 else "pm"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def _hour_name(hour: int) -> str:
    """Hour label such as 7 am or 3 pm."""
    suffix = "am" if hour < 12 else "pm"
    return f"{hour % 12 or 12} {suffix}"


def _lag(minutes: int) -> str:
    """A gap such as 1 hour 45 minutes."""
    hours, mins = divmod(abs(int(minutes)), 60)
    parts: list[str] = []
    if hours == 1:
        parts.append("1 hour")
    elif hours:
        parts.append(f"{hours} hours")
    if mins == 1:
        parts.append("1 minute")
    elif mins:
        parts.append(f"{mins} minutes")
    return " ".join(parts) or "0 minutes"


def _days(days: int) -> int:
    """Reject bools and anything below 1."""
    if isinstance(days, bool):
        raise ValueError("days must be an integer of at least 1")
    try:
        number = int(days)
    except (TypeError, ValueError):
        raise ValueError("days must be an integer of at least 1") from None
    if number < 1:
        raise ValueError("days must be an integer of at least 1")
    return number


def _device(device: str) -> str:
    """Reject a blank device id. SQL still binds this as a parameter."""
    if not isinstance(device, str) or not device.strip():
        raise ValueError("device must not be empty")
    return device.strip()


if __name__ == "__main__":
    main()
