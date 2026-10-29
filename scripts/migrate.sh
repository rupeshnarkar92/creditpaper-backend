#!/bin/bash
# Apply pending Alembic migrations
# Usage: ./scripts/migrate.sh
set -e
cd "$(dirname "$0")/.."
uv run alembic upgrade head
