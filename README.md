# Tremor Tracker

Tremor Tracker is a monitoring prototype that measures a resting hand's shake from a Myo armband, logs each dose with a fist squeeze, and shows the day and the week on a dashboard with a doctor-visit summary. An ankle device adds walking, freezing of gait, a cue beat, and heart rate, on the same page.

## The problem

A clinic visit is a snapshot. Tremor changes through the day, and it changes with medication timing. We record the tremor between visits and mark each dose, so those hours stay on one timeline.

## What it does

- Live frequency in Hz and strength in g.
- A dose from a 1.5 s fist squeeze, or from Enter.
- Confirmation from 8 muscle sensors.
- A day-by-hour week view.
- The 2 hours before each dose, versus 30 minutes to 2.5 hours after.
- A plain-language visit summary, plus an exact clinician table.
- Steps, cadence, and stride variability from an ankle sensor.
- Possible freezing of gait, answered by a cue beat on the buzzer.
- Heart rate from a MAX30102.
- Buzzer, cue beat, and detector settings from the dashboard, over Bluetooth.

<!-- screenshot: dashboard -->

## How it works

### Hardware

The Myo (an accelerometer and 8 muscle sensors) streams to the laptop over Bluetooth. The laptop does the analysis and the writes. The backup, and the low-cost version, is a Raspberry Pi Zero 2W with a Grove sensor, running the same script as `--source grove`. GPIO 5 logs a dose. GPIO 16 beeps. On I2C bus 1 the script accepts an ADXL345, an LIS3DH, or a 6-bit MMA7660. The MMA7660 misses mild tremor, so that path uses a 0.04 g floor.

### Ankle device
A Raspberry Pi Zero 2W at the ankle carries the Grove MMA7660 (I2C 0x4C), a MAX30102 pulse sensor (I2C 0x57, INT on GPIO 17), and the buzzer on GPIO 16. It advertises over Bluetooth LE as `Tremor Tracker`. The Pi does all the math. Bluetooth carries the results and the raw motion. The laptop stores and shows them.

The detector has two stages. Stage 1 cuts the acceleration magnitude into 2.5 s windows with 50% overlap. The freeze index is the 3 to 8 Hz power over the 0.5 to 3 Hz power. Above 2, right after walking, a window is a possible freeze. Above 2 with no walking, it is a leg tremor. Stage 2 counts strides only while walking, with a Mexican-hat wavelet at 1 to 2 Hz, a 600 ms lockout, and a threshold of 0.6 times the rolling spread. One ankle sees one stride per landing, so steps are twice the strides. A softmax model on 9 window features (`gait_model.json`) is trained on the laptop and runs on the Pi. After 1 s of a possible freeze, the buzzer plays a 3300/3800 Hz beat at the walker's own cadence, and it stops after 3 s of walking. Heart rate is the strongest 0.7 to 3.5 Hz rhythm in 8 s of the infrared channel. Details are in `GAIT.md`.
### Signal processing

The Myo sends 50 readings/s. A window is 128 samples (2.56 s), stepped every 64 (1.28 s). Grove uses 256 samples at 120 readings/s. We remove gravity, drift, and motion slower than 1.5 Hz. `rms_g` is the 3 to 12 Hz strength, in g. `band_ratio` is the share of that faster motion inside the band. `peak_share` is the share of the band within 1 Hz of the peak. A steady tremor is narrow. A jerk spreads out, so `peak_share` stays low.

A Myo window passes at 0.015 g, `band_ratio` 0.5, and `peak_share` 0.3. `tremor` is set after 3 passing windows in a row. Grove and simulate use `peak_share` 0.45 and 2 windows.

Calibration is 5 s relaxed, then 3 squeezes of 2 s. Activity is the RMS of 8 channels in 200 ms blocks, averaged over 0.5 s. The threshold is 60% of the way from relaxed to squeezing, saved in `myo_calibration.json`. A dose needs a 1.5 s hold, still for at least 80% of it. Still means motion under 0.05 g, or under the calibration limit (the greater of 0.05 g and twice the squeeze motion). One short buzz. The next squeeze waits out a 10 s lockout and a 1 s release.

Muscle confirmation runs only on tremor windows. The channels stream at 200 Hz, then are rectified and smoothed. A peak within 1 Hz of the motion peak, holding at least 0.3 of the muscle band, is muscle-confirmed. Live now shows a badge. `--selftest` accepts 5.0 Hz at 0.10 g and 8.3 Hz at 0.05 g within 0.3 Hz, and rejects a 1 s jerk. It needs no sensor or database.

### Data

Times are UTC `timestamptz`, shown in `America/New_York`. `accel_raw` and `tremor_features` are hypertables. Raw chunks are 1 hour and compress after 1 hour, segmented by `device_id`. Feature chunks are 1 day. `med_events` is a normal table (`note` defaults to `dose`). `tremor_hourly` is one row per device per hour: average and max strength, average tremor frequency, the tremor fraction, and the window count. It refreshes every 5 minutes, from 30 days ago to 1 hour ago. Real-time aggregation includes the newest hour.

