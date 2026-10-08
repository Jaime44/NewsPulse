from __future__ import annotations

import unittest

from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path
from tempfile import TemporaryDirectory

from app.backend.bd.db import Database
from app.backend.bd.message_content_store import (
    MessageContentStore,
    MessageContentStoreError,
    StoredMessageContent,
)


EXTRACTED_AT = datetime(
    2026,
    10,
    8,
    10,
    0,
    tzinfo=timezone.utc,
)


class MessageContentStoreTests(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.temporary_directory = (
            TemporaryDirectory()
        )

        database_path = (
            Path(
                self.temporary_directory.name
            )
            / "test.db"
        )

        self.database = Database(
            database_path
        )
        self.store = MessageContentStore(
            self.database,
            retention_days=30,
        )

        self.insert_account()
        self.insert_message(
            "gmail-message-1"
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def insert_account(self) -> None:
        timestamp = (
            EXTRACTED_AT.isoformat()
        )

        with self.database.connect() as connection:
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
                    timestamp,
                    timestamp,
                ),
            )

    def insert_message(
        self,
        gmail_message_id: str,
    ) -> None:
        timestamp = (
            EXTRACTED_AT.isoformat()
        )

        with self.database.connect() as connection:
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
                    gmail_message_id,
                    (
                        f"thread-for-"
                        f"{gmail_message_id}"
                    ),
                    1_757_721_600_000,
                    timestamp,
                    timestamp,
                ),
            )

    def test_retention_days_must_be_positive_integer(
        self,
    ) -> None:
        invalid_values = (
            0,
            -1,
            True,
            1.5,
            "30",
        )

        for value in invalid_values:
            with self.subTest(
                retention_days=value
            ):
                with self.assertRaises(
                    ValueError
                ):
                    MessageContentStore(
                        self.database,
                        retention_days=value,
                    )

    def test_content_is_saved_normalized_and_loaded(
        self,
    ) -> None:
        saved_content = (
            self.store.save_content(
                " gmail-message-1 ",
                text="  Weekly digest  ",
                links=(
                    (
                        " https://"
                        "Example.com/article "
                    ),
                    (
                        "https://"
                        "Example.com/article"
                    ),
                    (
                        "https://"
                        "example.com/second"
                    ),
                ),
                source_mime_type=(
                    " TEXT/PLAIN "
                ),
                extracted_at=EXTRACTED_AT,
            )
        )

        loaded_content = (
            self.store.load_content(
                "gmail-message-1"
            )
        )

        expected_content = (
            StoredMessageContent(
                gmail_message_row_id=(
                    saved_content
                    .gmail_message_row_id
                ),
                gmail_message_id=(
                    "gmail-message-1"
                ),
                text="Weekly digest",
                links=(
                    (
                        "https://"
                        "Example.com/article"
                    ),
                    (
                        "https://"
                        "example.com/second"
                    ),
                ),
                source_mime_type=(
                    "text/plain"
                ),
                extracted_at=EXTRACTED_AT,
                expires_at=(
                    EXTRACTED_AT
                    + timedelta(days=30)
                ),
            )
        )

        self.assertEqual(
            saved_content,
            expected_content,
        )
        self.assertEqual(
            loaded_content,
            expected_content,
        )

    def test_existing_content_is_replaced(
        self,
    ) -> None:
        self.store.save_content(
            "gmail-message-1",
            text="Original content",
            links=(),
            source_mime_type="text/plain",
            extracted_at=EXTRACTED_AT,
        )

        replacement_time = (
            EXTRACTED_AT
            + timedelta(days=2)
        )

        replacement = (
            self.store.save_content(
                "gmail-message-1",
                text="Replacement content",
                links=(
                    "https://example.com/new",
                ),
                source_mime_type="text/html",
                extracted_at=(
                    replacement_time
                ),
            )
        )

        loaded_content = (
            self.store.load_content(
                "gmail-message-1"
            )
        )

        with self.database.connect() as connection:
            content_count = (
                connection.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM message_contents
                    """
                ).fetchone()["total"]
            )

        self.assertEqual(
            content_count,
            1,
        )
        self.assertEqual(
            loaded_content,
            replacement,
        )
        self.assertEqual(
            replacement.text,
            "Replacement content",
        )
        self.assertEqual(
            replacement.source_mime_type,
            "text/html",
        )
        self.assertEqual(
            replacement.expires_at,
            (
                replacement_time
                + timedelta(days=30)
            ),
        )

    def test_unregistered_message_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(
            MessageContentStoreError
        ) as raised:
            self.store.save_content(
                "unknown-message",
                text="Readable content",
                links=(),
                source_mime_type=(
                    "text/plain"
                ),
                extracted_at=EXTRACTED_AT,
            )

        self.assertEqual(
            str(raised.exception),
            (
                "The Gmail message has not "
                "been registered"
            ),
        )

    def test_invalid_content_values_are_rejected(
        self,
    ) -> None:
        invalid_cases = (
            (
                "empty message identifier",
                {
                    "gmail_message_id": "   ",
                    "text": "Readable content",
                    "links": (),
                    "source_mime_type": (
                        "text/plain"
                    ),
                    "extracted_at": (
                        EXTRACTED_AT
                    ),
                },
            ),
            (
                "empty text",
                {
                    "gmail_message_id": (
                        "gmail-message-1"
                    ),
                    "text": "   ",
                    "links": (),
                    "source_mime_type": (
                        "text/plain"
                    ),
                    "extracted_at": (
                        EXTRACTED_AT
                    ),
                },
            ),
            (
                "links are a string",
                {
                    "gmail_message_id": (
                        "gmail-message-1"
                    ),
                    "text": "Readable content",
                    "links": (
                        "https://example.com"
                    ),
                    "source_mime_type": (
                        "text/plain"
                    ),
                    "extracted_at": (
                        EXTRACTED_AT
                    ),
                },
            ),
            (
                "empty link",
                {
                    "gmail_message_id": (
                        "gmail-message-1"
                    ),
                    "text": "Readable content",
                    "links": ("   ",),
                    "source_mime_type": (
                        "text/plain"
                    ),
                    "extracted_at": (
                        EXTRACTED_AT
                    ),
                },
            ),
            (
                "non-string link",
                {
                    "gmail_message_id": (
                        "gmail-message-1"
                    ),
                    "text": "Readable content",
                    "links": (42,),
                    "source_mime_type": (
                        "text/plain"
                    ),
                    "extracted_at": (
                        EXTRACTED_AT
                    ),
                },
            ),
            (
                "unsupported MIME type",
                {
                    "gmail_message_id": (
                        "gmail-message-1"
                    ),
                    "text": "Readable content",
                    "links": (),
                    "source_mime_type": (
                        "application/pdf"
                    ),
                    "extracted_at": (
                        EXTRACTED_AT
                    ),
                },
            ),
            (
                "naive timestamp",
                {
                    "gmail_message_id": (
                        "gmail-message-1"
                    ),
                    "text": "Readable content",
                    "links": (),
                    "source_mime_type": (
                        "text/plain"
                    ),
                    "extracted_at": datetime(
                        2026,
                        10,
                        8,
                        10,
                        0,
                    ),
                },
            ),
        )

        for label, arguments in invalid_cases:
            with self.subTest(case=label):
                with self.assertRaises(
                    MessageContentStoreError
                ):
                    self.store.save_content(
                        **arguments
                    )

    def test_missing_content_returns_none(
        self,
    ) -> None:
        loaded_content = (
            self.store.load_content(
                "gmail-message-1"
            )
        )

        self.assertIsNone(
            loaded_content
        )

    def test_expired_content_is_purged(
        self,
    ) -> None:
        self.insert_message(
            "gmail-message-2"
        )

        self.store.save_content(
            "gmail-message-1",
            text="First content",
            links=(),
            source_mime_type="text/plain",
            extracted_at=EXTRACTED_AT,
        )

        second_extraction = (
            EXTRACTED_AT
            + timedelta(days=1)
        )

        self.store.save_content(
            "gmail-message-2",
            text="Second content",
            links=(),
            source_mime_type="text/plain",
            extracted_at=second_extraction,
        )

        first_expiration = (
            EXTRACTED_AT
            + timedelta(days=30)
        )

        first_deleted_count = (
            self.store.purge_expired(
                expired_at=first_expiration
            )
        )

        self.assertEqual(
            first_deleted_count,
            1,
        )
        self.assertIsNone(
            self.store.load_content(
                "gmail-message-1"
            )
        )
        self.assertIsNotNone(
            self.store.load_content(
                "gmail-message-2"
            )
        )

        second_expiration = (
            second_extraction
            + timedelta(days=30)
        )

        second_deleted_count = (
            self.store.purge_expired(
                expired_at=second_expiration
            )
        )

        self.assertEqual(
            second_deleted_count,
            1,
        )
        self.assertIsNone(
            self.store.load_content(
                "gmail-message-2"
            )
        )

    def test_invalid_stored_links_are_rejected(
        self,
    ) -> None:
        self.store.save_content(
            "gmail-message-1",
            text="Readable content",
            links=(
                "https://example.com/article",
            ),
            source_mime_type="text/plain",
            extracted_at=EXTRACTED_AT,
        )

        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE message_contents
                SET links_json = ?
                """,
                ("not-valid-json",),
            )

        with self.assertRaises(
            MessageContentStoreError
        ) as raised:
            self.store.load_content(
                "gmail-message-1"
            )

        self.assertEqual(
            str(raised.exception),
            (
                "Stored content links "
                "are invalid"
            ),
        )


if __name__ == "__main__":
    unittest.main()
