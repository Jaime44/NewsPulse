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

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_state (
                    account_id INTEGER PRIMARY KEY
                        CHECK (account_id = 1),

                    started_at TEXT NOT NULL,
                    last_successful_scan_at TEXT,

                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,

                    FOREIGN KEY (account_id)
                        REFERENCES oauth_credentials (id)
                        ON DELETE CASCADE
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS gmail_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    account_id INTEGER NOT NULL
                        CHECK (account_id = 1),

                    gmail_message_id TEXT NOT NULL,
                    gmail_thread_id TEXT NOT NULL,

                    internal_date_ms INTEGER NOT NULL
                        CHECK (internal_date_ms >= 0),

                    status TEXT NOT NULL DEFAULT 'discovered'
                        CHECK (
                            status IN (
                                'discovered',
                                'processing',
                                'processed',
                                'skipped',
                                'failed'
                            )
                        ),

                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    processed_at TEXT,
                    last_error_code TEXT,

                    UNIQUE (account_id, gmail_message_id),

                    FOREIGN KEY (account_id)
                        REFERENCES oauth_credentials (id)
                        ON DELETE CASCADE
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_gmail_messages_account_status
                ON gmail_messages (account_id, status)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_gmail_messages_account_date
                ON gmail_messages (
                    account_id,
                    internal_date_ms
                )
                """
            )
