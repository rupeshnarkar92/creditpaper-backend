"""The one rule for which dotenv file a process reads.

`APP_ENV=local` reads `.env.local`; anything else reads `.env`. The API settings
(`auth/config.py`), the raw psycopg pool (`db.py`) and Alembic (`alembic/env.py`) all
resolve the file here, so the three can never read different databases.

The path is anchored at the project root, not the working directory, so a worker or
Alembic started from another folder still reads the same file. Variables already set
in the process environment win over the file.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def env_file_name(app_env: str | None = None) -> str:
    value = os.getenv("APP_ENV") if app_env is None else app_env
    return ".env.local" if value == "local" else ".env"


def env_file_path(app_env: str | None = None) -> Path:
    return PROJECT_ROOT / env_file_name(app_env)
