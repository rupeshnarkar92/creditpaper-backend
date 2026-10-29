"""Sync production schema to test database using raw SQL dump."""

# Use Python's subprocess to call pg_dump equivalent via psycopg
import psycopg

SRC = "postgresql://postgres:123456@localhost:5432/creditpaper"
DST = "postgresql://postgres:123456@localhost:5432/creditpaper_test"

src = psycopg.connect(SRC)
dst = psycopg.connect(DST)
cur_s = src.cursor()
cur_d = dst.cursor()

# Get all tables
cur_s.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
tables = [r[0] for r in cur_s.fetchall()]
print(f"Found {len(tables)} tables")

for t in tables:
    # Check if exists
    cur_d.execute("SELECT 1 FROM pg_tables WHERE tablename = %s AND schemaname = 'public'", (t,))
    if cur_d.fetchone():
        # Add missing columns
        cur_s.execute(
            """
            SELECT column_name, udt_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public'
            ORDER BY ordinal_position
        """,
            (t,),
        )
        cols = cur_s.fetchall()
        cur_d.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s AND table_schema = 'public'",
            (t,),
        )
        existing = {r[0] for r in cur_d.fetchall()}
        for name, udt, nullable, default in cols:
            if name not in existing:
                null_str = "" if nullable == "YES" else " NOT NULL"
                default_str = f" DEFAULT {default}" if default else ""
                pg_type = udt
                if pg_type == "int4":
                    pg_type = "INTEGER"
                elif pg_type == "int8":
                    pg_type = "BIGINT"
                elif pg_type == "text":
                    pg_type = "TEXT"
                elif pg_type == "varchar":
                    pg_type = "VARCHAR"
                elif pg_type == "bool":
                    pg_type = "BOOLEAN"
                elif pg_type == "jsonb":
                    pg_type = "JSONB"
                elif pg_type == "json":
                    pg_type = "JSON"
                elif pg_type == "timestamptz":
                    pg_type = "TIMESTAMPTZ"
                elif pg_type == "timestamp":
                    pg_type = "TIMESTAMP"
                elif pg_type == "uuid":
                    pg_type = "UUID"
                elif pg_type == "float8":
                    pg_type = "DOUBLE PRECISION"
                elif pg_type == "numeric":
                    pg_type = "NUMERIC"
                elif pg_type == "date":
                    pg_type = "DATE"
                elif pg_type == "bytea":
                    pg_type = "BYTEA"
                try:
                    cur_d.execute(f"ALTER TABLE {t} ADD COLUMN {name} {pg_type}{null_str}{default_str}")
                    print(f"  + {t}.{name}")
                except Exception as e:
                    print(f"  skip {t}.{name}: {e}")
        continue

    # Get full CREATE TABLE from production
    cur_s.execute(
        """
        SELECT column_name, udt_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = %s AND table_schema = 'public'
        ORDER BY ordinal_position
    """,
        (t,),
    )
    cols = cur_s.fetchall()
    col_defs = []
    for name, udt, nullable, default in cols:
        null_str = "" if nullable == "YES" else " NOT NULL"
        default_str = f" DEFAULT {default}" if default else ""
        pg_type = udt
        if pg_type == "int4":
            pg_type = "INTEGER"
        elif pg_type == "int8":
            pg_type = "BIGINT"
        elif pg_type == "text":
            pg_type = "TEXT"
        elif pg_type == "bool":
            pg_type = "BOOLEAN"
        elif pg_type == "jsonb":
            pg_type = "JSONB"
        elif pg_type == "json":
            pg_type = "JSON"
        elif pg_type == "timestamptz":
            pg_type = "TIMESTAMPTZ"
        elif pg_type == "timestamp":
            pg_type = "TIMESTAMP"
        elif pg_type == "uuid":
            pg_type = "UUID"
        elif pg_type == "float8":
            pg_type = "DOUBLE PRECISION"
        elif pg_type == "numeric":
            pg_type = "NUMERIC"
        elif pg_type == "date":
            pg_type = "DATE"
        col_defs.append(f"    {name} {pg_type}{null_str}{default_str}")
    ddl = f"CREATE TABLE {t} (\n" + ",\n".join(col_defs) + "\n)"
    try:
        cur_d.execute(ddl)
        print(f"  Created {t}")
    except Exception as e:
        print(f"  skip {t}: {e}")

# Copy primary keys
for t in tables:
    cur_s.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
    """,
        (t,),
    )
    pk_cols = [r[0] for r in cur_s.fetchall()]
    if pk_cols:
        try:
            cur_d.execute(f"ALTER TABLE {t} ADD CONSTRAINT pk_{t} PRIMARY KEY ({', '.join(pk_cols)})")
        except Exception:
            pass

# Copy unique indexes
for t in tables:
    cur_s.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE tablename = %s AND schemaname = 'public' AND indexname NOT LIKE '%%_pkey'
    """,
        (t,),
    )
    for _idx_name, idx_def in cur_s.fetchall():
        try:
            cur_d.execute(idx_def)
        except Exception:
            pass

dst.commit()
print("Done!")
src.close()
dst.close()
