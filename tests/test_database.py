from __future__ import annotations

import sqlite3
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from app.backend.bd.db import Database


NOW = "2026-09-13T00:00:00+00:00"


class DatabaseSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = (
            Path(self.temporary_directory.name)
            / "test.db"
        )
        self.database = Database(database_path)
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def insert_account(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            INSERT INTO oauth_credentials (
                id,
                email,
                credentials_encrypted,
                revoked,
                connected_at,
                updated_at
            )
            VALUES (1, ?, ?, 0, ?, ?)
            """,
            (
                "owner@example.com",
                b"encrypted-credentials",
                NOW,
                NOW,
            ),
        )

    @staticmethod
    def insert_message(
        connection: sqlite3.Connection,
        status: str = "discovered",
    ) -> None:
        connection.execute(
            """
            INSERT INTO gmail_messages (
                account_id,
                gmail_message_id,
                gmail_thread_id,
                internal_date_ms,
                status,
                discovered_at,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gmail-message-1",
                "gmail-thread-1",
                1_757_721_600_000,
                status,
                NOW,
                NOW,
            ),
        )

    def test_schema_creates_ingestion_tables_and_indexes(
        self,
    ) -> None:
        with self.database.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            }

            indexes = {
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'index'
                    """
                )
            }

        self.assertTrue(
            {
                "oauth_credentials",
                "ingestion_state",
                "gmail_messages",
                "newsletter_sources",
                "message_classifications",
            }.issubset(tables)
        )
        self.assertTrue(
            {
                "idx_gmail_messages_account_status",
                "idx_gmail_messages_account_date",
                "idx_newsletter_sources_account_active",
                "idx_message_classifications_verdict",
            }.issubset(indexes)
        )

    def test_duplicate_gmail_message_is_rejected(
        self,
    ) -> None:
        with self.database.connect() as connection:
            self.insert_account(connection)
            self.insert_message(connection)

            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_message(connection)

    def test_invalid_message_status_is_rejected(
        self,
    ) -> None:
        with self.database.connect() as connection:
            self.insert_account(connection)

            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_message(
                    connection,
                    status="unknown",

                )

    def test_invalid_classification_verdict_is_rejected(
        self,
    ) -> None:
        with self.database.connect() as connection:
            self.insert_account(connection)
            self.insert_message(connection)

            with self.assertRaises(
                sqlite3.IntegrityError
            ):
                connection.execute(
                    """
                    INSERT INTO message_classifications (
                        gmail_message_row_id,
                        verdict,
                        score,
                        reasons_json,
                        classifier_version,
                        classified_at
                    )
                    SELECT
                        id,
                        'unknown',
                        0,
                        '[]',
                        'rules-v1',
                        ?
                    FROM gmail_messages
                    WHERE account_id = 1
                      AND gmail_message_id = ?
                    """,
                    (
                        NOW,
                        "gmail-message-1",
                    ),
                )

    def test_deleting_account_removes_owned_data(
        self,
    ) -> None:
        with self.database.connect() as connection:
            self.insert_account(connection)

            connection.execute(
                """
                INSERT INTO ingestion_state (
                    account_id,
                    started_at,
                    created_at,
                    updated_at
                )
                VALUES (1, ?, ?, ?)
                """,
                (NOW, NOW, NOW),
            )

            self.insert_message(connection)

            connection.execute(
                """
                INSERT INTO newsletter_sources (
                    account_id,
                    source_type,
                    source_value,
                    decision,
                    origin,
                    confidence,
                    active,
                    created_at,
                    updated_at
                )
                VALUES (
                    1,
                    'sender',
                    'news@example.com',
                    'include',
                    'manual',
                    100,
                    1,
                    ?,
                    ?
                )
                """,
                (NOW, NOW),
            )

            connection.execute(
                """
                DELETE FROM oauth_credentials
                WHERE id = 1
                """
            )

            connection.execute(
                """
                INSERT INTO message_classifications (
                    gmail_message_row_id,
                    verdict,
                    score,
                    reasons_json,
                    classifier_version,
                    classified_at
                )
                SELECT
                    id,
                    'newsletter',
                    5,
                    ?,
                    'rules-v1',
                    ?
                FROM gmail_messages
                WHERE account_id = 1
                  AND gmail_message_id = ?
                """,
                (
                    '["configured_sender"]',
                    NOW,
                    "gmail-message-1",
                ),
            )

            state_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM ingestion_state
                """
            ).fetchone()["total"]

            message_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM gmail_messages
                """
            ).fetchone()["total"]

            source_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM newsletter_sources
                """
            ).fetchone()["total"]

            classification_count = (
                connection.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM message_classifications
                    """
                ).fetchone()["total"]
            )

        self.assertEqual(state_count, 0)
        self.assertEqual(message_count, 0)
        self.assertEqual(source_count, 0)
        self.assertEqual(
            classification_count,
            0,
        )

if __name__ == "__main__":
    unittest.main()
