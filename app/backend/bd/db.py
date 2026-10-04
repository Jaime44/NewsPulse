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
                CREATE TABLE IF NOT EXISTS newsletter_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    account_id INTEGER NOT NULL
                        CHECK (account_id = 1),

                    source_type TEXT NOT NULL
                        CHECK (
                            source_type IN (
                                'sender',
                                'list_id',
                                'gmail_label',
                                'domain'
                            )
                        ),

                    source_value TEXT NOT NULL
                        CHECK (
                            length(trim(source_value)) > 0
                        ),

                    decision TEXT NOT NULL
                        CHECK (
                            decision IN (
                                'include',
                                'exclude'
                            )
                        ),

                    origin TEXT NOT NULL
                        CHECK (
                            origin IN (
                                'manual',
                                'confirmed',
                                'automatic'
                            )
                        ),

                    confidence INTEGER NOT NULL DEFAULT 100
                        CHECK (
                            confidence BETWEEN 0 AND 100
                        ),

                    active INTEGER NOT NULL DEFAULT 1
                        CHECK (active IN (0, 1)),

                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_matched_at TEXT,

                    UNIQUE (
                        account_id,
                        source_type,
                        source_value
                    ),

                    FOREIGN KEY (account_id)
                        REFERENCES oauth_credentials (id)
                        ON DELETE CASCADE
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_newsletter_sources_account_active
                ON newsletter_sources (
                    account_id,
                    active,
                    decision
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS
                    message_classifications (
                        gmail_message_row_id INTEGER
                            PRIMARY KEY,

                        verdict TEXT NOT NULL
                            CHECK (
                                verdict IN (
                                    'newsletter',
                                    'review',
                                    'not_newsletter'
                                )
                            ),

                        score INTEGER NOT NULL
                            CHECK (score >= 0),

                        reasons_json TEXT NOT NULL
                            CHECK (
                                length(
                                    trim(reasons_json)
                                ) > 0
                            ),

                        classifier_version TEXT NOT NULL
                            CHECK (
                                length(
                                    trim(classifier_version)
                                ) > 0
                            ),

                        classified_at TEXT NOT NULL,

                        FOREIGN KEY (
                            gmail_message_row_id
                        )
                            REFERENCES gmail_messages (id)
                            ON DELETE CASCADE
                    )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_message_classifications_verdict
                ON message_classifications (
                    verdict,
                    classified_at
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
