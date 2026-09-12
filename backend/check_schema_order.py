"""Guard the schema against a SQLite-only foreign-key ordering.

SQLite does not resolve foreign-key targets at CREATE TABLE time, so a table
may reference one declared later and everything appears fine. PostgreSQL
resolves immediately and fails on first boot.

The original schema had exactly this: `scenes` referenced `assets` before
`assets` existed. Under SQLite it worked for months of local development. This
check makes that class of bug fail in CI instead of on the day of the
migration.

    python check_schema_order.py
"""
import re
import sys

from app import db

CREATE_TABLE = re.compile(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)", re.I)
REFERENCES = re.compile(r"REFERENCES\s+(\w+)\s*\(", re.I)


def main() -> int:
    declared: list[str] = []
    problems: list[str] = []

    for statement in db.SCHEMA:
        match = CREATE_TABLE.search(statement)
        if not match:
            continue
        table = match.group(1)
        for target in REFERENCES.findall(statement):
            # A self-reference is fine; anything else must already exist.
            if target != table and target not in declared:
                problems.append(
                    f"{table} references {target}, which is declared later."
                    f" PostgreSQL rejects this at CREATE TABLE time."
                )
        declared.append(table)

    if not declared:
        print("FAIL: no CREATE TABLE statements found; is db.SCHEMA still a list?")
        return 1

    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        print("\nReorder db.SCHEMA so every table is declared before it is used.")
        return 1

    print(f"ok  {len(declared)} tables declared in dependency order:"
          f" {', '.join(declared)}")

    # The dialect seam must actually rewrite both dialect-specific constructs,
    # or "portable SQL" is only portable by accident.
    probe = "SELECT * FROM t WHERE a = ? AND b = {now}"
    import app.config as config
    original = config.DB_DIALECT
    try:
        config.DB_DIALECT = "sqlite"
        sqlite_sql = db.sql(probe)
        assert "?" in sqlite_sql and "datetime('now')" in sqlite_sql, sqlite_sql
        config.DB_DIALECT = "postgres"
        pg_sql = db.sql(probe)
        assert "%s" in pg_sql and "now()" in pg_sql, pg_sql
        assert "?" not in pg_sql and "{now}" not in pg_sql, pg_sql
    finally:
        config.DB_DIALECT = original
    print("ok  dialect rewriting produces valid sqlite and postgres SQL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
