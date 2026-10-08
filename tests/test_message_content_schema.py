from __future__ import annotations

import sqlite3
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from app.backend.bd.db import Database


EXTRACTED_AT = "2026-10-08T10:00:00+00:00"
EXPIRES_AT = "2026-11-07T10:00:00+00:00"


class MessageContentSchemaTests(unittest.TestCase):
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
                EXTRACTED_AT,
                EXTRACTED_AT,
            ),
        )

    @staticmethod
    def insert_message(
        connection: sqlite3.Connection,
    ) -> int:
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
            VALUES (
                1,
                ?,
                ?,
                ?,
                'discovered',
                ?,
                ?
            )
            """,
            (
                "gmail-message-1",
                "gmail-thread-1",
                1_757_721_600_000,
                EXTRACTED_AT,
                EXTRACTED_AT,
            ),
        )

        return int(cursor.lastrowid)

    @staticmethod
    def insert_content(
        connection: sqlite3.Connection,
        gmail_message_row_id: int,
        *,
        text_content: str = "Readable newsletter content",
        links_json: str = '["https://example.com/article"]',
        source_mime_type: str = "text/plain",
        extracted_at: str = EXTRACTED_AT,
        expires_at: str = EXPIRES_AT,
    ) -> None:
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
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                gmail_message_row_id,
                text_content,
                links_json,
                source_mime_type,
                extracted_at,
                expires_at,
            ),
        )

    def test_schema_creates_content_table_and_index(
        self,
    ) -> None:
        with self.database.connect() as connection:
            table = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'message_contents'
                """
            ).fetchone()

            index = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
                  AND name = ?
                """,
                (
                    "idx_message_contents_expires_at",
                ),
            ).fetchone()

            columns = tuple(
                row["name"]
                for row in connection.execute(
                    """
                    PRAGMA table_info(
                        message_contents
                    )
                    """
                )
            )

        self.assertIsNotNone(table)
        self.assertIsNotNone(index)

        self.assertEqual(
            columns,
            (
                "gmail_message_row_id",
                "text_content",
                "links_json",
                "source_mime_type",
                "extracted_at",
                "expires_at",
            ),
        )

    def test_content_constraints_reject_invalid_data(
        self,
    ) -> None:
        with self.database.connect() as connection:
            self.insert_account(connection)

            gmail_message_row_id = (
                self.insert_message(connection)
            )

            self.insert_content(
                connection,
                gmail_message_row_id,
            )

            stored = connection.execute(
                """
                SELECT
                    text_content,
                    links_json,
                    source_mime_type,
                    extracted_at,
                    expires_at
                FROM message_contents
                WHERE gmail_message_row_id = ?
                """,
                (gmail_message_row_id,),
            ).fetchone()

            self.assertIsNotNone(stored)
            self.assertEqual(
                stored["text_content"],
                "Readable newsletter content",
            )
            self.assertEqual(
                stored["links_json"],
                '["https://example.com/article"]',
            )
            self.assertEqual(
                stored["source_mime_type"],
                "text/plain",
            )
            self.assertEqual(
                stored["extracted_at"],
                EXTRACTED_AT,
            )
            self.assertEqual(
                stored["expires_at"],
                EXPIRES_AT,
            )

            connection.execute(
                """
                DELETE FROM message_contents
                WHERE gmail_message_row_id = ?
                """,
                (gmail_message_row_id,),
            )

            invalid_cases = (
                (
                    "empty text",
                    "   ",
                    "[]",
                    "text/plain",
                    EXTRACTED_AT,
                    EXPIRES_AT,
                ),
                (
                    "empty links",
                    "Readable content",
                    "   ",
                    "text/plain",
                    EXTRACTED_AT,
                    EXPIRES_AT,
                ),
                (
                    "unsupported MIME type",
                    "Readable content",
                    "[]",
                    "application/pdf",
                    EXTRACTED_AT,
                    EXPIRES_AT,
                ),
                (
                    "invalid expiration",
                    "Readable content",
                    "[]",
                    "text/plain",
                    EXTRACTED_AT,
                    EXTRACTED_AT,
                ),
            )

            for (
                label,
                text_content,
                links_json,
                source_mime_type,
                extracted_at,
                expires_at,
            ) in invalid_cases:
                with self.subTest(case=label):
                    with self.assertRaises(
                        sqlite3.IntegrityError
                    ):
                        self.insert_content(
                            connection,
                            gmail_message_row_id,
                            text_content=text_content,
                            links_json=links_json,
                            source_mime_type=(
                                source_mime_type
                            ),
                            extracted_at=extracted_at,
                            expires_at=expires_at,
                        )

            with self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.insert_content(
                    connection,
                    999_999,
                )

            content_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM message_contents
                """
            ).fetchone()["total"]

        self.assertEqual(content_count, 0)

    def test_deleting_account_removes_message_content(
        self,
    ) -> None:
        with self.database.connect() as connection:
            self.insert_account(connection)

            gmail_message_row_id = (
                self.insert_message(connection)
            )

            self.insert_content(
                connection,
                gmail_message_row_id,
            )

            connection.execute(
                """
                DELETE FROM oauth_credentials
                WHERE id = 1
                """
            )

            message_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM gmail_messages
                """
            ).fetchone()["total"]

            content_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM message_contents
                """
            ).fetchone()["total"]

        self.assertEqual(message_count, 0)
        self.assertEqual(content_count, 0)


if __name__ == "__main__":
    unittest.main()
