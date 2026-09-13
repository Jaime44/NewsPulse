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
            }.issubset(tables)
        )
        self.assertTrue(
            {
                "idx_gmail_messages_account_status",
                "idx_gmail_messages_account_date",
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

    def test_deleting_account_removes_ingestion_data(
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
                DELETE FROM oauth_credentials
                WHERE id = 1
                """
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

        self.assertEqual(state_count, 0)
        self.assertEqual(message_count, 0)


if __name__ == "__main__":
    unittest.main()
