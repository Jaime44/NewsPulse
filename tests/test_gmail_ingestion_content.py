from __future__ import annotations

import unittest

from datetime import (
    datetime,
    timedelta,
    timezone,
)
from unittest.mock import Mock

from app.backend.bd.ingestion_store import (
    IngestionState,
)
from app.backend.services.classifier import (
    NewsletterClassification,
    NewsletterVerdict,
)
from app.backend.services.gmail_ingestion import (
    GmailIngestionError,
    GmailIngestionService,
)
from app.backend.services.message_content_processing import (
    MessageContentProcessingError,
)
from app.tools.gmail.gmail_client import (
    GmailMessageMetadata,
    GmailMessageReference,
)


APPLICATION_STARTED_AT = datetime(
    2026,
    10,
    8,
    10,
    0,
    tzinfo=timezone.utc,
)

SCAN_STARTED_AT = (
    APPLICATION_STARTED_AT
    + timedelta(minutes=5)
)


def _timestamp_ms(
    value: datetime,
) -> int:
    return int(
        value.timestamp() * 1_000
    )


class GmailIngestionContentTests(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.gmail_client = Mock()
        self.store = Mock()
        self.classification_service = Mock()
        self.content_processing_service = Mock()

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
            subject="Weekly newsletter",
            sender="news@example.com",
            date_header=None,
            list_id="<weekly.example.com>",
            list_unsubscribe=None,
            precedence="bulk",
            auto_submitted=None,
        )

        (
            self.store
            .get_or_create_state
            .return_value
        ) = self.state

        (
            self.store
            .load_message_classification
            .return_value
        ) = None

        (
            self.gmail_client
            .list_message_references
            .return_value
        ) = [
            self.reference,
        ]

        (
            self.store
            .is_message_known
            .return_value
        ) = False

        (
            self.gmail_client
            .get_message_metadata
            .return_value
        ) = self.metadata

        (
            self.store
            .register_message
            .return_value
        ) = True

        self.service = GmailIngestionService(
            gmail_client=self.gmail_client,
            store=self.store,
            classification_service=(
                self.classification_service
            ),
            content_processing_service=(
                self.content_processing_service
            ),
            clock=lambda: SCAN_STARTED_AT,
        )
        self.service.logger = Mock()

    def configure_verdict(
        self,
        verdict: NewsletterVerdict,
        *,
        score: int,
    ) -> None:
        (
            self.classification_service
            .classify
            .return_value
        ) = NewsletterClassification(
            verdict=verdict,
            score=score,
            reasons=("test_rule",),
        )

    def test_new_newsletter_content_is_processed(
        self,
    ) -> None:
        self.configure_verdict(
            NewsletterVerdict.NEWSLETTER,
            score=5,
        )

        result = self.service.scan(
            user_id="me"
        )

        self.assertEqual(
            result.newsletter_count,
            1,
        )
        self.assertEqual(
            result.review_count,
            0,
        )
        self.assertEqual(
            result.not_newsletter_count,
            0,
        )

        (
            self.store
            .save_message_classification
            .assert_called_once_with(
                "message-1",
                verdict="newsletter",
                score=5,
                reasons=("test_rule",),
                classifier_version="rules-v1",
                classified_at=SCAN_STARTED_AT,
            )
        )

        (
            self.store
            .mark_message_processing
            .assert_called_once_with(
                "message-1",
                started_at=SCAN_STARTED_AT,
            )
        )

        (
            self.content_processing_service
            .process_message
            .assert_called_once_with(
                "message-1",
                user_id="me",
                extracted_at=SCAN_STARTED_AT,
            )
        )

        (
            self.store
            .mark_message_processed
            .assert_called_once_with(
                "message-1",
                completed_at=SCAN_STARTED_AT,
            )
        )

        (
            self.store
            .mark_message_failed
            .assert_not_called()
        )

        (
            self.store
            .mark_scan_successful
            .assert_called_once_with(
                completed_at=SCAN_STARTED_AT
            )
        )

    def test_review_message_content_is_not_processed(
        self,
    ) -> None:
        self.configure_verdict(
            NewsletterVerdict.REVIEW,
            score=2,
        )

        result = self.service.scan()

        self.assertEqual(
            result.newsletter_count,
            0,
        )
        self.assertEqual(
            result.review_count,
            1,
        )
        self.assertEqual(
            result.not_newsletter_count,
            0,
        )

        (
            self.content_processing_service
            .process_message
            .assert_not_called()
        )
        (
            self.store
            .mark_message_processing
            .assert_not_called()
        )
        (
            self.store
            .mark_message_processed
            .assert_not_called()
        )
        (
            self.store
            .mark_message_failed
            .assert_not_called()
        )

    def test_non_newsletter_content_is_not_processed(
        self,
    ) -> None:
        self.configure_verdict(
            NewsletterVerdict.NOT_NEWSLETTER,
            score=0,
        )

        result = self.service.scan()

        self.assertEqual(
            result.newsletter_count,
            0,
        )
        self.assertEqual(
            result.review_count,
            0,
        )
        self.assertEqual(
            result.not_newsletter_count,
            1,
        )

        (
            self.content_processing_service
            .process_message
            .assert_not_called()
        )
        (
            self.store
            .mark_message_processing
            .assert_not_called()
        )
        (
            self.store
            .mark_message_processed
            .assert_not_called()
        )
        (
            self.store
            .mark_message_failed
            .assert_not_called()
        )

    def test_content_failure_marks_message_failed(
        self,
    ) -> None:
        self.configure_verdict(
            NewsletterVerdict.NEWSLETTER,
            score=5,
        )

        processing_error = (
            MessageContentProcessingError(
                "sensitive-content-error"
            )
        )

        (
            self.content_processing_service
            .process_message
            .side_effect
        ) = processing_error

        with self.assertRaises(
            GmailIngestionError
        ) as raised:
            self.service.scan()

        self.assertEqual(
            str(raised.exception),
            "Gmail ingestion scan failed",
        )
        self.assertIs(
            raised.exception.__cause__,
            processing_error,
        )

        (
            self.store
            .mark_message_processing
            .assert_called_once_with(
                "message-1",
                started_at=SCAN_STARTED_AT,
            )
        )

        (
            self.store
            .mark_message_failed
            .assert_called_once_with(
                "message-1",
                error_code=(
                    "content_processing_failed"
                ),
                failed_at=SCAN_STARTED_AT,
            )
        )

        (
            self.store
            .mark_message_processed
            .assert_not_called()
        )
        (
            self.store
            .mark_scan_successful
            .assert_not_called()
        )

        logged_value = (
            self.service
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertIn(
            "MessageContentProcessingError",
            logged_value,
        )
        self.assertNotIn(
            "sensitive-content-error",
            logged_value,
        )

    def test_failed_newsletter_content_is_retried(
        self,
    ) -> None:
        stored_classification = Mock()
        stored_classification.verdict = (
            "newsletter"
        )

        stored_message = Mock()
        stored_message.status = "failed"

        (
            self.store
            .load_message_classification
            .return_value
        ) = stored_classification

        (
            self.store
            .load_message
            .return_value
        ) = stored_message

        result = self.service.scan(
            user_id="me"
        )

        self.assertEqual(
            result.already_known_count,
            1,
        )
        self.assertEqual(
            result.discovered_count,
            0,
        )

        (
            self.gmail_client
            .get_message_metadata
            .assert_not_called()
        )
        (
            self.classification_service
            .classify
            .assert_not_called()
        )

        (
            self.store
            .load_message
            .assert_called_once_with(
                "message-1"
            )
        )

        (
            self.store
            .mark_message_processing
            .assert_called_once_with(
                "message-1",
                started_at=SCAN_STARTED_AT,
            )
        )

        (
            self.content_processing_service
            .process_message
            .assert_called_once_with(
                "message-1",
                user_id="me",
                extracted_at=SCAN_STARTED_AT,
            )
        )

        (
            self.store
            .mark_message_processed
            .assert_called_once_with(
                "message-1",
                completed_at=SCAN_STARTED_AT,
            )
        )

        (
            self.store
            .mark_scan_successful
            .assert_called_once_with(
                completed_at=SCAN_STARTED_AT
            )
        )

    def test_processed_newsletter_is_not_downloaded_again(
        self,
    ) -> None:
        stored_classification = Mock()
        stored_classification.verdict = (
            "newsletter"
        )

        stored_message = Mock()
        stored_message.status = "processed"

        (
            self.store
            .load_message_classification
            .return_value
        ) = stored_classification

        (
            self.store
            .load_message
            .return_value
        ) = stored_message

        result = self.service.scan()

        self.assertEqual(
            result.already_known_count,
            1,
        )
        self.assertEqual(
            result.discovered_count,
            0,
        )

        (
            self.store
            .load_message
            .assert_called_once_with(
                "message-1"
            )
        )

        (
            self.content_processing_service
            .process_message
            .assert_not_called()
        )
        (
            self.store
            .mark_message_processing
            .assert_not_called()
        )
        (
            self.store
            .mark_message_processed
            .assert_not_called()
        )
        (
            self.store
            .mark_message_failed
            .assert_not_called()
        )

        (
            self.store
            .mark_scan_successful
            .assert_called_once_with(
                completed_at=SCAN_STARTED_AT
            )
        )

    def test_scan_purges_expired_content(
        self,
    ) -> None:
        (
            self.gmail_client
            .list_message_references
            .return_value
        ) = []

        result = self.service.scan()

        self.assertEqual(
            result.listed_count,
            0,
        )

        (
            self.content_processing_service
            .purge_expired
            .assert_called_once_with(
                expired_at=SCAN_STARTED_AT
            )
        )

        (
            self.store
            .get_or_create_state
            .assert_called_once_with(
                started_at=SCAN_STARTED_AT
            )
        )

        (
            self.store
            .mark_scan_successful
            .assert_called_once_with(
                completed_at=SCAN_STARTED_AT
            )
        )

    def test_purge_failure_stops_scan_before_gmail(
        self,
    ) -> None:
        purge_error = (
            MessageContentProcessingError(
                "sensitive-purge-detail"
            )
        )

        (
            self.content_processing_service
            .purge_expired
            .side_effect
        ) = purge_error

        with self.assertRaises(
            GmailIngestionError
        ) as raised:
            self.service.scan()

        self.assertEqual(
            str(raised.exception),
            "Gmail ingestion scan failed",
        )
        self.assertIs(
            raised.exception.__cause__,
            purge_error,
        )

        (
            self.content_processing_service
            .purge_expired
            .assert_called_once_with(
                expired_at=SCAN_STARTED_AT
            )
        )

        (
            self.store
            .get_or_create_state
            .assert_not_called()
        )
        (
            self.gmail_client
            .list_message_references
            .assert_not_called()
        )
        (
            self.store
            .mark_scan_successful
            .assert_not_called()
        )

        logged_value = (
            self.service
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertIn(
            "MessageContentProcessingError",
            logged_value,
        )
        self.assertNotIn(
            "sensitive-purge-detail",
            logged_value,
        )

if __name__ == "__main__":
    unittest.main()
