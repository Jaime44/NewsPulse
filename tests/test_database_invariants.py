"""Test version 2 SQLite row-level data-integrity invariants."""

from __future__ import annotations

import sqlite3
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from app.backend.bd.db import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA_VERSION_2_TRIGGERS,
    SCHEMA_VERSION_3_COLUMNS,
    SCHEMA_VERSION_3_INDEXES,
    SCHEMA_VERSION_3_TRIGGERS,
    SCHEMA_VERSION_4_COLUMNS,
    SCHEMA_VERSION_4_INDEXES,
    SCHEMA_VERSION_4_TRIGGERS,
    Database,
    DatabaseSchemaError,
)


NOW = "2026-10-12T10:00:00+00:00"
LATER = "2026-11-11T10:00:00+00:00"


class DatabaseInvariantTests(unittest.TestCase):
    """Verify migrations and direct-SQL guards introduced by schema 2."""

    def setUp(self) -> None:
        """Create one isolated current database for each test."""

        self.temporary_directory = TemporaryDirectory()
        self.database = Database(
            Path(self.temporary_directory.name)
            / "test.db"
        )
        self.database.initialize()

    def tearDown(self) -> None:
        """Release temporary resources created for each test."""

        self.temporary_directory.cleanup()

    @staticmethod
    def insert_account(
        connection: sqlite3.Connection,
    ) -> None:
        """Insert the single valid account required by owned rows.

        Args:
            connection: Open SQLite connection receiving the account.
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
    ) -> int:
        """Insert and return one valid discovered-message row.

        Args:
            connection: Open SQLite connection receiving the message.

        Returns:
            Generated internal message identifier.
        """

        cursor = connection.execute(
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
            VALUES (1, ?, ?, 1, 'discovered', ?, ?)
            """,
            (
                "gmail-message-1",
                "gmail-thread-1",
                NOW,
                NOW,
            ),
        )

        return int(cursor.lastrowid)

    @staticmethod
    def insert_classification(
        connection: sqlite3.Connection,
        message_row_id: int,
        *,
        verdict: str = "newsletter",
        reasons_json: str = '["configured_sender"]',
        classified_at: str = NOW,
    ) -> None:
        """Insert one direct-SQL classification fixture.

        Args:
            connection: Open SQLite connection receiving the classification.
            message_row_id: Internal parent message identifier.
            verdict: Classification verdict stored for the message.
            reasons_json: Raw JSON value used to exercise database checks.
            classified_at: Raw timestamp used to exercise database checks.
        """

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
            VALUES (?, ?, 5, ?, 'rules-v1', ?)
            """,
            (
                message_row_id,
                verdict,
                reasons_json,
                classified_at,
            ),
        )

    @staticmethod
    def insert_content(
        connection: sqlite3.Connection,
        message_row_id: int,
        *,
        links_json: str = "[]",
        extracted_at: str = NOW,
        expires_at: str = LATER,
    ) -> None:
        """Insert one direct-SQL retained-content fixture.

        Args:
            connection: Open SQLite connection receiving the content.
            message_row_id: Internal parent message identifier.
            links_json: Raw JSON value used to exercise database checks.
            extracted_at: Raw extraction timestamp.
            expires_at: Raw retention-expiry timestamp.
        """

        connection.execute(
            """
            INSERT INTO message_contents (
                gmail_message_row_id,
                text_content,
                links_json,
                source_mime_type,
                extracted_at,
                expires_at
            )
            VALUES (?, 'Readable content', ?, 'text/plain', ?, ?)
            """,
            (
                message_row_id,
                links_json,
                extracted_at,
                expires_at,
            ),
        )

    def test_current_schema_contains_all_integrity_triggers(
        self,
    ) -> None:
        """Verify current initialization installs every required trigger."""

        with self.database.connect() as connection:
            actual_triggers = {
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'trigger'
                    """
                )
            }

        self.assertTrue(
            (
                SCHEMA_VERSION_2_TRIGGERS
                | SCHEMA_VERSION_3_TRIGGERS
                | SCHEMA_VERSION_4_TRIGGERS
            )
            <= actual_triggers
        )

    def test_current_schema_contains_history_tables_and_indexes(
        self,
    ) -> None:
        """Verify schema 3 installs each history table and lookup index."""

        with self.database.connect() as connection:
            table_names = {
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            }
            index_names = {
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
            set(SCHEMA_VERSION_3_COLUMNS)
            <= table_names
        )
        self.assertTrue(
            SCHEMA_VERSION_3_INDEXES
            <= index_names
        )

    def test_current_schema_contains_durable_lease_guards(
        self,
    ) -> None:
        """Verify schema 4 installs lease columns and exclusivity indexes."""

        with self.database.connect() as connection:
            columns = tuple(
                row["name"]
                for row in connection.execute(
                    'PRAGMA table_info("ingestion_runs")'
                )
            )
            index_names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }

        self.assertEqual(
            columns,
            SCHEMA_VERSION_4_COLUMNS["ingestion_runs"],
        )
        self.assertTrue(
            SCHEMA_VERSION_4_INDEXES <= index_names
        )

    def test_direct_sql_rejects_invalid_or_duplicate_leases(
        self,
    ) -> None:
        """Verify active leases require valid times and one owner."""

        with self.database.connect() as connection:
            self.insert_account(connection)

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO ingestion_runs (
                        account_id,
                        status,
                        started_at
                    )
                    VALUES (1, 'running', ?)
                    """,
                    (NOW,),
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
                (NOW, NOW, LATER),
            )

            with self.assertRaises(sqlite3.IntegrityError):
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
                    (NOW, NOW, LATER),
                )

    def test_direct_sql_rejects_non_canonical_timestamps(
        self,
    ) -> None:
        """Verify direct writes require parseable canonical UTC timestamps."""

        invalid_values = (
            "not-a-date",
            "2026-02-30T10:00:00+00:00",
            "2026-10-12T10:00:00+02:00",
            "2026-10-12 10:00:00+00:00",
        )

        with self.database.connect() as connection:
            self.insert_account(connection)
            message_row_id = self.insert_message(
                connection
            )

            for value in invalid_values:
                with self.subTest(value=value):
                    with self.assertRaises(
                        sqlite3.IntegrityError
                    ):
                        self.insert_classification(
                            connection,
                            message_row_id,
                            classified_at=value,
                        )

    def test_direct_sql_requires_json_string_arrays(
        self,
    ) -> None:
        """Verify classification reasons and content links are string arrays."""

        invalid_json_values = (
            "not-json",
            "{}",
            '["valid", 1]',
            '["valid", ""]',
        )

        with self.database.connect() as connection:
            self.insert_account(connection)
            message_row_id = self.insert_message(
                connection
            )

            for value in invalid_json_values:
                with self.subTest(
                    field="reasons_json",
                    value=value,
                ):
                    with self.assertRaises(
                        sqlite3.IntegrityError
                    ):
                        self.insert_classification(
                            connection,
                            message_row_id,
                            reasons_json=value,
                        )

            self.insert_classification(
                connection,
                message_row_id,
            )

            for value in invalid_json_values:
                with self.subTest(
                    field="links_json",
                    value=value,
                ):
                    with self.assertRaises(
                        sqlite3.IntegrityError
                    ):
                        self.insert_content(
                            connection,
                            message_row_id,
                            links_json=value,
                        )

    def test_message_lifecycle_is_coherent_but_retention_is_allowed(
        self,
    ) -> None:
        """Verify lifecycle fields agree without requiring retained content."""

        with self.database.connect() as connection:
            self.insert_account(connection)
            message_row_id = self.insert_message(
                connection
            )

            invalid_updates = (
                (
                    "processed",
                    None,
                    None,
                ),
                (
                    "failed",
                    None,
                    None,
                ),
                (
                    "discovered",
                    NOW,
                    None,
                ),
                (
                    "processing",
                    None,
                    "unexpected_error",
                ),
            )

            for status, processed_at, error_code in invalid_updates:
                with self.subTest(status=status):
                    with self.assertRaises(
                        sqlite3.IntegrityError
                    ):
                        connection.execute(
                            """
                            UPDATE gmail_messages
                            SET status = ?,
                                processed_at = ?,
                                last_error_code = ?
                            WHERE id = ?
                            """,
                            (
                                status,
                                processed_at,
                                error_code,
                                message_row_id,
                            ),
                        )

            connection.execute(
                """
                UPDATE gmail_messages
                SET status = 'processed',
                    updated_at = ?,
                    processed_at = ?,
                    last_error_code = NULL
                WHERE id = ?
                """,
                (
                    NOW,
                    NOW,
                    message_row_id,
                ),
            )

            content_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM message_contents
                """
            ).fetchone()["total"]

        self.assertEqual(content_count, 0)

    def test_content_requires_and_preserves_newsletter_classification(
        self,
    ) -> None:
        """Verify content cannot exist without a newsletter verdict."""

        with self.database.connect() as connection:
            self.insert_account(connection)
            message_row_id = self.insert_message(
                connection
            )

            with self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.insert_content(
                    connection,
                    message_row_id,
                )

            self.insert_classification(
                connection,
                message_row_id,
            )
            self.insert_content(
                connection,
                message_row_id,
            )

            with self.assertRaises(
                sqlite3.IntegrityError
            ):
                connection.execute(
                    """
                    UPDATE message_classifications
                    SET verdict = 'not_newsletter'
                    WHERE gmail_message_row_id = ?
                    """,
                    (message_row_id,),
                )

            with self.assertRaises(
                sqlite3.IntegrityError
            ):
                connection.execute(
                    """
                    DELETE FROM message_classifications
                    WHERE gmail_message_row_id = ?
                    """,
                    (message_row_id,),
                )

    def test_direct_sql_rejects_incoherent_ingestion_run(
        self,
    ) -> None:
        """Verify terminal scan rows require matching fields and safe codes."""

        with self.database.connect() as connection:
            self.insert_account(connection)

            invalid_cases = (
                (
                    "succeeded",
                    NOW,
                    None,
                    None,
                ),
                (
                    "failed",
                    NOW,
                    None,
                    None,
                ),
                (
                    "running",
                    NOW,
                    None,
                    None,
                ),
            )

            for (
                status,
                finished_at,
                cursor_after,
                error_code,
            ) in invalid_cases:
                with self.subTest(status=status):
                    with self.assertRaises(
                        sqlite3.IntegrityError
                    ):
                        connection.execute(
                            """
                            INSERT INTO ingestion_runs (
                                account_id,
                                status,
                                started_at,
                                finished_at,
                                cursor_after,
                                error_code
                            )
                            VALUES (1, ?, ?, ?, ?, ?)
                            """,
                            (
                                status,
                                NOW,
                                finished_at,
                                cursor_after,
                                error_code,
                            ),
                        )

    def test_version_2_database_backfills_current_histories(
        self,
    ) -> None:
        """Verify migration 2 to 3 preserves snapshots as baseline history."""

        with self.database.connect() as connection:
            self.insert_account(connection)
            message_row_id = self.insert_message(
                connection
            )
            self.insert_classification(
                connection,
                message_row_id,
            )

            for trigger_name in SCHEMA_VERSION_3_TRIGGERS:
                connection.execute(
                    f'DROP TRIGGER "{trigger_name}"'
                )

            for table_name in (
                "message_classification_history",
                "message_status_history",
                "ingestion_runs",
            ):
                connection.execute(
                    f'DROP TABLE "{table_name}"'
                )

            connection.execute(
                "PRAGMA user_version = 2"
            )

        self.database.initialize()

        with self.database.connect() as connection:
            schema_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            status_history = connection.execute(
                """
                SELECT from_status, to_status
                FROM message_status_history
                """
            ).fetchone()
            classification_history = (
                connection.execute(
                    """
                    SELECT verdict, classifier_version
                    FROM message_classification_history
                    """
                ).fetchone()
            )

        self.assertEqual(
            schema_version,
            CURRENT_SCHEMA_VERSION,
        )
        self.assertIsNotNone(status_history)
        assert status_history is not None
        self.assertIsNone(
            status_history["from_status"]
        )
        self.assertEqual(
            status_history["to_status"],
            "discovered",
        )
        self.assertIsNotNone(
            classification_history
        )
        assert classification_history is not None
        self.assertEqual(
            classification_history["verdict"],
            "newsletter",
        )
        self.assertEqual(
            classification_history[
                "classifier_version"
            ],
            "rules-v1",
        )

    def test_version_3_database_recovers_running_scan_for_leases(
        self,
    ) -> None:
        """Verify migration 3 to 4 safely closes a pre-lease running scan."""

        with self.database.connect() as connection:
            self.insert_account(connection)
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
                (NOW, NOW, LATER),
            )

            for trigger_name in SCHEMA_VERSION_4_TRIGGERS:
                connection.execute(
                    f'DROP TRIGGER "{trigger_name}"'
                )

            for index_name in SCHEMA_VERSION_4_INDEXES:
                connection.execute(
                    f'DROP INDEX "{index_name}"'
                )

            connection.execute(
                """
                ALTER TABLE ingestion_runs
                DROP COLUMN lease_expires_at
                """
            )
            connection.execute(
                """
                ALTER TABLE ingestion_runs
                DROP COLUMN lease_heartbeat_at
                """
            )
            connection.execute(
                "PRAGMA user_version = 3"
            )

        self.database.initialize()

        with self.database.connect() as connection:
            run = connection.execute(
                """
                SELECT
                    status,
                    error_code,
                    lease_heartbeat_at,
                    lease_expires_at
                FROM ingestion_runs
                """
            ).fetchone()

        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run["status"], "failed")
        self.assertEqual(
            run["error_code"],
            "application_restarted",
        )
        self.assertIsNone(run["lease_heartbeat_at"])
        self.assertIsNone(run["lease_expires_at"])

    def test_version_1_database_migrates_without_rewriting_rows(
        self,
    ) -> None:
        """Verify a valid version 1 database gains guards and keeps its data."""

        with self.database.connect() as connection:
            self.insert_account(connection)
            message_row_id = self.insert_message(
                connection
            )
            self.insert_classification(
                connection,
                message_row_id,
            )
            self.insert_content(
                connection,
                message_row_id,
            )

            trigger_names = tuple(
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'trigger'
                    """
                )
            )

            for trigger_name in trigger_names:
                connection.execute(
                    f'DROP TRIGGER "{trigger_name}"'
                )

            connection.execute(
                "PRAGMA user_version = 1"
            )

        self.database.initialize()

        with self.database.connect() as connection:
            schema_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            stored_content = connection.execute(
                """
                SELECT text_content
                FROM message_contents
                WHERE gmail_message_row_id = ?
                """,
                (message_row_id,),
            ).fetchone()

        self.assertEqual(
            schema_version,
            CURRENT_SCHEMA_VERSION,
        )
        self.assertIsNotNone(stored_content)
        assert stored_content is not None
        self.assertEqual(
            stored_content["text_content"],
            "Readable content",
        )

    def test_invalid_version_1_data_rolls_back_migration(
        self,
    ) -> None:
        """Verify invalid legacy rows prevent version 2 from being recorded."""

        with self.database.connect() as connection:
            self.insert_account(connection)
            message_row_id = self.insert_message(
                connection
            )
            self.insert_classification(
                connection,
                message_row_id,
            )

            trigger_names = tuple(
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'trigger'
                    """
                )
            )

            for trigger_name in trigger_names:
                connection.execute(
                    f'DROP TRIGGER "{trigger_name}"'
                )

            connection.execute(
                """
                UPDATE message_classifications
                SET reasons_json = '{}'
                WHERE gmail_message_row_id = ?
                """,
                (message_row_id,),
            )
            connection.execute(
                "PRAGMA user_version = 1"
            )

        with self.assertRaises(
            DatabaseSchemaError
        ):
            self.database.initialize()

        with self.database.connect() as connection:
            schema_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            trigger_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM sqlite_master
                WHERE type = 'trigger'
                """
            ).fetchone()["total"]

        self.assertEqual(schema_version, 1)
        self.assertEqual(trigger_count, 0)


if __name__ == "__main__":
    unittest.main()
