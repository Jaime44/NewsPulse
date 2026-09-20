from __future__ import annotations

import unittest

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from app.backend.bd.ingestion_store import (
    IngestionState,
    IngestionStoreError,
)
from app.backend.services.gmail_ingestion import (
    GmailIngestionError,
    GmailIngestionService,
    IngestionScanResult,
)
from app.tools.gmail.gmail_client import (
    GmailClientError,
    GmailListingLimitReached,
    GmailMessageMetadata,
    GmailMessageReference,
)


APPLICATION_STARTED_AT = datetime(
    2026,
    9,
    20,
    10,
    0,
    tzinfo=timezone.utc,
)

SCAN_STARTED_AT = datetime(
    2026,
    9,
    20,
    10,
    5,
    tzinfo=timezone.utc,
)


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


class GmailIngestionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gmail_client = Mock()
        self.store = Mock()

        self.state = IngestionState(
            account_id=1,
            started_at=APPLICATION_STARTED_AT,
            last_successful_scan_at=None,
            created_at=APPLICATION_STARTED_AT,
            updated_at=APPLICATION_STARTED_AT,
        )

        self.reference = GmailMessageReference(
            message_id="message-1",
            thread_id="thread-1",
        )

        self.metadata = GmailMessageMetadata(
            message_id="message-1",
            thread_id="thread-1",
            internal_date_ms=_timestamp_ms(
                APPLICATION_STARTED_AT
                + timedelta(minutes=1)
            ),
            label_ids=("INBOX",),
            subject="Weekly update",
            sender="Publisher <news@example.com>",
            date_header=None,
            list_id="<weekly.example.com>",
            list_unsubscribe=None,
            precedence="bulk",
            auto_submitted=None,
        )

        self.store.get_or_create_state.return_value = (
            self.state
        )

        self.service = GmailIngestionService(
            gmail_client=self.gmail_client,
            store=self.store,
            clock=lambda: SCAN_STARTED_AT,
        )
        self.service.logger = Mock()

    def test_successful_scan_registers_new_message(
        self,
    ) -> None:
        self.gmail_client.list_message_references.return_value = [
            self.reference
        ]
        self.store.is_message_known.return_value = False
        self.gmail_client.get_message_metadata.return_value = (
            self.metadata
        )
        self.store.register_message.return_value = True

        result = self.service.scan(
            user_id="me",
            total_limit=50,
            page_size=25,
            label_ids=("NEWSLETTERS",),
        )

        expected_query = (
            "after:"
            f"{int(APPLICATION_STARTED_AT.timestamp()) - 1}"
        )

        self.assertEqual(
            result,
            IngestionScanResult(
                scan_started_at=SCAN_STARTED_AT,
                cursor_before=APPLICATION_STARTED_AT,
                cursor_after=SCAN_STARTED_AT,
                gmail_query=expected_query,
                listed_count=1,
                discovered_count=1,
                already_known_count=0,
                ignored_before_start_count=0,
            ),
        )

        self.store.get_or_create_state.assert_called_once_with(
            started_at=SCAN_STARTED_AT
        )

        (
            self.gmail_client
            .list_message_references
            .assert_called_once_with(
                user_id="me",
                total_limit=50,
                page_size=25,
                query=expected_query,
                label_ids=("NEWSLETTERS",),
                include_spam_trash=False,
                require_complete=True,
            )
        )

        self.store.is_message_known.assert_called_once_with(
            "message-1"
        )

        (
            self.gmail_client
            .get_message_metadata
            .assert_called_once_with(
                message_id="message-1",
                user_id="me",
            )
        )

        self.store.register_message.assert_called_once_with(
            gmail_message_id="message-1",
            gmail_thread_id="thread-1",
            internal_date_ms=(
                self.metadata.internal_date_ms
            ),
            discovered_at=SCAN_STARTED_AT,
        )

        self.store.mark_scan_successful.assert_called_once_with(
            completed_at=SCAN_STARTED_AT
        )

    def test_known_message_skips_metadata_request(
        self,
    ) -> None:
        self.gmail_client.list_message_references.return_value = [
            self.reference
        ]
        self.store.is_message_known.return_value = True

        result = self.service.scan()

        self.assertEqual(result.listed_count, 1)
        self.assertEqual(result.discovered_count, 0)
        self.assertEqual(
            result.already_known_count,
            1,
        )
        self.assertEqual(
            result.ignored_before_start_count,
            0,
        )

        (
            self.gmail_client
            .get_message_metadata
            .assert_not_called()
        )
        self.store.register_message.assert_not_called()
        self.store.mark_scan_successful.assert_called_once_with(
            completed_at=SCAN_STARTED_AT
        )

    def test_message_before_application_start_is_ignored(
        self,
    ) -> None:
        historical_metadata = GmailMessageMetadata(
            message_id="message-1",
            thread_id="thread-1",
            internal_date_ms=(
                _timestamp_ms(APPLICATION_STARTED_AT)
                - 1
            ),
            label_ids=("INBOX",),
            subject="Historical message",
            sender="history@example.com",
            date_header=None,
            list_id=None,
            list_unsubscribe=None,
            precedence=None,
            auto_submitted=None,
        )

        self.gmail_client.list_message_references.return_value = [
            self.reference
        ]
        self.store.is_message_known.return_value = False
        self.gmail_client.get_message_metadata.return_value = (
            historical_metadata
        )

        result = self.service.scan()

        self.assertEqual(result.listed_count, 1)
        self.assertEqual(result.discovered_count, 0)
        self.assertEqual(
            result.already_known_count,
            0,
        )
        self.assertEqual(
            result.ignored_before_start_count,
            1,
        )

        self.store.register_message.assert_not_called()
        self.store.mark_scan_successful.assert_called_once_with(
            completed_at=SCAN_STARTED_AT
        )

    def test_existing_cursor_is_used_for_next_scan(
        self,
    ) -> None:
        last_successful_scan = (
            APPLICATION_STARTED_AT
            + timedelta(minutes=3)
        )

        self.store.get_or_create_state.return_value = (
            IngestionState(
                account_id=1,
                started_at=APPLICATION_STARTED_AT,
                last_successful_scan_at=(
                    last_successful_scan
                ),
                created_at=APPLICATION_STARTED_AT,
                updated_at=last_successful_scan,
            )
        )

        self.gmail_client.list_message_references.return_value = []

        result = self.service.scan()

        expected_query = (
            "after:"
            f"{int(last_successful_scan.timestamp()) - 1}"
        )

        self.assertEqual(
            result.cursor_before,
            last_successful_scan,
        )
        self.assertEqual(
            result.cursor_after,
            SCAN_STARTED_AT,
        )
        self.assertEqual(
            result.gmail_query,
            expected_query,
        )

        (
            self.gmail_client
            .list_message_references
            .assert_called_once_with(
                user_id="me",
                total_limit=1_000,
                page_size=100,
                query=expected_query,
                label_ids=None,
                include_spam_trash=False,
                require_complete=True,
            )
        )

    def test_naive_ingestion_clock_is_rejected(
        self,
    ) -> None:
        naive_clock = datetime(
            2026,
            9,
            20,
            10,
            5,
        )

        service = GmailIngestionService(
            gmail_client=self.gmail_client,
            store=self.store,
            clock=lambda: naive_clock,
        )
        service.logger = Mock()

        with self.assertRaises(
            GmailIngestionError
        ) as raised:
            service.scan()

        self.assertEqual(
            str(raised.exception),
            (
                "The ingestion clock must return "
                "a timezone-aware datetime"
            ),
        )

        self.store.get_or_create_state.assert_not_called()
        (
            self.gmail_client
            .list_message_references
            .assert_not_called()
        )

    def test_registration_conflict_counts_as_known(
        self,
    ) -> None:
        self.gmail_client.list_message_references.return_value = [
            self.reference
        ]
        self.store.is_message_known.return_value = False
        self.gmail_client.get_message_metadata.return_value = (
            self.metadata
        )
        self.store.register_message.return_value = False

        result = self.service.scan()

        self.assertEqual(result.listed_count, 1)
        self.assertEqual(result.discovered_count, 0)
        self.assertEqual(
            result.already_known_count,
            1,
        )

        self.store.mark_scan_successful.assert_called_once_with(
            completed_at=SCAN_STARTED_AT
        )

    def test_inconsistent_metadata_does_not_advance_cursor(
        self,
    ) -> None:
        inconsistent_metadata = replace(
            self.metadata,
            message_id="different-message",
        )

        self.gmail_client.list_message_references.return_value = [
            self.reference
        ]
        self.store.is_message_known.return_value = False
        self.gmail_client.get_message_metadata.return_value = (
            inconsistent_metadata
        )

        with self.assertRaises(
            GmailIngestionError
        ) as raised:
            self.service.scan()

        self.assertEqual(
            str(raised.exception),
            (
                "Gmail returned inconsistent "
                "message metadata"
            ),
        )

        self.store.register_message.assert_not_called()
        self.store.mark_scan_successful.assert_not_called()

        self.service.logger.error.assert_called_once_with(
            "Gmail ingestion scan failed: "
            "inconsistent metadata"
        )

    def test_partial_failure_keeps_cursor_for_retry(
        self,
    ) -> None:
        second_reference = GmailMessageReference(
            message_id="message-2",
            thread_id="thread-2",
        )

        self.gmail_client.list_message_references.return_value = [
            self.reference,
            second_reference,
        ]
        self.store.is_message_known.return_value = False

        self.gmail_client.get_message_metadata.side_effect = [
            self.metadata,
            GmailClientError(
                "sensitive-provider-value"
            ),
        ]

        self.store.register_message.return_value = True

        with self.assertRaises(
            GmailIngestionError
        ) as raised:
            self.service.scan()

        self.assertEqual(
            str(raised.exception),
            "Gmail ingestion scan failed",
        )

        self.store.register_message.assert_called_once_with(
            gmail_message_id="message-1",
            gmail_thread_id="thread-1",
            internal_date_ms=(
                self.metadata.internal_date_ms
            ),
            discovered_at=SCAN_STARTED_AT,
        )

        self.store.mark_scan_successful.assert_not_called()

        logged_value = (
            self.service
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertIn(
            "GmailClientError",
            logged_value,
        )
        self.assertNotIn(
            "sensitive-provider-value",
            logged_value,
        )

    def test_storage_failure_does_not_advance_cursor(
        self,
    ) -> None:
        self.gmail_client.list_message_references.return_value = [
            self.reference
        ]
        self.store.is_message_known.return_value = False
        self.gmail_client.get_message_metadata.return_value = (
            self.metadata
        )

        self.store.register_message.side_effect = (
            IngestionStoreError(
                "sensitive-database-value"
            )
        )

        with self.assertRaises(
            GmailIngestionError
        ) as raised:
            self.service.scan()

        self.assertEqual(
            str(raised.exception),
            "Gmail ingestion scan failed",
        )

        self.store.mark_scan_successful.assert_not_called()

        logged_value = (
            self.service
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertIn(
            "IngestionStoreError",
            logged_value,
        )
        self.assertNotIn(
            "sensitive-database-value",
            logged_value,
        )

    def test_unexpected_failure_is_sanitized(
        self,
    ) -> None:
        self.gmail_client.list_message_references.return_value = [
            self.reference
        ]

        self.store.is_message_known.side_effect = (
            RuntimeError(
                "sensitive-runtime-value"
            )
        )

        with self.assertRaises(
            GmailIngestionError
        ) as raised:
            self.service.scan()

        self.assertEqual(
            str(raised.exception),
            "Gmail ingestion scan failed",
        )

        self.store.mark_scan_successful.assert_not_called()

        logged_value = (
            self.service
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertIn(
            "RuntimeError",
            logged_value,
        )
        self.assertNotIn(
            "sensitive-runtime-value",
            logged_value,
        )

    def test_result_limit_does_not_advance_cursor(
        self,
    ) -> None:
        (
            self.gmail_client
            .list_message_references
            .side_effect
        ) = GmailListingLimitReached(
            "sensitive-pagination-value"
        )

        with self.assertRaises(
            GmailIngestionError
        ) as raised:
            self.service.scan()

        self.assertEqual(
            str(raised.exception),
            "Gmail ingestion result limit reached",
        )

        self.store.is_message_known.assert_not_called()
        (
            self.gmail_client
            .get_message_metadata
            .assert_not_called()
        )
        self.store.register_message.assert_not_called()
        self.store.mark_scan_successful.assert_not_called()

        self.service.logger.error.assert_called_once_with(
            "Gmail ingestion scan failed: "
            "result limit reached"
        )

        logged_value = (
            self.service
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertNotIn(
            "sensitive-pagination-value",
            logged_value,
        )


if __name__ == "__main__":
    unittest.main()
