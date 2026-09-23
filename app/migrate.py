from pathlib import Path

from app.db import connect


def migrate() -> None:
    root = Path(__file__).resolve().parent.parent
    with connect() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        for path in sorted((root / "migrations").glob("[0-9]*.sql")):
            version = int(path.name.split("_", 1)[0])
            if connection.execute("SELECT 1 FROM schema_migrations WHERE version=%s", (version,)).fetchone():
                continue
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (version,))


if __name__ == "__main__":
    migrate()
