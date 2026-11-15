-- Least-privilege roles for the tremor monitoring prototype.
-- Run once as the Tiger Data admin, after schema.sql:
--
--   psql "$TIGER_URL" -f security.sql
--
-- Replace the two password placeholders before running.
-- A password should not contain $writer$ or $reader$.
-- Do not commit this file with real passwords in it.
-- Client URLs must end in sslmode=require. This service has no
-- pg_hba file we can edit, so SSL is required on every connection string.

DO $roles$
DECLARE
    writer_password text := $writer$REPLACE_WRITER_PASSWORD$writer$;
    reader_password text := $reader$REPLACE_READER_PASSWORD$reader$;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tremor_writer') THEN
        IF left(writer_password, 8) = 'REPLACE_' THEN
            RAISE EXCEPTION
                'Replace the tremor_writer password in security.sql before running. Do not commit the password.';
        END IF;
        EXECUTE format(
            'CREATE ROLE tremor_writer WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L',
            writer_password
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tremor_reader') THEN
        IF left(reader_password, 8) = 'REPLACE_' THEN
            RAISE EXCEPTION
                'Replace the tremor_reader password in security.sql before running. Do not commit the password.';
        END IF;
        EXECUTE format(
            'CREATE ROLE tremor_reader WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L',
            reader_password
        );
    END IF;
END
$roles$;

DO $connect$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO tremor_writer, tremor_reader',
        current_database()
    );
END
$connect$;

GRANT USAGE ON SCHEMA public TO tremor_writer, tremor_reader;
REVOKE CREATE ON SCHEMA public FROM tremor_writer, tremor_reader;

-- Writer: insert into the three tables. No select, update, or delete.
REVOKE ALL ON TABLE accel_raw, tremor_features, med_events FROM tremor_writer;
GRANT INSERT ON TABLE accel_raw, tremor_features, med_events TO tremor_writer;
REVOKE ALL ON tremor_hourly FROM tremor_writer;

-- Reader: select on the three tables and the hourly rollup.
REVOKE ALL ON TABLE accel_raw, tremor_features, med_events FROM tremor_reader;
GRANT SELECT ON TABLE accel_raw, tremor_features, med_events TO tremor_reader;
REVOKE ALL ON tremor_hourly FROM tremor_reader;
GRANT SELECT ON tremor_hourly TO tremor_reader;

-- Existing chunks. Timescale copies the parent grants onto chunks it creates later.
DO $chunks$
DECLARE
    parent regclass;
    chunk regclass;
BEGIN
    FOR parent IN
        SELECT unnest(ARRAY['accel_raw'::regclass, 'tremor_features'::regclass])
    LOOP
        FOR chunk IN SELECT show_chunks(parent)
        LOOP
            EXECUTE format(
                'REVOKE ALL ON TABLE %s FROM tremor_writer, tremor_reader',
                chunk
            );
            EXECUTE format('GRANT INSERT ON TABLE %s TO tremor_writer', chunk);
            EXECUTE format('GRANT SELECT ON TABLE %s TO tremor_reader', chunk);
        END LOOP;
    END LOOP;
END
$chunks$;
