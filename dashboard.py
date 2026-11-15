"""Streamlit dashboard for the tremor monitoring prototype."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import altair as alt
import pandas as pd
import streamlit as st

import db
import gemini_report

_LIVE_MINUTES = 5
_HISTORY_DAYS = 7
_STRENGTH = "#2a78d6"
_DOSE = "#eb6834"
_BLUE_DARK = "#0d366b"
_BLUE_LIGHT = "#cde2fb"
_COLOR_NOTE = "Blue is strength. Orange marks a dose."


def main() -> None:
    """Draw the sidebar, the five panels, and the footer."""
    st.set_page_config(page_title="Tremor Tracker", layout="wide")
    st.title("Tremor Tracker")
    st.caption("Monitoring prototype")
    device, zone = _sidebar()
    use_connection()
    live_panel(device, zone)
    today_panel(device, zone)
    week_panel(device, zone)
    medication_panel(device, zone)
    tiger_panel()
    _doctor_report(device, zone)
    st.divider()
    st.caption("History is simulated for the demo. Live data comes from the wristband.")


def _sidebar() -> tuple[str, str]:
    device = st.sidebar.text_input("Device ID", value=db.default_device()).strip() or db.default_device()
    raw_zone = st.sidebar.text_input("Time zone", value=db.default_tz()).strip() or db.default_tz()
    try:
        ZoneInfo(raw_zone)
        zone = raw_zone
    except ZoneInfoNotFoundError:
        st.sidebar.error("Unknown time zone. Using America/New_York.")
        zone = "America/New_York"
    return device, zone


def _doctor_report(device: str, zone: str) -> None:
    """Write a visit summary on click, then keep it in session state.

    Later reruns, including the auto-refreshing panels, render the saved
    Markdown and do not call Gemini again.
    """
    if st.sidebar.button("Generate doctor report", key="generate_doctor_report"):
        with st.spinner("Writing the doctor-visit summary..."):
            try:
                text = gemini_report.build_report(
                    db.get_conn(), device, _HISTORY_DAYS, tz=zone
                )
            except (RuntimeError, ValueError) as exc:
                st.session_state["doctor_report_error"] = str(exc)
            else:
                st.session_state["doctor_report"] = {
                    "markdown": text,
                    "device": device,
                    "days": _HISTORY_DAYS,
                }
                st.session_state["doctor_report_error"] = None
    error = st.session_state.get("doctor_report_error")
    saved = st.session_state.get("doctor_report")
    if error:
        st.error(error)
    if not isinstance(saved, dict) or not saved.get("markdown"):
        return
    st.download_button(
        "Download report.md",
        data=saved["markdown"],
        file_name="report.md",
        mime="text/markdown",
        key="download_doctor_report",
    )
    st.markdown(saved["markdown"])


@st.cache_resource
def cached_connection() -> object:
    """Cache one Tiger Data connection for this Streamlit process."""
    return db.get_conn()


def use_connection() -> None:
    """Replace the Streamlit cache when db.py has reconnected."""
    current = cached_connection()
    if db.get_conn() is not current:
        cached_connection.clear()
        cached_connection()


@st.fragment(run_every=2)
def live_panel(device: str, zone: str) -> None:
    """Status, latest numbers, and a 5-minute strength line."""
    with st.container(border=True):
        st.subheader("Live now")
        try:
            use_connection()
            latest = db.latest_reading(device, zone)
            features = db.recent_features(_LIVE_MINUTES, device, zone)
            now = datetime.now(ZoneInfo(zone))
            dose_frame = db.doses(now - timedelta(days=8), now + timedelta(minutes=1), device, zone)
        except (RuntimeError, ValueError) as exc:
            st.error(str(exc))
            return
        _status_badge(latest)
        hz, strength = "—", "—"
        if not _is_waiting(latest):
            hz = _number(latest["peak_hz"].iloc[0], "{:.2f}")
            strength = _number(latest["rms_g"].iloc[0], "{:.3f} g")
        one, two, three = st.columns(3)
        one.metric("Peak Hz", hz)
        two.metric("Strength", strength)
        three.metric("Since last dose", _since_label(_minutes_since(dose_frame)))
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=_LIVE_MINUTES)
        recent = dose_frame[pd.to_datetime(dose_frame["time"], utc=True) >= cutoff]
        if features.empty:
            st.caption("No readings in the last 5 minutes.")
            return
        _draw(
            _strength_chart(features, recent, "rms_g", "%H:%M:%S", "point"),
            "5-minute strength, with dose markers",
            note=_COLOR_NOTE,
        )


@st.fragment(run_every=30)
def today_panel(device: str, zone: str) -> None:
    """One-minute strength since local midnight, with doses as vertical lines."""
    with st.container(border=True):
        st.subheader("Today")
        try:
            use_connection()
            timeline = db.today_timeline(device, zone)
            now = datetime.now(ZoneInfo(zone))
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            dose_frame = db.doses(midnight, now + timedelta(minutes=1), device, zone)
        except (RuntimeError, ValueError) as exc:
            st.error(str(exc))
            return
        if timeline.empty:
            st.caption("No readings since local midnight.")
            return
        extra = [alt.Tooltip("avg_peak_hz:Q", title="Average Hz", format=".2f")]
        chart = _strength_chart(timeline, dose_frame, "avg_rms_g", "%H:%M", "rule", extra)
        _draw(chart, "Strength since local midnight, with doses as vertical lines", note=_COLOR_NOTE)


def week_panel(device: str, zone: str) -> None:
    """Day-by-hour heatmap of average strength for the last 7 local days."""
    with st.container(border=True):
        st.subheader("This week")
        try:
            use_connection()
            frame = db.week_heatmap(device, zone)
        except (RuntimeError, ValueError) as exc:
            st.error(str(exc))
            return
        if frame.empty:
            st.caption("No hourly readings for this device.")
            return
        _draw(_heatmap(frame), "Average strength by day and hour", legend="right", grids=False)
        st.caption("Darker blue is higher average strength.")


def medication_panel(device: str, zone: str) -> None:
    """Before and after strength for each dose, plus the average drop."""
    with st.container(border=True):
        st.subheader("Medication effect")
        try:
            use_connection()
            frame = db.dose_response(_HISTORY_DAYS, device, zone)
        except (RuntimeError, ValueError) as exc:
            st.error(str(exc))
            return
        complete = frame.dropna(subset=["before_rms_g", "after_rms_g"])
        complete = complete[complete["before_rms_g"] > 0]
        if complete.empty:
            st.caption("No doses with before and after readings in the last 7 days.")
            return
        drop = ((complete["before_rms_g"] - complete["after_rms_g"]) / complete["before_rms_g"]).mean()
        st.metric("Average drop", f"{drop:.0%}")
        _draw(_dumbbell(complete), "Strength before and after each logged dose", height=320)
        st.caption(
            "Each pair is one logged dose: the 2 hours before it, and 30 minutes to 2.5 hours after."
        )


def tiger_panel() -> None:
    """Row counts, chunks, compression, and the continuous aggregate."""
    with st.expander("Under the hood: Tiger Data"):
        try:
            use_connection()
            stats = db.tiger_stats().iloc[0]
        except (RuntimeError, ValueError) as exc:
            st.error(str(exc))
            return
        raw_chunks, feature_chunks = int(stats["raw_chunks"]), int(stats["feature_chunks"])
        one, two, three = st.columns(3)
        one.metric("Raw rows", f"{int(stats['raw_rows']):,}")
        two.metric("Feature rows", f"{int(stats['feature_rows']):,}")
        three.metric("Chunks", f"{int(stats['chunk_count']):,}")
        four, five, six = st.columns(3)
        four.metric("Size before compression", _bytes(stats["before_bytes"]))
        five.metric("Size after compression", _bytes(stats["after_bytes"]))
        six.metric("Compression ratio", _ratio(stats["compression_ratio"]))
        st.caption(f"{feature_chunks} feature chunks and {raw_chunks} raw chunks (show_chunks).")
        if raw_chunks == 0:
            st.caption("Compression applies to accel_raw chunks older than one hour. None yet.")
        st.caption(
            "Continuous aggregate tremor_hourly rolls windows into one row per hour "
            "per device and refreshes in the database, including the newest hour."
        )


def _draw(
    chart: alt.Chart,
    alt_text: str,
    *,
    height: int = 280,
    legend: str = "top",
    grids: bool = True,
    note: str | None = None,
) -> None:
    quiet = _quiet(chart, height=height, legend=legend, y_grid=grids)
    st.altair_chart(quiet, width="stretch", theme=None, alt=alt_text)
    if note:
        st.caption(note)


def _strength_chart(
    features: pd.DataFrame,
    doses: pd.DataFrame,
    y_field: str,
    x_format: str,
    markers: str,
    extra: list[alt.Tooltip] | None = None,
) -> alt.Chart:
    plot = features.copy()
    plot["series"] = "Strength"
    plot["strength"] = pd.to_numeric(plot[y_field])
    tooltip = [
        alt.Tooltip("local_time:T", title="Time", format=x_format),
        alt.Tooltip("strength:Q", title="Strength (g)", format=".3f"),
        *(extra or []),
    ]
    if "peak_hz" in plot.columns and not extra:
        tooltip.append(alt.Tooltip("peak_hz:Q", title="Peak Hz", format=".2f"))
    line = (
        alt.Chart(plot)
        .mark_line(strokeWidth=2, interpolate="monotone")
        .encode(x=_time_x(x_format), y=_strength_y(), color=_series_color(), tooltip=tooltip)
    )
    marks = _dose_marks(plot, doses, y_field, x_format, markers)
    return alt.layer(line, marks) if marks is not None else line


def _dose_marks(
    features: pd.DataFrame, doses: pd.DataFrame, y_field: str, x_format: str, markers: str
) -> alt.Chart | None:
    if doses.empty:
        return None
    base = doses.copy()
    base["series"] = "Dose"
    tip = [
        alt.Tooltip("local_time:T", title="Dose", format=x_format),
        alt.Tooltip("note:N", title="Note"),
    ]
    if markers == "rule":
        return alt.Chart(base).mark_rule(strokeWidth=2).encode(
            x=_time_x(x_format), color=_series_color(), tooltip=tip
        )
    times = pd.to_datetime(features["local_time"])
    picked = [
        float(features.iloc[int((times - stamp).abs().to_numpy().argmin())][y_field])
        for stamp in pd.to_datetime(base["local_time"])
    ]
    base["strength"] = picked
    tip.append(alt.Tooltip("strength:Q", title="Strength (g)", format=".3f"))
    return alt.Chart(base).mark_circle(size=90).encode(
        x=_time_x(x_format), y=_strength_y(), color=_series_color(), tooltip=tip
    )


def _heatmap(frame: pd.DataFrame) -> alt.Chart:
    plot = frame.copy()
    labels = pd.to_datetime(plot["day"]).dt.strftime("%b %d").str.replace(" 0", " ", regex=False)
    plot["day_label"] = labels
    day_order = plot.sort_values("day", ascending=False)["day_label"].drop_duplicates().tolist()
    hours = sorted(int(hour) for hour in plot["hour"].unique())
    tip = [
        alt.Tooltip("day:T", title="Date", format="%b %d, %Y"),
        alt.Tooltip("hour:O", title="Hour"),
        alt.Tooltip("avg_rms_g:Q", title="Average strength (g)", format=".3f"),
        alt.Tooltip("frac_tremor:Q", title="% of time with tremor", format=".0%"),
        alt.Tooltip("avg_peak_hz:Q", title="Average Hz", format=".2f"),
    ]
    color = alt.Color(
        "avg_rms_g:Q",
        title="Strength (g)",
        scale=alt.Scale(range=[_BLUE_LIGHT, _BLUE_DARK]),
        legend=alt.Legend(format=".3f"),
    )
    return alt.Chart(plot).mark_rect(stroke="#ffffff", strokeWidth=1).encode(
        x=alt.X("hour:O", sort=hours, title="Hour"),
        y=alt.Y("day_label:O", sort=day_order, title="Day"),
        color=color,
        tooltip=tip,
    )


def _dumbbell(frame: pd.DataFrame) -> alt.Chart:
    plot = frame.copy()
    plot["drop_pct"] = (plot["before_rms_g"] - plot["after_rms_g"]) / plot["before_rms_g"]
    plot["dose_id"] = plot["dose_time"].astype(str)
    # value columns cannot also be id columns; pandas 3 then returns no rows.
    long = plot.melt(
        id_vars=["dose_id"],
        value_vars=["before_rms_g", "after_rms_g"],
        var_name="when",
        value_name="strength",
    )
    kept = plot[["dose_id", "local_time", "before_rms_g", "after_rms_g", "drop_pct"]]
    long = long.merge(kept, on="dose_id", how="left")
    long["when"] = long["when"].map({"before_rms_g": "Before", "after_rms_g": "After"})
    tooltip = [
        alt.Tooltip("local_time:T", title="Dose", format="%b %d, %H:%M"),
        alt.Tooltip("when:N", title="Point"),
        alt.Tooltip("before_rms_g:Q", title="Before (g)", format=".3f"),
        alt.Tooltip("after_rms_g:Q", title="After (g)", format=".3f"),
        alt.Tooltip("drop_pct:Q", title="Drop", format=".0%"),
    ]
    x = alt.X(
        "local_time:T",
        title="Dose time",
        axis=alt.Axis(format="%b %d %H:%M", grid=False, labelAngle=-30),
    )
    color = alt.Color(
        "when:N",
        title=None,
        scale=alt.Scale(domain=["Before", "After"], range=[_STRENGTH, _BLUE_DARK]),
        legend=alt.Legend(title=None, orient="top"),
    )
    stems = alt.Chart(long).mark_line(color=_STRENGTH, strokeWidth=2).encode(
        x=x, y=_strength_y(), detail="dose_id:N", tooltip=tooltip
    )
    points = alt.Chart(long).mark_circle(size=70).encode(
        x=x, y=_strength_y(), color=color, tooltip=tooltip
    )
    return alt.layer(stems, points)


def _time_x(x_format: str) -> alt.X:
    axis = alt.Axis(format=x_format, grid=False, labelOverlap=True)
    return alt.X("local_time:T", title="Time", axis=axis)


def _strength_y() -> alt.Y:
    return alt.Y("strength:Q", title="Strength (g)", scale=alt.Scale(zero=True))


def _series_color() -> alt.Color:
    scale = alt.Scale(domain=["Strength", "Dose"], range=[_STRENGTH, _DOSE])
    return alt.Color("series:N", title=None, scale=scale, legend=alt.Legend(title=None, orient="top"))


def _quiet(chart: alt.Chart, *, height: int, legend: str, y_grid: bool) -> alt.Chart:
    axis = dict(
        gridColor="#eef1f4", domainColor="#e6e8ec", tickColor="#e6e8ec",
        labelColor="#667085", titleColor="#344054", labelFontSize=11, titleFontSize=12,
    )
    return (
        chart.properties(height=height)
        .configure_axis(**axis)
        .configure_axisX(grid=False)
        .configure_axisY(grid=y_grid)
        .configure_legend(labelColor="#344054", titleColor="#344054", orient=legend)
        .configure_view(strokeWidth=0)
    )


def _status_badge(latest: pd.DataFrame) -> None:
    if _is_waiting(latest):
        st.badge("Waiting for the wristband", icon=":material/watch:", color="gray")
    elif bool(latest["tremor"].iloc[0]):
        st.badge("Tremor detected", icon=":material/graphic_eq:", color="orange")
    else:
        st.badge("Calm", icon=":material/check_circle:", color="blue")


def _is_waiting(latest: pd.DataFrame) -> bool:
    if latest.empty:
        return True
    stamp = pd.Timestamp(latest["time"].iloc[0])
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return pd.Timestamp.now(tz="UTC") - stamp.tz_convert("UTC") > timedelta(minutes=1)


def _minutes_since(doses: pd.DataFrame) -> float | None:
    if doses.empty:
        return None
    last = pd.to_datetime(doses["time"], utc=True).max()
    if pd.isna(last):
        return None
    return max(0.0, (pd.Timestamp.now(tz="UTC") - last).total_seconds() / 60.0)


def _since_label(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    hours, mins = divmod(int(minutes), 60)
    return f"{hours} h {mins} min" if hours else f"{mins} min"


def _number(value: object, template: str) -> str:
    if value is None or pd.isna(value):
        return "—"
    return template.format(float(value))


def _bytes(value: object) -> str:
    if value is None or pd.isna(value):
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _ratio(value: object) -> str:
    return "—" if value is None or pd.isna(value) else f"{float(value):.1f}×"


if __name__ == "__main__":
    main()
