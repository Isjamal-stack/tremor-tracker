# Gait & heart rate (ankle device)

An add-on to Tremor Tracker. A Raspberry Pi Zero 2 W worn at the ankle measures walking, flags possible freezing of gait, plays a cue beat on its buzzer, and reads heart rate. All computing happens on the Pi. Bluetooth LE carries the results to the laptop, which keeps the history. This is a monitoring prototype, not a medical device.

## Hardware
| Part | Connection |
| --- | --- |
| Grove MMA7660 accelerometer | I2C bus 1, address 0x4C |
| MAX30102 pulse sensor | I2C 0x57. VIN pin 1, SDA pin 3, SCL pin 5, GND pin 6, INT pin 11 (GPIO17) |
| Buzzer | GPIO16 (D16). Cues use a 3300/3800 Hz warble, its loudest sound |

## How it works
1. **Stage 1, freeze and tremor gate.** The zero-mean acceleration magnitude is cut into 2.5 s windows with 50% overlap. The freeze index is the 3–8 Hz power divided by the 0.5–3 Hz power. Each window is rest, walk, fog (index above 2 just after walking) or tremor (index above 2 with no walking).
2. **Stage 2, strides.** Strides are counted only in walk windows. The signal goes through a Mexican-hat wavelet at 1–2 Hz. Peaks must clear 0.6 × the rolling std and be 600 ms apart. Each peak is then moved to the sharpest impact nearby. One ankle sees one stride per landing, so steps = 2 × strides.
3. **Model.** A softmax regression on 9 window features (`gait_model.json`). It's trained on the laptop and run by the Pi.
4. **Cueing.** After 1 s of possible freeze, the buzzer plays a beat at the walker's learned cadence. It stops after 3 s of walking or rest.
5. **Heart rate.** The MAX30102's infrared channel, baseline removed, using the strongest 0.7–3.5 Hz peak over 8 s.

## Results so far
| Test | Result |
| --- | --- |
| Stride detection, 37 simulated sessions | F1 0.94, count error about 1 stride |
| Activity model, 3 real acted trials, leave-one-trial-out | 74% (67%, 82%, 73%) |
| Pi analysis time, 12 s of data | 0.06 s |

The real trials were acted by a healthy volunteer.

## Run it
On the Pi (Bluetooth unblocked, `bless` in `~/gaitenv`):
```bash
~/gaitenv/bin/python gait_ble.py               # advertises as "Tremor Tracker"
python3 gait_trial.py --phased 3               # record labelled training trials
```
On the laptop:
```bash
python gait_bridge.py                          # Bluetooth -> data/live, data/history
python gait_bridge.py --sim                    # no hardware: simulated walking
streamlit run gait_dashboard.py                # or the "Gait and heart rate" page in dashboard.py
python gait_model.py train data/sim/*.csv data/real/*.csv
python gait_algo.py data/sim/*.csv --method cwt
python gait_sim.py --dataset 36 --outdir data/sim
```

## Files
| File | What it does |
| --- | --- |
| `gait_engine.py` | Sampling, detection, cueing and heart rate (Pi and simulator) |
| `gait_ble.py` | BLE peripheral: status, accel and control characteristics |
| `gait_bridge.py` | Laptop BLE client. Writes live data and history, forwards commands |
| `gait_dashboard.py`, `pages/` | Streamlit page: live, controls, history, trials |
| `gait_algo.py` | Two-stage detector and scoring |
| `gait_model.py` | Tiny activity model: train, evaluate, predict |
| `gait_sim.py` | Simulated ankle data with ground truth |
| `gait_trial.py` | Guided trial recorder with buzzer cues |
| `gait_cue.py` | Standalone tilt, metronome and gait modes |
| `heart_rate.py` | MAX30102 driver and heart-rate estimate |
