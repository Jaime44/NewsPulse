from __future__ import annotations

import sqlite3

from pathlib import Path


class Database:
    """Manage connections and schema initialization for NewsPulse."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        """Open a configured SQLite connection."""

        connection = sqlite3.connect(
            self.path,
            timeout=5,
        )

        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    def initialize(self) -> None:
        """Create the local database and required tables."""

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_credentials (
                    id INTEGER PRIMARY KEY
                        CHECK (id = 1),

                    email TEXT NOT NULL,

                    credentials_encrypted BLOB NOT NULL,

                    revoked INTEGER NOT NULL DEFAULT 0
                        CHECK (revoked IN (0, 1)),

                    connected_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )