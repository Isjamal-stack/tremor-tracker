# Tremor Tracker — Build Spec for Cursor

Nov 13, 2026 · @Ismail

## How to use this doc

Give Cursor one step at a time, and check that step's "Done when" yourself before you start the next one. Steps 0 to 5 are all software and run on simulated data, so do them tonight. Step 6 is hardware, Saturday morning.

1. Export this doc as Markdown and save it as `BUILD_SPEC.md` in the root of your project.
2. Copy the "Global rules for Cursor" section into a Cursor project rule (or `AGENTS.md`) so every chat follows it.
3. For each step, open a new Cursor Agent chat and paste that step's prompt. Each prompt points Cursor at `@BUILD_SPEC.md`.
4. Run the "Done when" check yourself. When it passes, make a git commit, so you can roll back if a later step breaks something.
5. If something fails, paste the error or output to Claude.

**Progress**

- [ ] Step 0: Project setup
- [ ] Step 1: Database
- [ ] Step 2: Seed a week of history
- [ ] Step 3: Dashboard
- [ ] Step 4: Ingest script (Myo, with Pi backup)
- [ ] Step 5: Gemini report
- [ ] Step 6: Hardware setup and tuning
- [ ] Step 7: Hardening and prize add-ons
- [ ] Step 8: Demo and submission

## Project overview

A wrist-worn accelerometer measures how fast and how strongly a resting hand is shaking. The readings are stored in Tiger Data and turned into a live dashboard plus a doctor-visit summary written by Gemini.

**The problem:** a clinic visit is a snapshot, but tremor changes throughout the day with medication timing. The wearer presses a button each time they take a dose, so the effect of every dose shows up in the data. This is a monitoring prototype, not a diagnostic device.

**Hardware:** a Myo armband (motion sensors plus 8 muscle sensors) streaming over Bluetooth to the laptop, which runs all the analysis. The wearer logs a dose by squeezing a fist, and the band buzzes to confirm. The backup and low-cost version is a Raspberry Pi Zero 2W with a Grove HAT, the ±1.5g accelerometer (MMA7660), a button on D5 and a buzzer on D16, running the same analysis.

&#91;embedded content: architecture · 2 writers, 1 database, 2 readers\]

Only the Pi script (or its `--simulate` mode) and the seed script write data. The dashboard and the report only read it, so each piece can be built and tested on its own.

**Prizes targeted:** Best MedTech, Best Hardware, Best Use of Tiger Data, Best Use of Gemini, the GoDaddy Registry domain prize, the SpaceXSI raffle (Cursor + Grok Bot) and Best Pitch. Stretch goals: Most Secure Hack and Vultr.

| File | Runs on | What it does | Step |
| --- | --- | --- | --- |
| `schema.sql` | Tiger Data, once | Tables, compression, hourly rollup | 1 |
| `seed_week.py` | Laptop | Writes 7 days of simulated history | 2 |
| `db.py` | Laptop | Database connection plus every SQL query the dashboard and report use | 3 |
| `dashboard.py` | Laptop (later Vultr) | Streamlit app | 3 |
| `tremor_ingest.py` | Laptop (Myo), Pi (Grove backup), or `--simulate` | Turns sensor readings into tremor features, logs doses, writes to the database | 4 |
| `myo_source.py` | Laptop | Connects to the Myo over Bluetooth: motion and muscle data in, vibration out | 4 |
| `gemini_report.py` | Laptop | Doctor-visit summary | 5 |
| `check_env.py` | Laptop | Confirms the database and Gemini keys work | 0 |
| `requirements.txt`, `.env.example`, `.gitignore`, `README.md` | Repo | Setup and secrets hygiene | 0 |

## Global rules for Cursor

Paste this whole section into a Cursor project rule so every step follows the same contract.

**Scope**

- Build only the step you are asked for. Ask before changing files that belong to another step.
- Plain Python 3.11+ scripts. Type hints and a docstring on every public function. Keep each file under about 400 lines.
- When a step is finished, tell me how to run its "Done when" check.

**Shared contract (never rename these)**