### The report

Python does the math. Gemini writes the prose at temperature 0.3, using `GEMINI_MODEL`. `.env.example` sets `gemini-3.5-flash`. If that model is not found, the report tries `gemini-3.7-flash`, then `gemini-2.5-flash`. The headings are Summary (3 sentences), Daily pattern, Medication timing, Notable events, and Questions to ask your doctor (3 to 5). The model is told to describe patterns, to write "the data suggests", and to call this a monitoring prototype. Python adds the clinician table: day, doses logged, missed or late doses, average strength (g), and average frequency (Hz).

<!-- screenshot: doctor report -->

## Setup

Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Python 3.13 the last requirements line, `dl-myo` 1.1.8, fails. It pins a Bleak release with no macOS wheel. The comment in `requirements.txt` is the fix:

```bash
pip install "bleak==0.22.3"
pip install --no-deps "dl-myo==1.1.8"
```

Copy `.env.example` to `.env`. Fill in `TIGER_URL`, `TIGER_WRITER_URL`, `TIGER_READER_URL`, and `GEMINI_API_KEY`. Every URL ends in `sslmode=require`.

```bash
python check_env.py
psql "$TIGER_URL" -f schema.sql
psql "$TIGER_URL" -c "ALTER TABLE tremor_features ADD COLUMN emg_peak_hz REAL, ADD COLUMN emg_confirmed BOOLEAN;"
psql "$TIGER_URL" -f security.sql
python seed_week.py --dry-run
python seed_week.py --wipe
```

`check_env.py` prints the Postgres version, the TimescaleDB version, and Gemini's one-line reply. The `ALTER` adds `emg_peak_hz` and `emg_confirmed`. Replace both passwords in `security.sql` before that psql command. A password should not contain `$writer$` or `$reader$`. Then put the role URLs in `.env`.

The seed runs 07:00 to 22:30 local, with doses near 07:30, 11:30, 15:30, and 19:30, one window every 1.28 s, ending 10 minutes ago. The 15:30 dose is missing 3 days ago. The 11:30 dose is 1 hour 45 minutes late 5 days ago. `--scale 1` matches the Myo. `--scale 3` matches the Pi. `--wipe` deletes that device's feature and dose rows first.

## Running it

```bash
python tremor_ingest.py --selftest
python tremor_ingest.py --source myo --calibrate
python tremor_ingest.py --source myo --dry-run
python tremor_ingest.py --source myo
python tremor_ingest.py --source simulate
python tremor_ingest.py --source grove
streamlit run dashboard.py
python gemini_report.py --days 7
```
The ankle device:
```bash
~/gaitenv/bin/python gait_ble.py              # on the Pi
python3 gait_trial.py --phased 3              # on the Pi: record labelled walking trials
python gait_bridge.py                         # on the laptop: Bluetooth to data/live and data/history
python gait_bridge.py --sim                   # no hardware: simulated walking
python gait_model.py train data/sim/*.csv data/real/*.csv
```
The Gait and heart rate section of `streamlit run dashboard.py` refreshes every second. It reads only local files, so it still loads without Tiger Data. Its controls go to the Pi over Bluetooth. The history is one row per second in `data/history/`, kept on the laptop.

Calibration is 5 seconds still, then three squeezes. It writes `myo_calibration.json`. Without that file, only Enter logs a dose. `--dry-run` writes nothing. Ctrl+C stops. Simulate is a 5.2 Hz tremor of about 0.10 g on device `tremor`. Grove is the Pi. Export `TIGER_WRITER_URL` there, use apt `python3-psycopg2`, and if I2C is missing run `sudo apt install -y python3-smbus`. Copy `tremor_ingest.py` over. Its device id is `tremor-pi`. Type that into Device ID. Run `sudo shutdown now` before unplugging.

Live now refreshes every 2 seconds, and shows Waiting for the wristband when the newest window is over 1 minute old. Today refreshes every 30 seconds. The week heatmap, the medication chart, and the `accel_raw` compression stats are on the same page. Generate doctor report matches the command above for the sidebar device and downloads `report.md`. The footer says the history is simulated and the live data comes from the wristband.

<!-- screenshot: week heatmap -->

## Main options

An explicit flag overrides the source default.

| Flag | Default | What it changes |
| --- | --- | --- |
| `--source` | required | `myo`, `grove`, or `simulate` |
| `--device` | `tremor`; `tremor-pi` on grove | `device_id` on each row |
| `--rate` | 50 Hz; 120 Hz on grove | Grove and simulate rate. Myo clock is 50 Hz |
| `--window` | 128; 256 on grove | Samples per window |
| `--hop` | 64; 128 on grove | Samples between windows |
| `--min-rms` | 0.015 g; 0.04 g on grove | Minimum strength |
| `--min-ratio` | 0.5 | Minimum `band_ratio` |
| `--min-peak-share` | 0.3 on myo; 0.45 otherwise | Share of 3-12 Hz power within 1 Hz of the peak |
| `--min-windows` | 3 on myo; 2 otherwise | Passing windows before `tremor` is true |
| `--dose-lockout` | 10 s | Wait before the next squeeze can log |
| `--still-g` | 0.05 g, or the calibration file | Still-hand limit |
| `--button-pin` | 5 | Grove button GPIO |
| `--buzzer-pin` | 16 | Grove buzzer GPIO |
| `--days` | 7 | Days for the seed and the report |
| `--scale` | 1 | Seeded strength. Use 3 for the Pi |
| `--seed` | 42 | Repeatable simulated week |

