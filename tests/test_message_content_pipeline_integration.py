from __future__ import annotations

import base64
import unittest

from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app.backend.bd.db import Database
from app.backend.bd.ingestion_store import (
    IngestionStore,
)
from app.backend.bd.message_content_store import (
    MessageContentStore,
)
from app.backend.bd.newsletter_source_store import (
    NewsletterSourceStore,
)
from app.backend.services.classifier import (
    NewsletterClassifier,
)
from app.backend.services.content_parser import (
    MessageContentParser,
)
from app.backend.services.gmail_ingestion import (
    GmailIngestionService,
)
from app.backend.services.message_content import (
    GmailMessageContentService,
)
from app.backend.services.message_content_processing import (
    MessageContentProcessingService,
)
from app.backend.services.newsletter_classification import (
    NewsletterClassificationService,
)
from app.tools.gmail.gmail_client import (
    GmailFullMessage,
    GmailMessageMetadata,
    GmailMessageReference,
    GmailMimePart,
)


FIRST_SCAN_AT = datetime(
    2026,
    10,
    8,
    10,
    0,
    tzinfo=timezone.utc,
)

HTML_CONTENT = (
    b"<h1>Weekly news</h1>"
    b'<a href="'
    b"https://example.com/story#top"
    b'">Read</a>'
)

ENCODED_HTML_CONTENT = (
    base64.urlsafe_b64encode(
        HTML_CONTENT
    )
    .decode("ascii")
    .rstrip("=")
)


class MessageContentPipelineIntegrationTests(
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
            / "integration.db"
        )

        self.database = Database(
            database_path
        )
        self.ingestion_store = (
            IngestionStore(
                self.database
            )
        )
        self.content_store = (
            MessageContentStore(
                self.database,
                retention_days=30,
            )
        )

        self.insert_account()

        self.gmail_client = Mock()

        (
            self.gmail_client
            .list_message_references
            .return_value
        ) = [
            GmailMessageReference(
                message_id="message-1",
                thread_id="thread-1",
            ),
        ]

        (
            self.gmail_client
            .get_message_metadata
            .return_value
        ) = GmailMessageMetadata(
            message_id="message-1",
            thread_id="thread-1",
            internal_date_ms=int(
                (
                    FIRST_SCAN_AT
                    + timedelta(minutes=1)
                ).timestamp()
                * 1_000
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
            self.gmail_client
            .get_full_message
            .return_value
        ) = GmailFullMessage(
            message_id="message-1",
            thread_id="thread-1",
            root_part=GmailMimePart(
                mime_type="text/html",
                filename=None,
                headers=(
                    (
                        "content-type",
                        (
                            "text/html; "
                            "charset=utf-8"
                        ),
                    ),
                ),
                body_size=len(
                    HTML_CONTENT
                ),
                body_data=(
                    ENCODED_HTML_CONTENT
                ),
                attachment_id=None,
                parts=(),
            ),
        )

        self.classification_service = (
            NewsletterClassificationService(
                source_store=(
                    NewsletterSourceStore(
                        self.database
                    )
                ),
                classifier=(
                    NewsletterClassifier()
                ),
            )
        )

        self.content_processing_service = (
            MessageContentProcessingService(
                content_service=(
                    GmailMessageContentService(
                        self.gmail_client
                    )
                ),
                parser=(
                    MessageContentParser()
                ),
                store=self.content_store,
            )
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def insert_account(self) -> None:
        timestamp = (
            FIRST_SCAN_AT.isoformat()
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

    def build_service(
        self,
        scan_at: datetime,
    ) -> GmailIngestionService:
        service = GmailIngestionService(
            gmail_client=self.gmail_client,
            store=self.ingestion_store,
            classification_service=(
                self.classification_service
            ),
            content_processing_service=(
                self.content_processing_service
            ),
            clock=lambda: scan_at,
        )
        service.logger = Mock()

        return service

    def test_newsletter_content_lifecycle(
        self,
    ) -> None:
        first_service = self.build_service(
            FIRST_SCAN_AT
        )

        first_result = first_service.scan()

        stored_message = (
            self.ingestion_store
            .load_message(
                "message-1"
            )
        )
        stored_classification = (
            self.ingestion_store
            .load_message_classification(
                "message-1"
            )
        )
        stored_content = (
            self.content_store
            .load_content(
                "message-1"
            )
        )

        self.assertEqual(
            first_result.newsletter_count,
            1,
        )
        self.assertEqual(
            first_result.discovered_count,
            1,
        )

        self.assertIsNotNone(
            stored_message
        )
        assert stored_message is not None
        self.assertEqual(
            stored_message.status,
            "processed",
        )
        self.assertEqual(
            stored_message.processed_at,
            FIRST_SCAN_AT,
        )
        self.assertIsNone(
            stored_message.last_error_code
        )

        self.assertIsNotNone(
            stored_classification
        )
        assert stored_classification is not None
        self.assertEqual(
            stored_classification.verdict,
            "newsletter",
        )
        self.assertEqual(
            stored_classification.score,
            5,
        )
        self.assertEqual(
            stored_classification.reasons,
            (
                "list_id_header",
                "bulk_precedence",
            ),
        )

        self.assertIsNotNone(
            stored_content
        )
        assert stored_content is not None
        self.assertEqual(
            stored_content.text,
            "Weekly news\nRead",
        )
        self.assertEqual(
            stored_content.links,
            (
                "https://example.com/story",
            ),
        )
        self.assertEqual(
            stored_content.source_mime_type,
            "text/html",
        )
        self.assertEqual(
            stored_content.extracted_at,
            FIRST_SCAN_AT,
        )
        self.assertEqual(
            stored_content.expires_at,
            (
                FIRST_SCAN_AT
                + timedelta(days=30)
            ),
        )

        (
            self.gmail_client
            .get_full_message
            .assert_called_once_with(
                message_id="message-1",
                user_id="me",
            )
        )

        self.gmail_client.get_full_message.reset_mock()
        self.gmail_client.get_message_metadata.reset_mock()

        retry_service = self.build_service(
            FIRST_SCAN_AT
            + timedelta(minutes=5)
        )

        retry_result = retry_service.scan()

        self.assertEqual(
            retry_result.already_known_count,
            1,
        )
        self.assertEqual(
            retry_result.discovered_count,
            0,
        )

        (
            self.gmail_client
            .get_message_metadata
            .assert_not_called()
        )
        (
            self.gmail_client
            .get_full_message
            .assert_not_called()
        )

        retained_content = (
            self.content_store
            .load_content(
                "message-1"
            )
        )

        self.assertEqual(
            retained_content,
            stored_content,
        )

        purge_service = self.build_service(
            FIRST_SCAN_AT
            + timedelta(days=31)
        )

        purge_result = purge_service.scan()

        self.assertEqual(
            purge_result.already_known_count,
            1,
        )
        self.assertIsNone(
            self.content_store.load_content(
                "message-1"
            )
        )

        retained_message = (
            self.ingestion_store
            .load_message(
                "message-1"
            )
        )
        retained_classification = (
            self.ingestion_store
            .load_message_classification(
                "message-1"
            )
        )

        self.assertIsNotNone(
            retained_message
        )
        assert retained_message is not None
        self.assertEqual(
            retained_message.status,
            "processed",
        )
        self.assertEqual(
            retained_classification,
            stored_classification,
        )

        (
            self.gmail_client
            .get_full_message
            .assert_not_called()
        )


if __name__ == "__main__":
    unittest.main()