- Table and column names exactly as in Step 1.
- Tremor features: `peak_hz`, `rms_g`, `band_ratio`, `tremor`. Tremor band is 3 to 12 Hz. A window counts as tremor when `rms_g >= 0.015` and `band_ratio >= 0.5`.
- The default device ID everywhere is `tremor`.
- Store times as UTC `timestamptz`. Display them in `America/New_York`, converting in SQL with `AT TIME ZONE`.

* **Hardware path:** the Myo streams to the laptop over Bluetooth, and all analysis and database writes happen on the laptop. The Raspberry Pi is a backup that runs the same `tremor_ingest.py` with the Grove sensor (`--source grove`).

**Secrets and security**

- Read every secret from the environment (`python-dotenv` and `.env` on the laptop, `export` on the Pi). Never hardcode, log or print a key. `.env` is in `.gitignore`.
- Use parameterized SQL only. No f-strings or string concatenation in queries.
- Database driver: `psycopg2` (`psycopg2-binary` on the laptop, the apt package `python3-psycopg2` on the Pi).

**Medical wording**

- Call it a "monitoring prototype". Never diagnose and never suggest medication changes, in the UI or in Gemini prompts.

**Charts (Altair, bundled with Streamlit)**

- Strength line in blue `#2a78d6`. Dose markers in orange `#eb6834`, labeled or in a legend.
- Heatmaps use one blue scale from light (`#cde2fb`) to dark (`#0d366b`). Never a rainbow.
- One y-axis per chart. A tooltip on every chart. Gridlines and axes kept quiet.

**Streamlit gotchas**

- Auto-refreshing panels use `@st.fragment(run_every=...)`, so only that panel reruns.
- Cache the database connection with `st.cache_resource` and reconnect if it drops.
- If a deprecation warning mentions `use_container_width`, switch to `width="stretch"`.

## Step 0: Project setup

Set up the repo, accounts and secrets once. Every later step reads them from `.env`.

**You do this part (not Cursor)**

- Create a free Tiger Data service and copy its connection URL. It ends in `sslmode=require`.
- Get a Gemini API key from Google AI Studio.
- Create a GitHub repo, open it in Cursor, and save this doc as `BUILD_SPEC.md` in the root.
- Charge the Myo armband over micro-USB.

**Cursor builds**

- `requirements.txt`: numpy, pandas, psycopg2-binary, streamlit, altair, google-genai, python-dotenv, plus the Myo library your friend's script uses (for example dl-myo).
- `.env.example` with `TIGER_URL`, `GEMINI_API_KEY`, `GEMINI_MODEL=gemini-3.8-flash`, `TREMOR_DEVICE=tremor`, `TREMOR_TZ=America/New_York`.
- `.gitignore` covering `.env`, `.venv`, `__pycache__`, `*.csv` and `report.md`.
- `README.md` with a one-paragraph description and a setup section.
- `check_env.py`, which connects to Tiger Data, prints the Postgres and TimescaleDB versions, and makes one tiny Gemini call.

**Done when:** `python check_env.py` prints both versions and a one-line reply from Gemini.

```text
Read @BUILD_SPEC.md, especially "Global rules for Cursor" and "Step 0: Project setup".
Create requirements.txt, .env.example, .gitignore, README.md and check_env.py exactly as Step 0 describes.
check_env.py should load .env, connect to TIGER_URL with psycopg2, print SELECT version() and the timescaledb
extension version from pg_extension, then call Gemini (google-genai SDK, model from GEMINI_MODEL) with
"Reply with the word ready" and print the reply. Show clear error messages if a key is missing or wrong.
Then tell me the commands to create a venv, install requirements and run the check.
```

## Step 1: Database

The database is three tables, compression on the raw data, and an hourly rollup. The `schema.sql` Claude already sent implements exactly this.

- **Option A (fastest):** paste `schema.sql` into the Tiger Data SQL editor and run it.
- **Option B:** have Cursor write `schema.sql` from the spec below, using the prompt.

