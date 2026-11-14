-- Tremor Tracker schema for Tiger Data
-- Run once: paste into the Tiger Console SQL editor, or  psql "$TIGER_URL" -f schema.sql
--
-- If Tiger Data says the compression functions are deprecated, the newer names are
-- add_columnstore_policy / convert_to_columnstore. Same idea.

-- 1. Raw accelerometer samples (100 per second per device) ---------------------------
CREATE TABLE IF NOT EXISTS accel_raw (
    time      TIMESTAMPTZ NOT NULL,
    device_id TEXT        NOT NULL,
    ax REAL, ay REAL, az REAL
);
-- 1-hour chunks so compression actually kicks in during the hackathon (default is 7 days)
SELECT create_hypertable('accel_raw', by_range('time', INTERVAL '1 hour'), if_not_exists => TRUE);

-- 2. Tremor results, one row per ~1.3 s analysis window ------------------------------
CREATE TABLE IF NOT EXISTS tremor_features (
    time       TIMESTAMPTZ NOT NULL,
    device_id  TEXT        NOT NULL,
    peak_hz    REAL,       -- dominant frequency in the 3-12 Hz band
    rms_g      REAL,       -- tremor strength (RMS acceleration in the band), g
    band_ratio REAL,       -- share of fast motion that is in the band (0-1)
    tremor     BOOLEAN
);
SELECT create_hypertable('tremor_features', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE);

-- 3. Medication button presses (tiny, so a regular Postgres table) --------------------
CREATE TABLE IF NOT EXISTS med_events (
    time      TIMESTAMPTZ NOT NULL,
    device_id TEXT        NOT NULL,
    note      TEXT        DEFAULT 'dose'
);
CREATE INDEX IF NOT EXISTS med_events_dev_time ON med_events (device_id, time DESC);

-- 4. Compress raw data older than an hour ----------------------------------------------
ALTER TABLE accel_raw SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id',
    timescaledb.compress_orderby   = 'time DESC'
);
SELECT add_compression_policy('accel_raw', INTERVAL '1 hour', if_not_exists => TRUE);

-- 5. Hourly rollup (continuous aggregate): the dashboard and Gemini report read this --
CREATE MATERIALIZED VIEW IF NOT EXISTS tremor_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', time)          AS bucket,
       device_id,
       avg(rms_g)                                    AS avg_rms_g,
       max(rms_g)                                    AS max_rms_g,
       avg(CASE WHEN tremor THEN peak_hz END)        AS avg_peak_hz,
       avg(CASE WHEN tremor THEN 1.0 ELSE 0.0 END)   AS frac_tremor,
       count(*)                                      AS windows
FROM tremor_features
GROUP BY bucket, device_id
WITH NO DATA;

-- Queries also include recent rows not yet rolled up (real-time aggregation)
ALTER MATERIALIZED VIEW tremor_hourly SET (timescaledb.materialized_only = false);

SELECT add_continuous_aggregate_policy('tremor_hourly',
    start_offset      => INTERVAL '30 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists     => TRUE);


-- =====================================================================================
-- Demo queries (safe to run any time)
-- =====================================================================================

-- Compression ratio: show this to the Tiger Data judges
SELECT pg_size_pretty(before_compression_total_bytes) AS before,
       pg_size_pretty(after_compression_total_bytes)  AS after,
       round(before_compression_total_bytes::numeric
             / NULLIF(after_compression_total_bytes, 0), 1) AS ratio
FROM hypertable_compression_stats('accel_raw');

-- Compress finished chunks right now instead of waiting for the policy
SELECT compress_chunk(c, if_not_compressed => TRUE)
FROM show_chunks('accel_raw', older_than => INTERVAL '1 hour') c;

-- Last 24 hours, hour by hour
SELECT * FROM tremor_hourly
WHERE bucket > now() - INTERVAL '24 hours'
ORDER BY bucket;

-- The money shot: tremor in the 2 h before each dose vs 30 min to 2.5 h after
SELECT m.time AS dose_time,
       (SELECT avg(rms_g) FROM tremor_features f
         WHERE f.device_id = m.device_id
           AND f.time BETWEEN m.time - INTERVAL '2 hours' AND m.time)          AS before_rms_g,
       (SELECT avg(rms_g) FROM tremor_features f
         WHERE f.device_id = m.device_id
           AND f.time BETWEEN m.time + INTERVAL '30 minutes'
                          AND m.time + INTERVAL '2 hours 30 minutes')        AS after_rms_g
FROM med_events m
ORDER BY m.time DESC
LIMIT 10;
