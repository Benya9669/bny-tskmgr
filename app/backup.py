from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from app.server import DB_PATH


def create_backup(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Database not found: {source}")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing backup: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_db, closing(sqlite3.connect(destination)) as backup_db:
        source_db.backup(backup_db)
        result = backup_db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent TaskFlow SQLite backup")
    parser.add_argument("output", type=Path, help="New backup file; an existing file is never overwritten")
    parser.add_argument("--source", type=Path, default=DB_PATH, help="Source database path")
    args = parser.parse_args()
    create_backup(args.source.resolve(), args.output.resolve())
    print(f"Backup created and verified: {args.output.resolve()}")


if __name__ == "__main__":
    main()