| Table | Kind | Columns | Notes |
| --- | --- | --- | --- |
| `accel_raw` | Hypertable, 1-hour chunks | `time` timestamptz, `device_id` text, `ax`, `ay`, `az` real (g) | About 100 rows/s. Compressed after 1 hour, segmented by `device_id` |
| `tremor_features` | Hypertable, 1-day chunks | `time`, `device_id`, `peak_hz`, `rms_g`, `band_ratio` real, `tremor` boolean | One row per analysis window (about 1.28 s) |
| `med_events` | Plain table | `time`, `device_id`, `note` text default `'dose'` | Index on (`device_id`, `time` DESC) |
| `tremor_hourly` | Continuous aggregate | `bucket` (1 hour), `device_id`, `avg_rms_g`, `max_rms_g`, `avg_peak_hz` (tremor windows only), `frac_tremor`, `windows` | Real-time aggregation on. Policy refreshes every 5 minutes |

**Why 1-hour chunks:** compression only applies to whole chunks older than the policy's age. The default 7-day chunks would never compress during the hackathon, and the compression ratio is something the Tiger Data judges want to see.

If the console says the compression functions are deprecated, the newer names are `add_columnstore_policy` and `convert_to_columnstore`.

**Done when:** `SELECT hypertable_name FROM timescaledb_information.hypertables;` lists `accel_raw` and `tremor_features`, and `SELECT * FROM tremor_hourly LIMIT 1;` runs without an error (an empty result is fine).

```text
Only if I'm not using the existing schema.sql:
Read @BUILD_SPEC.md "Step 1: Database". Write schema.sql for Tiger Data (TimescaleDB) implementing the table
exactly: hypertables via create_hypertable(..., by_range('time', <chunk interval>), if_not_exists => TRUE),
compression on accel_raw (segmentby device_id, orderby time DESC) with a 1-hour policy, the tremor_hourly
continuous aggregate WITH NO DATA, materialized_only = false, and a refresh policy (start 30 days, end 1 hour,
every 5 minutes). Make it safe to run twice. Add commented demo queries: compression ratio from
hypertable_compression_stats, and average rms_g in the 2 h before vs 30 min-2.5 h after each dose.
```

## Step 2: Seed a week of history

`seed_week.py` writes 7 realistic days of tremor readings and doses, so the dashboard and Gemini have a story to tell before any real data exists.

**The story the data must tell**

- **Hours:** waking hours only, 07:00 to 22:30 local time. Nothing overnight (the device is charging).
- **Doses:** around 07:30, 11:30, 15:30 and 19:30 local time, each shifted up to 15 minutes at random.
- **Tremor strength (`rms_g`) over a dose:**
  - Off medication, it's about 0.08 to 0.10 g. Each day's level varies by about 10%.
  - It starts falling 20 to 30 minutes after a dose and reaches about 0.02 g by 45 minutes.
  - It stays low for about 2.5 hours, then wears off over the next 45 minutes.
  - Overlapping doses don't push it below the low level.
- **Mornings:** before the first dose, tremor is at the full off-medication level.
- **Frequency:** one value per day between 4.5 and 5.5 Hz, plus or minus 0.3 Hz noise per window.
- **Voluntary movement:** 10% of windows. These get `band_ratio` 0.1 to 0.3 and `tremor` false.
- **`band_ratio`:** 0.6 to 0.95 for tremor windows, 0.3 to 0.6 for low "on medication" windows.
- **`tremor` flag:** the shared rule (`rms_g >= 0.015` and `band_ratio >= 0.5`).
- **Two planted events:**
  - 3 days ago, the 15:30 dose is missing, so tremor stays high until 19:30.
  - 5 days ago, the 11:30 dose is 1 h 45 min late.
- **Volume:** one window every 1.28 s, about 300k rows. The history ends 10 minutes before now.

**How it writes**

- Bulk-load with `COPY ... FROM STDIN` (psycopg2 `copy_expert`), one transaction per table. Never row-by-row INSERTs.
- Afterwards, run `CALL refresh_continuous_aggregate('tremor_hourly', NULL, NULL)` on a separate **autocommit** connection. It fails inside a transaction.
- Flags: `--days 7`, `--device tremor`, `--wipe` (first deletes that device's rows from `tremor_features` and `med_events`), `--seed 42` for repeatable output, and `--dry-run` (print a summary, write nothing).
- On Sunday morning, re-run it with `--wipe` so the week ends at demo time.

**Strength scale:** add a `--scale` flag (default 1) that multiplies every simulated strength. Use 1 for the Myo. If you demo on the Pi backup with the coarse ±1.5g sensor, use `--scale 3` so the history matches its live readings.

**Done when:** the script prints row counts and the dates of the two planted events, and the query below shows 7 days, with the missed-dose day highest in the afternoon.

```sql
SELECT (bucket AT TIME ZONE 'America/New_York')::date AS day,
       round(avg(avg_rms_g)::numeric, 3) AS avg_strength_g
FROM tremor_hourly GROUP BY 1 ORDER BY 1;
```

```text
Read @BUILD_SPEC.md "Global rules for Cursor" and "Step 2: Seed a week of history".
Write seed_week.py. Generate the data with numpy/pandas in a pure function (easy to test), then write it with
psycopg2 COPY FROM STDIN into tremor_features and med_events, then refresh tremor_hourly on an autocommit
connection. Implement every rule in "The story the data must tell" and all the flags. Print a summary:
rows written, date range, average strength per day, and the dates of the missed and late doses.
Run it with --dry-run first and show me the summary.
```

## Step 3: Dashboard

`dashboard.py` is a Streamlit app with five panels, all reading Tiger Data through `db.py`. Build it against the seeded week.

**`db.py`** holds every query, as a named function that returns a pandas DataFrame:

- `get_conn()`, cached and able to reconnect.
- `latest_reading()`.
- `recent_features(minutes)`.
- `today_timeline()`: 1-minute `time_bucket` averages from local midnight.
- `week_heatmap()`: reads from `tremor_hourly`.
- `doses(start, end)`.
- `dose_response(days)`: average `rms_g` in the 2 h before each dose vs from 30 min to 2.5 h after.
- `tiger_stats()`: `hypertable_compression_stats('accel_raw')`, `approximate_row_count` for both hypertables, and the chunk count from `show_chunks`.

| Panel | Refresh | Shows |
| --- | --- | --- |
| Live now | Every 2 s | Status ("Tremor detected" or "Calm", icon + label), peak Hz, strength in g, minutes since the last dose. A 5-minute strength line with dose markers |
| Today | Every 30 s | Strength in 1-minute buckets since local midnight, with doses as vertical lines |
| This week | On load | Heatmap, day × hour, of average strength. Tooltip: date, hour, average strength, % of time with tremor, average Hz |
| Medication effect | On load | Before vs after strength for each dose (dumbbell chart), plus the average drop in % |
| Under the hood: Tiger Data | Expander | Raw and feature row counts, chunk count, size before and after compression, compression ratio, one line on continuous aggregates |

**Sidebar:** device ID, time zone, and a "Generate doctor report" button (wired up in Step 5).

**Footer:** "History is simulated for the demo. Live data comes from the wristband."

**Empty states:** if there's no data in the last minute, the Live panel says "Waiting for the wristband" instead of erroring.

**Done when:** `streamlit run dashboard.py` fills every panel from the seeded data, the missed-dose afternoon stands out on the heatmap, and the Live panel shows its waiting message (it fills in during Step 4).

```text
Read @BUILD_SPEC.md "Global rules for Cursor" and "Step 3: Dashboard".
First write db.py with the listed query functions (parameterized SQL, times converted to America/New_York in
SQL, results as pandas DataFrames). Then write dashboard.py in Streamlit with the five panels in the table,
the sidebar and the footer. Use Altair for charts, following the chart rules: blue strength line, orange dose
markers, single-hue blue heatmap, tooltips everywhere. Use @st.fragment(run_every=2) for Live now and
run_every=30 for Today. Leave the report button as a placeholder that says "coming in Step 5".
```

## Step 4: Ingest script (Myo, with Pi backup)

`tremor_ingest.py` (renamed from `tremor_pi.py`) turns sensor readings into tremor features and writes them to Tiger Data. It takes one of three sources: the Myo armband (main), the Grove sensor on the Pi (backup), or a simulated sensor. Split the work: your friend builds `myo_source.py`, you build the rest.

| Source | Runs on | Motion data | Logging a dose | Feedback |
| --- | --- | --- | --- | --- |
| `--source myo` | Laptop | Myo accelerometer, about 50 readings/s (confirm the real rate) | Fist squeeze held 1.5 s, or Enter | Short buzz on the band |
| `--source grove` | Pi Zero 2W | Grove ±1.5g MMA7660, 120 readings/s (the existing I2C code) | Button on D5 (`--button-pin 5`), or Enter | Beep on D16 (`--buzzer-pin 16`) |
| `--source simulate` | Laptop | Fake 5.2 Hz tremor | Enter | None |

**`myo_source.py` (your friend)**

- Turn the working Myo script into a class with `start()` and `stop()`.
- Callbacks for motion samples, as `(time, ax, ay, az)` in g, and for muscle samples, as `(time, emg[8])`.
- **Timestamps:** Bluetooth delivers samples in bursts, so arrival times are uneven. Timestamp motion samples by counting at the band's fixed rate from the first sample, and re-sync to the laptop clock if they drift more than 0.5 s apart.
- `vibrate("short")` and `vibrate("long")`.
- Reconnect automatically if the band drops, with a clear message.

**Squeeze to log a dose (you)**

- **Calibrate:** `--calibrate` records 5 s of a relaxed arm, then 3 squeezes. It measures muscle activity as the RMS across all 8 channels in 200 ms windows, sets the threshold halfway between relaxed and squeezing, and saves it to `myo_calibration.json`.
- **Detect:** log a dose when activity stays above the threshold for at least 1.5 s. Then ignore squeezes for 10 s and give a short buzz.

**Settings per source**

- **Myo:** 128-sample window (about 2.6 s at 50/s), hop 64, `--min-rms 0.015`, device ID `tremor`.
- **Grove:** `--rate 120`, 256-sample window, hop 128, `--min-rms 0.04`, device ID `tremor-pi`.
- Keep the existing feature math exactly as it is: detrending, Hann window, per-axis power spectra added together, peak between 3 and 12 Hz, `band_ratio` and `rms_g`.

**Muscle confirmation (only after the basics work)**

- For each analysis window, rectify and smooth the EMG, then find its strongest rhythm between 3 and 12 Hz. If that's within 1 Hz of the motion's `peak_hz`, mark the window `emg_confirmed`.
- Add the columns: `ALTER TABLE tremor_features ADD COLUMN emg_peak_hz REAL, ADD COLUMN emg_confirmed BOOLEAN;`
- On the dashboard's Live panel, show a "Muscle-confirmed" badge.

**Done when:**

- `--selftest` still passes. 5.0 Hz at 0.10 g reads about 4.99 Hz and 0.071 g. 8.3 Hz at 0.05 g reads about 8.30 Hz and 0.035 g.
- `--source myo --dry-run`: a still arm shows no TREMOR. Shaking about 4 to 5 times a second shows TREMOR at a matching Hz. The measured rate is near 50.
- A 2-second squeeze logs a dose and the band buzzes, but ordinary hand movements don't.
- On the Pi, `--source grove --rate 120 --min-rms 0.04 --button-pin 5` still works like today's test.
- `--source simulate` still drives the dashboard.

```text
For your friend:
Read @BUILD_SPEC.md "Global rules for Cursor" and "Step 4". Turn @<the working Myo script> into
myo_source.py as described in "myo_source.py": a class with start/stop, callbacks for motion samples (in g)
and muscle samples, sample-count timestamps with re-sync, vibrate(), and auto-reconnect. Add a small test
that prints the measured motion rate and the muscle activity level for 10 seconds.
```

```text
For you:
Read @BUILD_SPEC.md "Global rules for Cursor" and "Step 4". Rename tremor_pi.py to tremor_ingest.py and add
--source myo|grove|simulate as in the table. Wire --source myo to myo_source.py, add --calibrate and
squeeze-to-log-dose, add --buzzer-pin for the grove source, and apply the settings per source. Keep the
feature math, writer thread, Enter-to-log, --selftest, --dry-run and --log-csv exactly as they are. Run
--selftest, then tell me how to run each source.
```

```text
After both work:
Read @BUILD_SPEC.md "Step 4", "Muscle confirmation". Implement it in tremor_ingest.py, give me the
ALTER TABLE to run in Tiger Data, and add the "Muscle-confirmed" badge to the Live panel in dashboard.py.
```

## Step 5: Gemini report

`gemini_report.py` turns the past 7 days into a one-page doctor-visit summary. The dashboard's button calls it.

**What goes to Gemini** (compact, built with `db.py`):

- The hourly table as CSV text: date, hour, `avg_rms_g`, `frac_tremor`, `avg_peak_hz`. About 110 rows.
- Dose times for each day, in local time.
- The before/after-dose rows and the average change.
- A note on the data source: a wrist accelerometer measuring rest tremor, in g and Hz.

**What comes back** (Markdown):

1. Summary (3 sentences)
2. Daily pattern
3. Medication timing, including missed or late doses it spots from gaps
4. Notable events
5. Questions to ask your doctor (3 to 5)

**Guardrails** (system instruction):

- Describe patterns only. No diagnosis, and no advice on medication or doses.
- Write "the data suggests", never "you have".
- Plain language for the patient, exact numbers for the clinician.
- State that the data comes from a monitoring prototype.

**SDK and model:**

- Use `google-genai` with the model from `GEMINI_MODEL`, default `gemini-3.8-flash` (the newest Flash model in Google's docs as of Nov 2026).
- If that model isn't found, try `gemini-3.7-flash`, then `gemini-2.5-flash`.
- Temperature about 0.3, a timeout, and a friendly error message.

**CLI:** `python gemini_report.py --days 7` writes `report.md` and prints it.

**Dashboard:** the button shows a spinner, renders the report, and offers it as a `.md` download.

**Done when:** without being told, the report mentions the missed afternoon dose 3 days ago and the late dose 5 days ago, and it contains no diagnosis or medication advice.

```text
Read @BUILD_SPEC.md "Global rules for Cursor" and "Step 5: Gemini report".
Write gemini_report.py: a build_report(conn, device, days) function that gathers the inputs with db.py,
calls Gemini with the guardrail system instruction and the model fallback order, and returns Markdown with
the five sections. Add the CLI. Then wire the dashboard's "Generate doctor report" button to it, with a
spinner, rendered Markdown and a download button. Keep the report in st.session_state so reruns don't
regenerate it.
```

## Step 6: Hardware setup and tuning

The Myo is the main sensor and needs only charging, a snug fit and a one-time calibration. The Pi with its Grove HAT is already set up from today's test and stays ready as the backup.

**Myo armband**

1. Charge it over micro-USB before the demo.
2. Wear it snug on the thickest part of the forearm, in the same spot every time. The muscle thresholds depend on where it sits.
3. Make sure nothing else is connected to it, such as another laptop or app.
4. Run `python tremor_ingest.py --source myo --calibrate`: relax for 5 s, then squeeze three times.
5. Run `python tremor_ingest.py --source myo --dry-run`, then tune the settings below.
6. Keep the laptop within a few meters of the band.

**Pi backup** (Grove HAT: the ±1.5g accelerometer in an I2C port, the button on D5, the buzzer on D16)

1. Copy the renamed script over: `scp tremor_ingest.py pi@tremor.local:~`.
2. On the Pi, run `export TIGER_URL='...'`.
3. Run `python3 tremor_ingest.py --source grove --rate 120 --min-rms 0.04 --button-pin 5 --buzzer-pin 16`.
4. Its data lands under the device ID `tremor-pi`. Switch to it with the dashboard's device selector.
5. Before unplugging, always run `sudo shutdown now`.

**Tuning**

- **Resting threshold:** with your arm resting, note the `g` readings in `--dry-run`. Then set `--min-rms` between that and what a deliberate shake gives. The Myo should pick up much milder shakes than the Grove sensor.
- **Frequency check:** shake along with a metronome app at 240 bpm, one back-and-forth per beat. That's 4 Hz, and the reading should say about 4 Hz.
- **Squeeze check:** wave your hand, pick up a cup and type a little. None of those should log a dose. If one does, re-run `--calibrate` or raise the hold time.

**Done when:** a shake while wearing the Myo shows up on the dashboard's Live panel within about 3 s, a squeeze logs a dose with a buzz, and the Pi backup logs doses with its button.

## Step 7: Hardening and prize add-ons

Start these only after Steps 0 to 6 work. Each one adds a prize angle in about an hour or less. If you're behind, cut them in this order: Vultr, then the servo rig, then the extra security.

| Add-on | Prize | What to do | Time |
| --- | --- | --- | --- |
| Grok Bot automation and Cursor write-up | SpaceXSI raffle | Ask the MLH/SpaceXSI table what counts as a Grok Bot automation and how to get access. Candidate: a bot that sends a caregiver the daily report, or one that reviews your repo's changes. Keep notes and screenshots of how Cursor built each step | 30 min |
| Security pass | Most Secure Hack | Give the ingest script (tremor\_ingest.py) a database role that can only INSERT into the 3 tables, and the dashboard a read-only role. Require SSL. Parameterized SQL only. No secrets in the repo. Add a threat model to the README (device spoofing, health data at rest, leaked keys) | 45 min |
| Domain | GoDaddy Registry | Register a domain and point it at the dashboard | 15 min |
| Hosting | Vultr | Run the dashboard on a Vultr instance behind your domain, with HTTPS (for example through Caddy) | 60 min |
| Servo validation rig | Best Hardware (credibility) | A Grove servo shakes the sensor at 3, 4 and 5 Hz. Show a small table of set vs measured frequency | 60 min |
| DEV post | DEV Writing Challenge | Write up the build after the event | After |
| Rest vs posture test | Best MedTech, Most Impactful AI | A guided 40 s test: 20 s with the arm resting, then 20 s held straight out. Compare strength and Hz between the two. Parkinson's tremor tends to be strongest at rest, essential tremor when holding a posture. Show it as a pattern, never a diagnosis | 60 min |
| Proactive reminders | Best Agentic Hack (Sentient OS) | An agent loop that checks Tiger Data every few minutes. If tremor has been high for 30 min and no dose was logged in 4 h, it gives the band a long buzz and drafts a caregiver message for approval | 2 h |
| Accessibility write-up | Best Accessibility | No buttons or touchscreen: a squeeze logs a dose and a silent buzz confirms it. Explain in the README and pitch why that matters for hands that tremble | 20 min |

```text
Read @BUILD_SPEC.md "Step 7: Hardening and prize add-ons", the "Security pass" row.
Write security.sql that creates two Tiger Data roles: tremor_writer (INSERT only on accel_raw,
tremor_features, med_events) and tremor_reader (SELECT only, including tremor_hourly). Update .env.example so
the ingest script uses the writer URL and the dashboard uses the reader URL. Add a "Security" section to README.md with a
short threat model and what we did about each threat.
```

## Step 8: Demo and submission

Stop adding features about 6 hours before the deadline. Spend the rest on a backup video, the Devpost page and pitch practice, and submit an hour early.

**Before judging**

- [ ] Run `seed_week.py --wipe` so the simulated week ends at demo time.
- [ ] Record a 2-minute backup video of the full demo, in case the Wi-Fi fails at the table.
- [ ] Write the Devpost page: the problem, what it does, how it works (with an architecture picture), which Tiger Data features you used (hypertables, compression ratio, continuous aggregate, real-time aggregation), how you used Gemini, a paragraph on how you used Cursor and Grok Bot, challenges, and what's next.
- [ ] Opt into every prize you qualify for, including the SpaceXSI raffle.
- [ ] Check the public repo has no secrets in it, including old commits.
- [ ] Submit an hour early.

**2-minute pitch**

1. **Hook (15 s):** a clinic visit is a snapshot, but tremor changes all day with medication timing.
2. **Live demo (30 s):** put on the armband, shake your hand, and watch the Live panel react.
3. **The dose (20 s):** squeeze your fist for 2 seconds, feel the band buzz, and show the dose marker. In the week view, tremor drops after each dose and creeps back as it wears off.
4. **The catch (20 s):** point to the missed afternoon dose on the heatmap, then generate the Gemini report and show that it found it on its own.
5. **Under the hood (20 s):** Tiger Data's compression ratio and continuous aggregates, and how the frequency analysis turns raw readings into Hz and g, and how muscle signals confirm a real tremor.
6. **Close (15 s):** "History is simulated for the demo, live data is real." The same analysis also runs on a Raspberry Pi with a basic sensor, for about $30. Next steps: clinical validation and caregiver alerts.
