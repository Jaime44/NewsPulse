from __future__ import annotations

import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app.backend.bd.db import Database
from app.backend.bd.ingestion_store import (
    IngestionStore,
)
from app.backend.bd.newsletter_source_store import (
    NewsletterSourceStore,
)
from app.backend.services.classifier import (
    NewsletterClassifier,
)
from app.backend.services.newsletter_classification import (
    NewsletterClassificationService,
)
from app.backend.services.gmail_ingestion import (
    GmailIngestionError,
    GmailIngestionService,
)
from app.tools.gmail.gmail_client import (
    GmailClientError,
    GmailMessageMetadata,
    GmailMessageReference,
)


FIRST_SCAN_AT = datetime(
    2026,
    9,
    20,
    10,
    0,
    tzinfo=timezone.utc,
)

RETRY_SCAN_AT = (
    FIRST_SCAN_AT + timedelta(minutes=5)
)


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


class GmailIngestionIntegrationTests(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.temporary_directory = (
            TemporaryDirectory()
        )

        database_path = (
            Path(self.temporary_directory.name)
            / "test.db"
        )

        self.database = Database(database_path)
        self.store = IngestionStore(
            self.database
        )

        self._insert_account()

        self.source_store = NewsletterSourceStore(
            self.database
        )
        self.classification_service = (
            NewsletterClassificationService(
                source_store=self.source_store,
                classifier=NewsletterClassifier(),
            )
        )

        self.gmail_client = Mock()

        self.references = [
            GmailMessageReference(
                message_id="message-1",
                thread_id="thread-1",
            ),
            GmailMessageReference(
                message_id="message-2",
                thread_id="thread-2",
            ),
        ]

        self.first_metadata = GmailMessageMetadata(
            message_id="message-1",
            thread_id="thread-1",
            internal_date_ms=_timestamp_ms(
                FIRST_SCAN_AT
                + timedelta(minutes=1)
            ),
            label_ids=("INBOX",),
            subject="First newsletter",
            sender="first@example.com",
            date_header=None,
            list_id="<first.example.com>",
            list_unsubscribe=None,
            precedence="bulk",
            auto_submitted=None,
        )

        self.second_metadata = GmailMessageMetadata(
            message_id="message-2",
            thread_id="thread-2",
            internal_date_ms=_timestamp_ms(
                FIRST_SCAN_AT
                + timedelta(minutes=2)
            ),
            label_ids=("INBOX",),
            subject="Second newsletter",
            sender="second@example.com",
            date_header=None,
            list_id="<second.example.com>",
            list_unsubscribe=None,
            precedence="bulk",
            auto_submitted=None,
        )

        (
            self.gmail_client
            .list_message_references
            .return_value
        ) = self.references

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _insert_account(self) -> None:
        timestamp = FIRST_SCAN_AT.isoformat()

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

    def _service_at(
        self,
        scan_time: datetime,
    ) -> GmailIngestionService:
        service = GmailIngestionService(
            gmail_client=self.gmail_client,
            store=self.store,
            classification_service=(
                self.classification_service
            ),
            content_processing_service=Mock(),
            clock=lambda: scan_time,
        )
        service.logger = Mock()

        return service

    def test_partial_failure_can_be_retried(
        self,
    ) -> None:
        first_service = self._service_at(
            FIRST_SCAN_AT
        )

        (
            self.gmail_client
            .get_message_metadata
            .side_effect
        ) = [
            self.first_metadata,
            GmailClientError(
                "temporary-provider-error"
            ),
        ]

        with self.assertRaises(
            GmailIngestionError
        ):
            first_service.scan()

        state_after_failure = (
            self.store.load_state()
        )
        first_stored_message = (
            self.store.load_message(
                "message-1"
            )
        )
        first_classification = (
            self.store.load_message_classification(
                "message-1"
            )
        )
        self.assertIsNotNone(
            state_after_failure
        )
        assert state_after_failure is not None

        self.assertIsNone(
            state_after_failure
            .last_successful_scan_at
        )

        self.assertIsNotNone(
            first_stored_message
        )
        self.assertIsNotNone(
            first_classification
        )
        assert first_classification is not None
        self.assertEqual(
            first_classification.verdict,
            "newsletter",
        )
        self.assertIsNone(
            self.store.load_message(
                "message-2"
            )
        )

        (
            self.gmail_client
            .get_message_metadata
            .reset_mock()
        )
        (
            self.gmail_client
            .get_message_metadata
            .side_effect
        ) = None
        (
            self.gmail_client
            .get_message_metadata
            .return_value
        ) = self.second_metadata

        retry_service = self._service_at(
            RETRY_SCAN_AT
        )

        result = retry_service.scan()

        state_after_retry = (
            self.store.load_state()
        )
        second_stored_message = (
            self.store.load_message(
                "message-2"
            )
        )
        second_classification = (
            self.store.load_message_classification(
                "message-2"
            )
        )
        self.assertEqual(
            result.listed_count,
            2,
        )
        self.assertEqual(
            result.discovered_count,
            1,
        )
        self.assertEqual(
            result.already_known_count,
            1,
        )
        self.assertEqual(
            result.ignored_before_start_count,
            0,
        )

        self.assertIsNotNone(
            state_after_retry
        )
        assert state_after_retry is not None

        self.assertEqual(
            state_after_retry
            .last_successful_scan_at,
            RETRY_SCAN_AT,
        )

        self.assertIsNotNone(
            second_stored_message
        )
        self.assertIsNotNone(
            second_classification
        )
        assert second_classification is not None
        self.assertEqual(
            second_classification.verdict,
            "newsletter",
        )
        (
            self.gmail_client
            .get_message_metadata
            .assert_called_once_with(
                message_id="message-2",
                user_id="me",
            )
        )
