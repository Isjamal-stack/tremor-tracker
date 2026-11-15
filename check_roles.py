import os
from dotenv import load_dotenv
import psycopg2
load_dotenv(".env")
def check(name, sql, ok):
    conn = psycopg2.connect(os.environ[name], connect_timeout=15)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.rollback()
        print(name, "allowed" if ok else "UNEXPECTEDLY ALLOWED")
    except psycopg2.Error as exc:
        conn.rollback()
        print(name, exc.pgcode if not ok else f"UNEXPECTED {exc.pgcode}")
    finally:
        conn.close()
check("TIGER_WRITER_URL", "INSERT INTO med_events (time, device_id) VALUES (now(), 'tremor')", True)
check("TIGER_WRITER_URL", "SELECT count(*) FROM med_events", False)
check("TIGER_READER_URL", "SELECT count(*) FROM tremor_hourly", True)
check("TIGER_READER_URL", "INSERT INTO med_events (time, device_id) VALUES (now(), 'tremor')", False)