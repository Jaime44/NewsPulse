from __future__ import annotations

import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.backend.bd.db import Database
from app.backend.bd.ingestion_store import (
    IngestionStore,
    IngestionStoreError,
)


STARTED_AT = datetime(
    2026,
    9,
    13,
    10,
    0,
    tzinfo=timezone.utc,
)


class IngestionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = (
            Path(self.temporary_directory.name)
            / "test.db"
        )

        self.database = Database(database_path)
        self.store = IngestionStore(self.database)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def insert_account(self) -> None:
        timestamp = STARTED_AT.isoformat()

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

    def test_state_requires_authenticated_account(
        self,
    ) -> None:
        with self.assertRaises(IngestionStoreError):
            self.store.get_or_create_state(
                started_at=STARTED_AT
            )

    def test_initial_timestamp_is_created_only_once(
        self,
    ) -> None:
        self.insert_account()

        first_state = self.store.get_or_create_state(
            started_at=STARTED_AT
        )

        later_time = STARTED_AT + timedelta(days=7)

        second_state = self.store.get_or_create_state(
            started_at=later_time
        )

        self.assertEqual(
            first_state.started_at,
            STARTED_AT,
        )
        self.assertEqual(
            second_state.started_at,
            STARTED_AT,
        )
        self.assertEqual(
            second_state.created_at,
            first_state.created_at,
        )
        self.assertIsNone(
            second_state.last_successful_scan_at
        )

    def test_timestamp_is_normalized_to_utc(
        self,
    ) -> None:
        self.insert_account()

        madrid_offset = timezone(
            timedelta(hours=2)
        )
        local_time = datetime(
            2026,
            9,
            13,
            12,
            0,
            tzinfo=madrid_offset,
        )

        state = self.store.get_or_create_state(
            started_at=local_time
        )

        self.assertEqual(
            state.started_at,
            STARTED_AT,
        )
        self.assertEqual(
            state.started_at.tzinfo,
            timezone.utc,
        )

    def test_successful_scan_advances_cursor(
        self,
    ) -> None:
        self.insert_account()
        self.store.get_or_create_state(
            started_at=STARTED_AT
        )

        completed_at = (
            STARTED_AT + timedelta(hours=1)
        )

        updated_state = (
            self.store.mark_scan_successful(
                completed_at=completed_at
            )
        )
        stored_state = self.store.load_state()

        self.assertEqual(
            updated_state.last_successful_scan_at,
            completed_at,
        )
        self.assertIsNotNone(stored_state)
        assert stored_state is not None
        self.assertEqual(
            stored_state.last_successful_scan_at,
            completed_at,
        )

    def test_cursor_cannot_move_backwards(
        self,
    ) -> None:
        self.insert_account()
        self.store.get_or_create_state(
            started_at=STARTED_AT
        )

        first_completion = (
            STARTED_AT + timedelta(hours=2)
        )
        earlier_completion = (
            STARTED_AT + timedelta(hours=1)
        )

        self.store.mark_scan_successful(
            completed_at=first_completion
        )

        with self.assertRaises(IngestionStoreError):
            self.store.mark_scan_successful(
                completed_at=earlier_completion
            )

        stored_state = self.store.load_state()

        self.assertIsNotNone(stored_state)
        assert stored_state is not None
        self.assertEqual(
            stored_state.last_successful_scan_at,
            first_completion,
        )

    def test_naive_timestamp_is_rejected(
        self,
    ) -> None:
        self.insert_account()

        naive_timestamp = datetime(
            2026,
            9,
            13,
            10,
            0,
        )

        with self.assertRaises(IngestionStoreError):
            self.store.get_or_create_state(
                started_at=naive_timestamp
            )

    def prepare_ingestion(self) -> None:
        self.insert_account()
        self.store.get_or_create_state(
            started_at=STARTED_AT
        )

    def test_message_requires_initialized_ingestion(
        self,
    ) -> None:
        self.insert_account()

        with self.assertRaises(IngestionStoreError):
            self.store.register_message(
                gmail_message_id="gmail-message-1",
                gmail_thread_id="gmail-thread-1",
                internal_date_ms=1_757_721_600_000,
                discovered_at=STARTED_AT,
            )

    def test_message_is_registered_and_loaded(
        self,
    ) -> None:
        self.prepare_ingestion()

        discovered_at = (
            STARTED_AT + timedelta(minutes=5)
        )

        was_inserted = self.store.register_message(
            gmail_message_id=" gmail-message-1 ",
            gmail_thread_id=" gmail-thread-1 ",
            internal_date_ms=1_757_721_600_000,
            discovered_at=discovered_at,
        )

        stored_message = self.store.load_message(
            "gmail-message-1"
        )

        self.assertTrue(was_inserted)
        self.assertTrue(
            self.store.is_message_known(
                "gmail-message-1"
            )
        )
        self.assertIsNotNone(stored_message)
        assert stored_message is not None

        self.assertEqual(stored_message.account_id, 1)
        self.assertEqual(
            stored_message.gmail_message_id,
            "gmail-message-1",
        )
        self.assertEqual(
            stored_message.gmail_thread_id,
            "gmail-thread-1",
        )
        self.assertEqual(
            stored_message.internal_date_ms,
            1_757_721_600_000,
        )
        self.assertEqual(
            stored_message.status,
            "discovered",
        )
        self.assertEqual(
            stored_message.discovered_at,
            discovered_at,
        )
        self.assertIsNone(stored_message.processed_at)
        self.assertIsNone(stored_message.last_error_code)

    def test_duplicate_message_is_idempotent(
        self,
    ) -> None:
        self.prepare_ingestion()

        first_insert = self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-original",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )

        duplicate_insert = self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-changed",
            internal_date_ms=1_757_725_200_000,
            discovered_at=(
                STARTED_AT + timedelta(hours=1)
            ),
        )

        stored_message = self.store.load_message(
            "gmail-message-1"
        )

        self.assertTrue(first_insert)
        self.assertFalse(duplicate_insert)
        self.assertIsNotNone(stored_message)
        assert stored_message is not None

        self.assertEqual(
            stored_message.gmail_thread_id,
            "gmail-thread-original",
        )
        self.assertEqual(
            stored_message.internal_date_ms,
            1_757_721_600_000,
        )

    def test_message_identifiers_are_required(
        self,
    ) -> None:
        self.prepare_ingestion()

        with self.assertRaises(IngestionStoreError):
            self.store.register_message(
                gmail_message_id="",
                gmail_thread_id="gmail-thread-1",
                internal_date_ms=1,
            )

        with self.assertRaises(IngestionStoreError):
            self.store.register_message(
                gmail_message_id="gmail-message-1",
                gmail_thread_id="   ",
                internal_date_ms=1,
            )

    def test_internal_date_must_be_valid(
        self,
    ) -> None:
        self.prepare_ingestion()

        invalid_values = (
            -1,
            True,
            1.5,
        )

        for invalid_value in invalid_values:
            with self.subTest(value=invalid_value):
                with self.assertRaises(
                    IngestionStoreError
                ):
                    self.store.register_message(
                        gmail_message_id=(
                            f"message-{invalid_value}"
                        ),
                        gmail_thread_id="thread-1",
                        internal_date_ms=invalid_value,
                    )

    def test_message_timestamp_requires_timezone(
        self,
    ) -> None:
        self.prepare_ingestion()

        naive_timestamp = datetime(
            2026,
            9,
            13,
            10,
            0,
        )

        with self.assertRaises(IngestionStoreError):
            self.store.register_message(
                gmail_message_id="gmail-message-1",
                gmail_thread_id="gmail-thread-1",
                internal_date_ms=1,
                discovered_at=naive_timestamp,
            )

    def test_unknown_message_is_not_known(
        self,
    ) -> None:
        self.prepare_ingestion()

        self.assertFalse(
            self.store.is_message_known(
                "unknown-message"
            )
        )
        self.assertIsNone(
            self.store.load_message(
                "unknown-message"
            )
        )

if __name__ == "__main__":
    unittest.main()
