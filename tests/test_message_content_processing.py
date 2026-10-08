from __future__ import annotations

import unittest

from datetime import (
    datetime,
    timezone,
)
from unittest.mock import Mock

from app.backend.bd.message_content_store import (
    MessageContentStore,
    MessageContentStoreError,
    StoredMessageContent,
)
from app.backend.services.content_parser import (
    ContentParsingError,
    ExtractedMessageContent,
    MessageContentParser,
)
from app.backend.services.message_content import (
    DecodedTextPart,
    GmailMessageContentService,
    MessageContentError,
)
from app.backend.services.message_content_processing import (
    MessageContentProcessingError,
    MessageContentProcessingService,
)


EXTRACTED_AT = datetime(
    2026,
    10,
    8,
    10,
    0,
    tzinfo=timezone.utc,
)

EXPIRES_AT = datetime(
    2026,
    11,
    7,
    10,
    0,
    tzinfo=timezone.utc,
)


class MessageContentProcessingServiceTests(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.content_service = Mock(
            spec=GmailMessageContentService
        )
        self.parser = Mock(
            spec=MessageContentParser
        )
        self.store = Mock(
            spec=MessageContentStore
        )

        self.service = (
            MessageContentProcessingService(
                content_service=(
                    self.content_service
                ),
                parser=self.parser,
                store=self.store,
            )
        )

        self.decoded_parts = (
            DecodedTextPart(
                mime_type="text/plain",
                text="Weekly newsletter",
            ),
        )

        self.extracted_content = (
            ExtractedMessageContent(
                text="Weekly newsletter",
                links=(
                    "https://example.com/article",
                ),
                source_mime_type="text/plain",
            )
        )

        self.stored_content = (
            StoredMessageContent(
                gmail_message_row_id=7,
                gmail_message_id=(
                    "gmail-message-1"
                ),
                text="Weekly newsletter",
                links=(
                    "https://example.com/article",
                ),
                source_mime_type="text/plain",
                extracted_at=EXTRACTED_AT,
                expires_at=EXPIRES_AT,
            )
        )

    def test_message_is_decoded_parsed_and_stored(
        self,
    ) -> None:
        (
            self.content_service
            .extract_text_parts
            .return_value
        ) = self.decoded_parts

        self.parser.parse.return_value = (
            self.extracted_content
        )

        self.store.save_content.return_value = (
            self.stored_content
        )

        result = self.service.process_message(
            "gmail-message-1",
            user_id="owner",
            extracted_at=EXTRACTED_AT,
        )

        self.assertEqual(
            result,
            self.stored_content,
        )

        (
            self.content_service
            .extract_text_parts
            .assert_called_once_with(
                message_id="gmail-message-1",
                user_id="owner",
            )
        )

        self.parser.parse.assert_called_once_with(
            self.decoded_parts
        )

        (
            self.store
            .save_content
            .assert_called_once_with(
                "gmail-message-1",
                text="Weekly newsletter",
                links=(
                    "https://example.com/article",
                ),
                source_mime_type="text/plain",
                extracted_at=EXTRACTED_AT,
            )
        )

    def test_decoding_error_stops_processing(
        self,
    ) -> None:
        decoding_error = MessageContentError(
            "Unable to decode message"
        )

        (
            self.content_service
            .extract_text_parts
            .side_effect
        ) = decoding_error

        with self.assertRaises(
            MessageContentProcessingError
        ) as raised:
            self.service.process_message(
                "gmail-message-1",
                extracted_at=EXTRACTED_AT,
            )

        self.assertEqual(
            str(raised.exception),
            (
                "Failed to process Gmail "
                "message content"
            ),
        )
        self.assertIs(
            raised.exception.__cause__,
            decoding_error,
        )

        self.parser.parse.assert_not_called()
        self.store.save_content.assert_not_called()

    def test_parsing_error_stops_before_storage(
        self,
    ) -> None:
        (
            self.content_service
            .extract_text_parts
            .return_value
        ) = self.decoded_parts

        parsing_error = ContentParsingError(
            "Unable to parse message"
        )

        self.parser.parse.side_effect = (
            parsing_error
        )

        with self.assertRaises(
            MessageContentProcessingError
        ) as raised:
            self.service.process_message(
                "gmail-message-1",
                extracted_at=EXTRACTED_AT,
            )

        self.assertIs(
            raised.exception.__cause__,
            parsing_error,
        )

        (
            self.content_service
            .extract_text_parts
            .assert_called_once_with(
                message_id="gmail-message-1",
                user_id="me",
            )
        )

        self.store.save_content.assert_not_called()

    def test_storage_error_is_exposed_as_processing_error(
        self,
    ) -> None:
        (
            self.content_service
            .extract_text_parts
            .return_value
        ) = self.decoded_parts

        self.parser.parse.return_value = (
            self.extracted_content
        )

        storage_error = (
            MessageContentStoreError(
                "Unable to store content"
            )
        )

        self.store.save_content.side_effect = (
            storage_error
        )

        with self.assertRaises(
            MessageContentProcessingError
        ) as raised:
            self.service.process_message(
                "gmail-message-1",
                extracted_at=EXTRACTED_AT,
            )

        self.assertIs(
            raised.exception.__cause__,
            storage_error,
        )

        self.parser.parse.assert_called_once_with(
            self.decoded_parts
        )

        (
            self.store
            .save_content
            .assert_called_once_with(
                "gmail-message-1",
                text="Weekly newsletter",
                links=(
                    "https://example.com/article",
                ),
                source_mime_type="text/plain",
                extracted_at=EXTRACTED_AT,
            )
        )

    def test_expired_content_is_purged(
        self,
    ) -> None:
        self.store.purge_expired.return_value = 3

        deleted_count = (
            self.service.purge_expired(
                expired_at=EXTRACTED_AT
            )
        )

        self.assertEqual(
            deleted_count,
            3,
        )

        (
            self.store
            .purge_expired
            .assert_called_once_with(
                expired_at=EXTRACTED_AT
            )
        )

        (
            self.content_service
            .extract_text_parts
            .assert_not_called()
        )
        self.parser.parse.assert_not_called()
        self.store.save_content.assert_not_called()

    def test_purge_error_is_exposed_as_processing_error(
        self,
    ) -> None:
        storage_error = (
            MessageContentStoreError(
                "Sensitive database detail"
            )
        )

        self.store.purge_expired.side_effect = (
            storage_error
        )

        with self.assertRaises(
            MessageContentProcessingError
        ) as raised:
            self.service.purge_expired(
                expired_at=EXTRACTED_AT
            )

        self.assertEqual(
            str(raised.exception),
            (
                "Failed to purge expired "
                "message content"
            ),
        )
        self.assertIs(
            raised.exception.__cause__,
            storage_error,
        )

        (
            self.store
            .purge_expired
            .assert_called_once_with(
                expired_at=EXTRACTED_AT
            )
        )

        (
            self.content_service
            .extract_text_parts
            .assert_not_called()
        )
        self.parser.parse.assert_not_called()
        self.store.save_content.assert_not_called()

if __name__ == "__main__":
    unittest.main()
