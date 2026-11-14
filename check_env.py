#!/usr/bin/env python3
"""Confirm Tiger Data and Gemini credentials from the environment."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

REQUIRED = ("TIGER_URL", "GEMINI_API_KEY", "GEMINI_MODEL")
PROMPT = "Reply with the word ready"
ENV_PATH = Path(__file__).resolve().parent / ".env"


def fail(message: str) -> NoReturn:
    """Print an error and exit. The message must not contain a secret."""
    print(message, file=sys.stderr)
    raise SystemExit(1)


def redact(message: str, *secrets: str) -> str:
    """Return one line of an error with any secret values removed."""
    cleaned = " ".join(message.split())
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def load_settings() -> dict[str, str]:
    """Load .env from the project folder and return the required settings."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        fail("python-dotenv is not installed. Run: pip install -r requirements.txt")

    if not ENV_PATH.is_file():
        fail(
            f"No .env file in {ENV_PATH.parent}. "
            "Copy .env.example to .env and fill in the values."
        )
    # override: an empty TIGER_URL already in the environment must not hide .env
    load_dotenv(ENV_PATH, override=True)
    missing = [name for name in REQUIRED if not os.environ.get(name, "").strip()]
    if missing:
        names = ", ".join(missing)
        fail(f"Missing {names} in {ENV_PATH}. Fill in the values and save the file.")

    return {
        "url": os.environ["TIGER_URL"].strip(),
        "api_key": os.environ["GEMINI_API_KEY"].strip(),
        "model": os.environ["GEMINI_MODEL"].strip(),
    }


def check_database(url: str) -> None:
    """Connect to Tiger Data and print the Postgres and TimescaleDB versions."""
    try:
        import psycopg2
    except ImportError:
        fail("psycopg2 is not installed. Run: pip install -r requirements.txt")

    try:
        conn = psycopg2.connect(url, connect_timeout=60000)
    except psycopg2.Error as exc:
        fail(
            "Could not connect to Tiger Data. Check TIGER_URL "
            f"(it should end in sslmode=require). ({redact(str(exc), url)})"
        )

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            pg_version = cur.fetchone()[0]
            cur.execute(
                "SELECT extversion FROM pg_extension WHERE extname = %s",
                ("timescaledb",),
            )
            row = cur.fetchone()
    except psycopg2.Error as exc:
        fail(f"Connected, but the version query failed. ({redact(str(exc), url)})")
    finally:
        conn.close()

    if row is None or not row[0]:
        fail("Connected to Postgres, but the timescaledb extension is not installed.")

    print(f"Postgres: {pg_version}", flush=True)
    print(f"TimescaleDB: {row[0]}", flush=True)


def check_gemini(api_key: str, model: str) -> None:
    """Ask Gemini to reply with the word ready and print that reply."""
    try:
        from google import genai
    except ImportError:
        fail("google-genai is not installed. Run: pip install -r requirements.txt")

    try:
        client = genai.Client(api_key=api_key, http_options={"timeout": 30000})
        response = client.models.generate_content(model=model, contents=PROMPT)
    except Exception as exc:
        fail(
            "Gemini call failed. Check GEMINI_API_KEY and GEMINI_MODEL. "
            f"({redact(str(exc), api_key)})"
        )

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        fail("Gemini returned an empty reply. Check GEMINI_MODEL.")
    print(f"Gemini: {text}", flush=True)


def main() -> None:
    """Load .env, check Tiger Data, then check Gemini."""
    settings = load_settings()
    check_database(settings["url"])
    check_gemini(settings["api_key"], settings["model"])


if __name__ == "__main__":
    main()
