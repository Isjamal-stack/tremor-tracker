# Tremor Tracker

A wrist-worn accelerometer measures how fast and how strongly a resting hand is shaking. Readings are stored in Tiger Data and shown on a live dashboard, with a doctor-visit summary written by Gemini. This is a monitoring prototype, not a diagnostic device.

## Accessibility

A dose is logged without a button or a touchscreen. The wearer holds a fist squeeze for about a second and a half, and the Myo answers with a short, silent buzz on the wrist. Confirmation is felt, so it does not depend on looking at a screen or hearing a sound.

That matters for people whose hands tremble. Small buttons, phone taps, and touch targets are easy to miss when a hand is shaking. A whole-hand squeeze is a motion they can still make, and the buzz confirms the log without a second precise action.

The Raspberry Pi backup uses a large physical button. One press logs the dose and a beep confirms it, so the backup stays usable without a screen or fine aiming.

## Setup

Copy `.env.example` to `.env` and fill in `TIGER_URL` and `GEMINI_API_KEY`. The database URL should end in `sslmode=require`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python check_env.py
```

`python check_env.py` prints the Postgres version, the TimescaleDB extension version, and a one-line reply from Gemini.

## Security

`security.sql` adds two database roles. Replace the two password placeholders and run the file once as the admin, in the Tiger console or with `psql "$TIGER_URL" -f security.sql`. Then fill the new URLs in `.env`. Every URL ends in `sslmode=require`. On the Pi, export the writer URL the same way.

`tremor_ingest.py` uses `TIGER_WRITER_URL` (role `tremor_writer`). That role can insert into `accel_raw`, `tremor_features`, and `med_events`. The dashboard and the doctor-visit summary use `TIGER_READER_URL` (role `tremor_reader`). That role can select from those tables and from `tremor_hourly`. `TIGER_URL` stays the admin connection for the schema, the seed, and `check_env.py`. If a split URL is empty, that script uses `TIGER_URL` until you set it.

**Device spoofing.** The writer picks the `device_id` on each insert, including `tremor`. Its only grant is insert, so rows already stored stay as they were. The dashboard connects as the reader, whose only grant is select.

**Health data at rest.** Samples, tremor features, and dose times stay in Tiger Data. The app URLs require SSL, so that data is encrypted on the wire. The writer role's grant is insert-only, which keeps the ingest password from reading the stored history.

**Leaked keys.** URLs and `GEMINI_API_KEY` come from the environment. `.env` is gitignored, and `.env.example` ships blank. The writer URL can insert. The reader URL can read. Query values are passed as parameters.
