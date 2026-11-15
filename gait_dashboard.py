"""Streamlit page: live gait + heart rate from the "Tremor Tracker" ankle device.

  streamlit run gait_dashboard.py            # this page on its own
  (also appears as a page inside dashboard.py via pages/)

Reads only local files written by gait_bridge.py; nothing leaves the laptop.
Monitoring prototype, not a medical device.
"""
from __future__ import annotations

import glob
import json
import os
import time

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

import gait_algo
import gait_model

ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE_JSON = os.path.join(ROOT, "data", "live", "live.json")
CMD_FILE = os.path.join(ROOT, "data", "live", "commands.jsonl")
HIST_GLOB = os.path.join(ROOT, "data", "history", "*.csv")
TRIAL_GLOB = os.path.join(ROOT, "data", "real", "*.csv")
BLUE, ORANGE, DARK = "#2a78d6", "#eb6834", "#0d366b"
ZONE = "America/New_York"
STATE_LABEL = {"walk": ("Walking", "blue", ":material/directions_walk:"),
               "rest": ("Standing still", "gray", ":material/accessibility_new:"),
               "fog": ("Possible freeze", "orange", ":material/front_hand:"),
               "tremor": ("Leg tremor", "violet", ":material/graphic_eq:")}


def send(cmd: dict) -> None:
    """Queue a command for the bridge, which forwards it over Bluetooth."""
    os.makedirs(os.path.dirname(CMD_FILE), exist_ok=True)
    with open(CMD_FILE, "a") as f:
        f.write(json.dumps(cmd) + "\n")
    st.toast(f"Sent: {cmd['cmd']}")


def read_live() -> dict | None:
    """Latest live.json, or None if the bridge has not written one."""
    try:
        with open(LIVE_JSON) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _quiet(chart: alt.Chart, height: int = 220) -> alt.Chart:
    return (chart.properties(height=height)
            .configure_axis(gridColor="#eef1f4", domainColor="#e6e8ec", tickColor="#e6e8ec",
                            labelColor="#667085", titleColor="#344054")
            .configure_axisX(grid=False).configure_view(strokeWidth=0)
            .configure_legend(orient="top", title=None, labelColor="#344054"))


@st.fragment(run_every=1)
def live_panel() -> None:
    """Connection badge, live metrics and the last 10 s of motion."""
    live = read_live()
    age = time.time() - live["t"] if live else 1e9
    with st.container(border=True):
        st.subheader("Live now")
        if not live or age > 5 or not live.get("status"):
            st.badge("Waiting for Tremor Tracker", icon=":material/bluetooth_searching:", color="gray")
            st.caption("Start the bridge: `python gait_bridge.py` (Bluetooth) "
                       "or `python gait_bridge.py --sim` (no hardware).")
            return
        s = live["status"]
        name, color, icon = STATE_LABEL.get(s.get("s"), (s.get("s"), "gray", None))
        with st.container(horizontal=True, gap="small"):
            st.badge(name, icon=icon, color=color)
            src = "Bluetooth" if live["source"] == "bluetooth" else "Simulated"
            st.badge(f"{src} · {age:.1f} s ago", icon=":material/bluetooth_connected:", color="green")
            if s.get("q"):
                st.badge("Cue beat playing", icon=":material/music_note:", color="orange")
        a, b, c, d, e = st.columns(5)
        a.metric("Steps", f"{s.get('n', 0)}")
        b.metric("Cadence", f"{s.get('c') or '—'}" + (" /min" if s.get("c") else ""))
        c.metric("Stride variability", f"{s.get('v')}%" if s.get("v") else "—")
        d.metric("Freeze index", f"{s.get('f', 0):.2f}")
        e.metric("Heart rate", f"{s['b']} bpm" if s.get("b") else "—",
                 help="MAX30102 on the device. Needs a fingertip or skin on the sensor.")
        svm = live.get("svm") or []
        if svm:
            df = pd.DataFrame({"seconds": np.arange(len(svm)) / 25.0 - len(svm) / 25.0,
                               "g": svm, "series": "Motion"})
            ch = alt.Chart(df).mark_line(strokeWidth=1.6).encode(
                x=alt.X("seconds:Q", title="Seconds ago"),
                y=alt.Y("g:Q", title="Acceleration (g)", scale=alt.Scale(zero=False)),
                color=alt.Color("series:N", scale=alt.Scale(range=[BLUE]), legend=None),
                tooltip=[alt.Tooltip("seconds:Q", format=".1f"), alt.Tooltip("g:Q", format=".2f")])
            st.altair_chart(_quiet(ch, 180), width="stretch", theme=None,
                            alt="Ankle acceleration magnitude, last 10 seconds")


