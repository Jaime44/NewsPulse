"""Test the SQLite schema, constraints, indexes, and ownership cascades."""

from __future__ import annotations

import os
import sqlite3
import stat
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app.backend.bd.db import (
    CURRENT_SCHEMA_VERSION,
    Database,
    DatabaseSchemaError,
)


NOW = "2026-09-13T00:00:00+00:00"


class DatabaseSchemaTests(unittest.TestCase):
    """Verify schema creation, constraints, indexes, and cascades."""

    def setUp(self) -> None:
        """Create isolated dependencies and fixtures for each test."""

        self.temporary_directory = TemporaryDirectory()
        database_path = (
            Path(self.temporary_directory.name)
            / "test.db"
        )
        self.database = Database(database_path)
        self.database.initialize()

    def tearDown(self) -> None:
        """Release temporary resources created for each test."""

        self.temporary_directory.cleanup()

    @staticmethod
    def insert_account(
        connection: sqlite3.Connection,
    ) -> None:
        """Insert the single authenticated account required by the fixture.

        Args:
            connection: Open SQLite connection receiving the account row.
        """

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
        """Insert a Gmail message required by the database fixture.

        Args:
            connection: Open SQLite connection receiving the message row.
            status: Initial message processing status.
        """

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
        """Verify that schema creates ingestion tables and indexes."""

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

    def test_new_database_uses_current_schema_version(
        self,
    ) -> None:
        """Verify that initialization records the supported schema version."""

        with self.database.connect() as connection:
            schema_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]

        self.assertEqual(
            schema_version,
            CURRENT_SCHEMA_VERSION,
        )

    def test_database_enables_wal_and_configured_busy_timeout(
        self,
    ) -> None:
        """Verify connections use WAL and wait for competing writers."""

        configured_database = Database(
            self.database.path,
            busy_timeout_ms=1_234,
        )
        configured_database.initialize()

        with configured_database.connect() as connection:
            journal_mode = connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
            busy_timeout = connection.execute(
                "PRAGMA busy_timeout"
            ).fetchone()[0]

        self.assertEqual(journal_mode, "wal")
        self.assertEqual(busy_timeout, 1_234)

    def test_database_rejects_invalid_busy_timeout(
        self,
    ) -> None:
        """Verify invalid writer-wait configuration fails immediately."""

        for invalid_value in (0, -1, True):
            with self.subTest(value=invalid_value):
                with self.assertRaises(ValueError):
                    Database(
                        self.database.path,
                        busy_timeout_ms=invalid_value,
                    )

    def test_legacy_schema_is_migrated_without_losing_data(
        self,
    ) -> None:
        """Verify that an unversioned compatible database retains its rows."""

        with self.database.connect() as connection:
            self.insert_account(connection)
            connection.execute(
                "PRAGMA user_version = 0"
            )

        logger = Mock()
        migrated_database = Database(
            self.database.path,
            logger=logger,
        )

        migrated_database.initialize()

        with migrated_database.connect() as connection:
            schema_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            stored_email = connection.execute(
                """
                SELECT email
                FROM oauth_credentials
                WHERE id = 1
                """
            ).fetchone()["email"]

        self.assertEqual(
            schema_version,
            CURRENT_SCHEMA_VERSION,
        )
        self.assertEqual(
            stored_email,
            "owner@example.com",
        )
        self.assertEqual(
            logger.info.call_count,
            2,
        )

        logged_templates = tuple(
            call.args[0]
            for call in logger.info.call_args_list
        )
        self.assertTrue(
            all(
                "newspulse.db" not in template
                and "owner@example.com" not in template
                for template in logged_templates
            )
        )

    def test_incompatible_legacy_schema_rolls_back(
        self,
    ) -> None:
        """Verify that incompatible version 0 storage is not marked as current."""

        database_path = (
            Path(self.temporary_directory.name)
            / "incompatible.db"
        )
        connection = sqlite3.connect(
            database_path
        )

        try:
            connection.execute(
                """
                CREATE TABLE oauth_credentials (
                    id INTEGER PRIMARY KEY
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(
            DatabaseSchemaError
        ):
            Database(database_path).initialize()

        connection = sqlite3.connect(
            database_path
        )

        try:
            schema_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            }
        finally:
            connection.close()

        self.assertEqual(schema_version, 0)
        self.assertEqual(
            tables,
            {"oauth_credentials"},
        )

    def test_newer_schema_version_is_rejected_unchanged(
        self,
    ) -> None:
        """Verify that older application code cannot open a newer schema."""

        future_version = (
            CURRENT_SCHEMA_VERSION + 1
        )

        with self.database.connect() as connection:
            connection.execute(
                "PRAGMA user_version = "
                f"{future_version}"
            )

        with self.assertRaises(
            DatabaseSchemaError
        ):
            Database(
                self.database.path
            ).initialize()

        with self.database.connect() as connection:
            stored_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]

        self.assertEqual(
            stored_version,
            future_version,
        )

    def test_future_schema_is_rejected_before_enabling_wal(
        self,
    ) -> None:
        """Verify startup never changes the journal of a future database."""

        database_path = (
            Path(self.temporary_directory.name)
            / "future.db"
        )
        future_version = CURRENT_SCHEMA_VERSION + 1
        connection = sqlite3.connect(database_path)

        try:
            connection.execute(
                f"PRAGMA user_version = {future_version}"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(DatabaseSchemaError):
            Database(database_path).initialize()

        connection = sqlite3.connect(database_path)

        try:
            stored_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            journal_mode = connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(stored_version, future_version)
        self.assertEqual(journal_mode, "delete")

    def test_current_schema_requires_all_indexes(
        self,
    ) -> None:
        """Verify that structural drift is rejected at application startup."""

        with self.database.connect() as connection:
            connection.execute(
                """
                DROP INDEX
                    idx_message_contents_expires_at
                """
            )

        with self.assertRaises(
            DatabaseSchemaError
        ):
            Database(
                self.database.path
            ).initialize()

    @unittest.skipUnless(
        os.name == "posix",
        "POSIX permission bits are required",
    )
    def test_initialize_restricts_storage_permissions(
        self,
    ) -> None:
        """Verify that existing permissive storage is restricted on POSIX."""

        storage_directory = (
            Path(self.temporary_directory.name)
            / "private-data"
        )
        database_path = (
            storage_directory
            / "permissions.db"
        )

        storage_directory.mkdir(mode=0o755)
        storage_directory.chmod(0o755)
        database_path.touch(mode=0o644)
        database_path.chmod(0o644)

        Database(database_path).initialize()

        self.assertEqual(
            stat.S_IMODE(
                storage_directory.stat().st_mode
            ),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(
                database_path.stat().st_mode
            ),
            0o600,
        )

    def test_connection_commits_and_closes_after_success(
        self,
    ) -> None:
        """Verify that a successful context commits and closes its connection."""

        with self.database.connect() as connection:
            self.insert_account(connection)

        with self.assertRaises(
            sqlite3.ProgrammingError
        ):
            connection.execute("SELECT 1")

        with self.database.connect() as connection:
            account_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM oauth_credentials
                """
            ).fetchone()["total"]

        self.assertEqual(account_count, 1)

    def test_connection_rolls_back_and_closes_after_failure(
        self,
    ) -> None:
        """Verify that an escaping error rolls back and closes its connection."""

        with self.assertRaises(RuntimeError):
            with self.database.connect() as connection:
                self.insert_account(connection)
                raise RuntimeError(
                    "Force transaction rollback"
                )

        with self.assertRaises(
            sqlite3.ProgrammingError
        ):
            connection.execute("SELECT 1")

        with self.database.connect() as connection:
            account_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM oauth_credentials
                """
            ).fetchone()["total"]

        self.assertEqual(account_count, 0)

    def test_duplicate_gmail_message_is_rejected(
        self,
    ) -> None:
        """Verify that duplicate Gmail message is rejected."""

        with self.database.connect() as connection:
            self.insert_account(connection)
            self.insert_message(connection)

            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_message(connection)

    def test_invalid_message_status_is_rejected(
        self,
    ) -> None:
        """Verify that invalid message status is rejected."""

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
        """Verify that invalid classification verdict is rejected."""

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
        """Verify that deleting account removes owned data."""

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

            connection.execute(
                """
                INSERT INTO ingestion_runs (
                    account_id,
                    status,
                    started_at,
                    lease_heartbeat_at,
                    lease_expires_at
                )
                VALUES (1, 'running', ?, ?, ?)
                """,
                (
                    NOW,
                    NOW,
                    "2026-09-13T00:15:00+00:00",
                ),
            )

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

            run_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM ingestion_runs
                """
            ).fetchone()["total"]

            status_history_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM message_status_history
                """
            ).fetchone()["total"]

            classification_history_count = (
                connection.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM message_classification_history
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
        self.assertEqual(run_count, 0)
        self.assertEqual(
            status_history_count,
            0,
        )
        self.assertEqual(
            classification_history_count,
            0,
        )

if __name__ == "__main__":
    unittest.main()
