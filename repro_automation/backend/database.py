from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import URL, create_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)


def database_url():
    """Build the same PostgreSQL connection used by repro_download.py."""
    return URL.create(
        "postgresql+psycopg",
        username=os.getenv("REPRO_PG_USER", "postgres"),
        password=os.getenv("REPRO_PG_PASSWORD", ""),
        host=os.getenv("REPRO_PG_HOST", "localhost"),
        port=int(os.getenv("REPRO_PG_PORT", "5432")),
        database=os.getenv("REPRO_PG_DATABASE", "repro_reports"),
    )


engine = create_engine(database_url(), pool_pre_ping=True, future=True)
