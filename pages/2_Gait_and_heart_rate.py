"""Gait & heart-rate page inside the main Tremor Tracker dashboard."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st  # noqa: E402

import gait_dashboard  # noqa: E402

st.set_page_config(page_title="Tremor Tracker · Gait", layout="wide")
gait_dashboard.render()
