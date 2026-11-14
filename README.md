# Tremor Tracker

A wrist-worn accelerometer measures how fast and how strongly a resting hand is shaking. Readings are stored in Tiger Data and shown on a live dashboard, with a doctor-visit summary written by Gemini. This is a monitoring prototype, not a diagnostic device.

## Setup

Copy `.env.example` to `.env` and fill in `TIGER_URL` and `GEMINI_API_KEY`. The database URL should end in `sslmode=require`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python check_env.py
```

`python check_env.py` prints the Postgres version, the TimescaleDB extension version, and a one-line reply from Gemini.
