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