## Project structure

| File | What it does |
| --- | --- |
| `tremor_ingest.py` | Features, doses, database writes |
| `myo_source.py` | Bluetooth Myo: samples in, vibration out |
| `myo_test.py` | Motion rate and muscle level for 10 seconds |
| `dashboard.py` | Streamlit dashboard |
| `db.py` | Connection and queries |
| `gemini_report.py` | Visit summary. Writes `report.md` |
| `seed_week.py` | Simulated `tremor_features` and `med_events` |
| `schema.sql` | Tables, compression, `tremor_hourly` |
| `security.sql` | Insert-only writer, select-only reader |
| `check_env.py` | Checks Tiger Data and Gemini |
| `gait_engine.py` | Ankle device: sampling, detection, cueing, heart rate |
| `gait_ble.py` | Pi Bluetooth service `Tremor Tracker` |
| `gait_bridge.py` | Laptop Bluetooth client, history, and commands |
| `gait_dashboard.py` | Gait and heart rate section of the dashboard |
| `gait_algo.py` | Two-stage gait detector and scoring |
| `gait_model.py`, `gait_model.json` | Tiny activity model, trained on the laptop |
| `gait_sim.py` | Simulated ankle data with ground truth |
| `gait_trial.py` | Guided trial recorder with buzzer cues |
| `gait_cue.py` | Tilt, metronome, and gait modes from the terminal |
| `heart_rate.py` | MAX30102 driver and heart rate |
| `requirements.txt` | Python packages |
| `.env.example` | Variable names, blank secrets |

## Accessibility

A dose is logged without a button or a touchscreen. The wearer holds a fist squeeze for 1.5 seconds, and the Myo answers with a short, silent buzz on the wrist. Confirmation is felt, so it does not depend on looking at a screen or hearing a sound.

That matters for people whose hands tremble. Small buttons, phone taps, and touch targets are easy to miss when a hand is shaking. A whole-hand squeeze is a motion they can still make, and the buzz confirms the log without a second precise action.

The Raspberry Pi backup uses a large physical button. One press logs the dose and a beep confirms it, so the backup stays usable without a screen or fine aiming.

## Security

`security.sql` adds two database roles. Replace the two password placeholders and run the file once as the admin, in the Tiger console or with `psql "$TIGER_URL" -f security.sql`. Then fill the new URLs in `.env`. Every URL ends in `sslmode=require`. On the Pi, export the writer URL the same way.

`tremor_ingest.py` uses `TIGER_WRITER_URL` (role `tremor_writer`). That role can insert into `accel_raw`, `tremor_features`, and `med_events`. The dashboard and the doctor-visit summary use `TIGER_READER_URL` (role `tremor_reader`). That role can select from those tables and from `tremor_hourly`. `TIGER_URL` stays the admin connection for the schema, the seed, and `check_env.py`. If a split URL is empty, that script uses `TIGER_URL` until you set it.

**Device spoofing.** The writer picks the `device_id` on each insert, including `tremor`. Its only grant is insert, so rows already stored stay as they were. The dashboard connects as the reader, whose only grant is select.

**Health data at rest.** Samples, tremor features, and dose times stay in Tiger Data. The app URLs require SSL, so that data is encrypted on the wire. The writer role's grant is insert-only, which keeps the ingest password from reading the stored history.

**Leaked keys.** URLs and `GEMINI_API_KEY` come from the environment. `.env` is gitignored, and `.env.example` ships blank. The writer URL can insert. The reader URL can read. Query values are passed as parameters.

## Limitations

This is a monitoring prototype. It is not a medical device, and it has not been clinically validated. The demo week is simulated. The Myo is discontinued. The Pi's MMA7660 is coarse (6-bit, threshold 0.04 g). We tuned the thresholds on a few people. The gait model learned from simulated walks and from acted trials by one healthy volunteer. On held-out acted trials it is right 74% of the time. Stride detection scores F1 0.94 on 37 simulated walks. Heart rate needs a still fingertip on the sensor. Blood oxygen is not shown, because it needs calibration.

## What's next

- Clinical validation with more people.
- A caregiver alert when tremor stays high and no dose was logged.
- A rest-versus-outstretched-arm comparison, shown as a pattern only.
- Hosting the dashboard off the laptop.

## Built with

We built this in Cursor with Grok, and the ankle device with Kiro. The database is Tiger Data. The visit summary uses the Gemini API. The sensors are a Myo armband and a Raspberry Pi with an accelerometer and a MAX30102.
