"""Provision a model-specific HNSW index with migration credentials.

The embedding model identifier and dimension are deployment inputs, never
application model defaults. Run outside serving traffic; index build is concurrent.
"""

import argparse
import hashlib

from psycopg import connect, sql

from creditpaper.db import _build_dsn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--dimensions", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.dimensions <= 2000 or not 1 <= len(args.model) <= 200:
        parser.error("HNSW vector dimensions must be 1..2000 and model identifier 1..200 characters")
    with connect(_build_dsn(), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(
            "ALTER TABLE ledger_embeddings ADD COLUMN IF NOT EXISTS embedding_vector vector "
            "GENERATED ALWAYS AS (embedding::vector) STORED"
        )
        name = "ix_embedding_" + hashlib.sha256(args.model.encode()).hexdigest()[:16]
        conn.execute(
            sql.SQL("""CREATE INDEX CONCURRENTLY IF NOT EXISTS {} ON ledger_embeddings
            USING hnsw ((embedding_vector::vector({})) vector_cosine_ops)
            WHERE model={} AND dimensions={} """).format(
                sql.Identifier(name),
                sql.Literal(args.dimensions),
                sql.Literal(args.model),
                sql.Literal(args.dimensions),
            )
        )
        conn.execute("ANALYZE ledger_embeddings")
    print("Vector index configured")


if __name__ == "__main__":
    main()