def controls_panel() -> None:
    """Buzzer, cue beat and detector settings, sent to the device over Bluetooth."""
    with st.container(border=True):
        st.subheader("Device controls")
        one, two, three = st.columns(3)
        with one:
            st.markdown("**Buzzer**")
            if st.button("Beep", icon=":material/notifications_active:"):
                send({"cmd": "beep", "s": 0.5})
            st.markdown("**Cue beat (metronome)**")
            bpm = st.slider("Beats per minute", 60, 140, 100, 5, key="metro_bpm")
            m1, m2 = st.columns(2)
            if m1.button("Start", key="metro_on"):
                send({"cmd": "metronome", "bpm": bpm, "on": True})
            if m2.button("Stop", key="metro_off"):
                send({"cmd": "metronome", "on": False})
        with two:
            st.markdown("**Freeze cueing**")
            mode = st.radio("When a possible freeze is detected", ["Play cue beat", "Off"],
                            key="cue_mode", horizontal=True)
            if st.button("Apply", key="cue_apply"):
                send({"cmd": "cue", "mode": "off" if mode == "Off" else "freeze"})
            cue_bpm = st.number_input("Cue tempo (0 = learn from walking)", 0, 160, 0, 5)
            if st.button("Set tempo"):
                send({"cmd": "cue_bpm", "bpm": cue_bpm})
        with three:
            st.markdown("**Detector**")
            fi = st.slider("Freeze index threshold", 1.0, 5.0, 2.0, 0.1,
                           help="3-8 Hz power / 0.5-3 Hz power. Higher = fewer freeze alerts.")
            if st.button("Apply threshold"):
                send({"cmd": "fi", "v": fi})
            if st.button("Reset step count", icon=":material/restart_alt:"):
                send({"cmd": "reset"})
            st.caption("Laser: reserved for a later version.")


def history_panel() -> None:
    """Tracker history: per-minute steps, cadence, heart rate and freezes."""
    files = sorted(glob.glob(HIST_GLOB))
    with st.container(border=True):
        st.subheader("Tracker history")
        if not files:
            st.caption("No history yet. The bridge saves one row per second while connected.")
            return
        df = pd.concat([pd.read_csv(f) for f in files[-7:]], ignore_index=True)
        df["time"] = pd.to_datetime(df["time_utc"], utc=True).dt.tz_convert(ZONE)
        df = df.sort_values("time")
        session = (df["time"].diff() > pd.Timedelta(minutes=1)) | (df["steps"].diff() < 0)
        df["session"] = session.cumsum()
        df["step_inc"] = df.groupby("session")["steps"].diff().clip(lower=0).fillna(0)
        df["fog_inc"] = df.groupby("session")["fog_events"].diff().clip(lower=0).fillna(0)
        per = df.set_index("time").resample("1min").agg(
            steps=("step_inc", "sum"), cadence=("cadence_spm", lambda v: v[v > 0].median()),
            bpm=("bpm", "mean"), freezes=("fog_inc", "sum"),
            walking=("state", lambda v: (v == "walk").mean())).reset_index()
        per = per[per["walking"].notna()]
        today = df[df["time"].dt.date == df["time"].iloc[-1].date()]
        a, b, c, d = st.columns(4)
        a.metric("Steps today", f"{int(today['step_inc'].sum()):,}")
        b.metric("Time walking", f"{(today['state'] == 'walk').sum() / 60:.1f} min")
        c.metric("Possible freezes", f"{int(today['fog_inc'].sum())}")
        hr = today["bpm"].dropna()
        d.metric("Average heart rate", f"{hr.mean():.0f} bpm" if len(hr) else "—")
        base = alt.Chart(per).encode(x=alt.X("time:T", title="Time", axis=alt.Axis(format="%H:%M")))
        steps = base.mark_bar(color=BLUE).encode(
            y=alt.Y("steps:Q", title="Steps per minute"),
            tooltip=[alt.Tooltip("time:T", format="%H:%M"), "steps:Q",
                     alt.Tooltip("cadence:Q", format=".0f", title="Cadence")])
        st.altair_chart(_quiet(steps, 200), width="stretch", theme=None, alt="Steps per minute")
        hrc = base.mark_line(color=ORANGE, point=True).encode(
            y=alt.Y("bpm:Q", title="Heart rate (bpm)", scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("time:T", format="%H:%M"), alt.Tooltip("bpm:Q", format=".0f")])
        st.altair_chart(_quiet(hrc, 180), width="stretch", theme=None, alt="Heart rate per minute")
        st.caption("Bars are steps per minute; the orange line is heart rate. "
                   "History is stored on this laptop only.")


def trials_panel() -> None:
    """Recorded walking trials, detector output and the trained model."""
    files = sorted(glob.glob(TRIAL_GLOB))
    with st.expander(f"Walking trials and model ({len(files)} recordings)"):
        model = gait_model.load()
        if model:
            st.caption(f"On-device model: softmax regression, {model.get('train_windows', 0)} "
                       f"training windows (simulated + recorded), rest / walk / freeze / tremor.")
        if not files:
            st.caption("No trials yet. Record with `python3 gait_trial.py --phased 3` on the Pi.")
            return
        pick = st.selectbox("Recording", files, format_func=os.path.basename)
        d = gait_algo.load_csv(pick)
        res = gait_algo.analyse(d["xyz"], method="cwt")
        df = pd.DataFrame({"t": d["t_s"], "g": res.svm, "phase": d.get("label", "")})
        strides = pd.DataFrame({"t": d["t_s"][res.strides], "g": res.svm[res.strides]})
        line = alt.Chart(df).mark_line(strokeWidth=1, color=BLUE).encode(
            x=alt.X("t:Q", title="Seconds"), y=alt.Y("g:Q", title="Acceleration (g)"),
            tooltip=[alt.Tooltip("t:Q", format=".1f"), alt.Tooltip("g:Q", format=".2f"), "phase:N"])
        dots = alt.Chart(strides).mark_circle(color=ORANGE, size=40).encode(
            x="t:Q", y="g:Q", tooltip=[alt.Tooltip("t:Q", format=".2f", title="Foot strike (s)")])
        st.altair_chart(_quiet(alt.layer(line, dots), 220), width="stretch", theme=None,
                        alt="Recorded trial with detected foot strikes")
        st.caption("Orange dots are detected foot strikes (one per stride of the ankle with the sensor).")
        if "label" in d:
            rows, prev, start = [], None, 0
            lab = list(d["label"]) + [None]
            for i, v in enumerate(lab):
                if v != prev:
                    if prev is not None:
                        n = int(((res.strides >= start) & (res.strides < i)).sum())
                        dur = (i - start) / gait_algo.RATE_HZ
                        rows.append({"phase": prev, "seconds": round(dur, 1), "steps": 2 * n,
                                     "cadence /min": round(120 * n / dur) if dur else 0})
                    prev, start = v, i
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def render() -> None:
    """Draw the whole page."""
    st.title("Gait & heart rate")
    st.caption("Ankle-worn Tremor Tracker over Bluetooth · computed on the device · monitoring prototype")
    live_panel()
    controls_panel()
    history_panel()
    trials_panel()
    st.divider()
    st.caption("Walking cues use rhythmic auditory cueing, a beat to step to. This is a monitoring "
               "prototype; it does not diagnose and is not a medical device.")


if __name__ == "__main__":
    st.set_page_config(page_title="Tremor Tracker · Gait", layout="wide")
    render()
