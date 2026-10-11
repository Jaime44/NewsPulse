"""Compose Gmail decoding, parsing, and retained-content persistence.

This service forms the content-processing boundary used by ingestion and maps
lower-level retrieval, parsing, and storage failures to one stable exception.
"""

from __future__ import annotations

from datetime import datetime
from time import perf_counter

from app.backend.bd.message_content_store import (
    MessageContentStore,
    MessageContentStoreError,
    StoredMessageContent,
)
from app.backend.services.content_parser import (
    ContentParsingError,
    MessageContentParser,
)
from app.backend.services.message_content import (
    GmailMessageContentService,
    MessageContentError,
)
from app.tools.logger import (
    AppLogger,
    is_error_logged,
    mark_error_logged,
)


class MessageContentProcessingError(
    RuntimeError
):
    """Raised when Gmail message content cannot be processed."""


class MessageContentProcessingService:
    """Decode, parse, and persist readable content for Gmail messages.

    Attributes:
        content_service: MIME retrieval and decoding service.
        parser: Readable-text and link extraction service.
        store: Retained-content repository.
    """

    def __init__(
        self,
        content_service: (
            GmailMessageContentService
        ),
        parser: MessageContentParser,
        store: MessageContentStore,
    ) -> None:
        """Bind the three stages of the message-content pipeline.

        Args:
            content_service: MIME retrieval and decoding service.
            parser: Readable-text and validated-link parser.
            store: Parsed-content persistence repository.
        """

        self.content_service = (
            content_service
        )
        self.parser = parser
        self.store = store
        self.logger = AppLogger(
            "content_processing.log"
        )

    def purge_expired(
        self,
        expired_at: datetime | None = None,
    ) -> int:
        """Remove readable content whose retention period ended.

        Args:
            expired_at: Optional timezone-aware retention cutoff.

        Returns:
            Number of deleted content records.

        Raises:
            MessageContentProcessingError: If the repository rejects the purge.
        """

        operation_started_at = perf_counter()

        try:
            deleted_count = self.store.purge_expired(
                expired_at=expired_at
            )
            self.logger.debug(
                "Message content retention purge completed: "
                "deleted_count=%d duration_ms=%.2f",
                deleted_count,
                (perf_counter() - operation_started_at) * 1_000,
            )
            return deleted_count
        except MessageContentStoreError as exc:
            raise MessageContentProcessingError(
                "Failed to purge expired "
                "message content"
            ) from exc

    def process_message(
        self,
        gmail_message_id: str,
        *,
        user_id: str = "me",
        extracted_at: datetime | None = None,
    ) -> StoredMessageContent:
        """Process and persist readable content for one message.

        Args:
            gmail_message_id: External Gmail message identifier.
            user_id: Gmail user identifier used for retrieval.
            extracted_at: Optional timezone-aware extraction timestamp.

        Returns:
            The persisted readable-content record.

        Raises:
            MessageContentProcessingError: If decoding, parsing, or storage
                fails.
        """

        operation_started_at = perf_counter()
        self.logger.debug(
            "Message content pipeline started: message_id=%s",
            gmail_message_id,
        )

        try:
            decoded_parts = (
                self.content_service
                .extract_text_parts(
                    message_id=(
                        gmail_message_id
                    ),
                    user_id=user_id,
                )
            )

            extracted_content = (
                self.parser.parse(
                    decoded_parts
                )
            )

            stored_content = self.store.save_content(
                gmail_message_id,
                text=extracted_content.text,
                links=extracted_content.links,
                source_mime_type=(
                    extracted_content
                    .source_mime_type
                ),
                extracted_at=extracted_at,
            )
            self.logger.debug(
                "Message content pipeline completed: message_id=%s "
                "part_count=%d text_chars=%d link_count=%d "
                "source_mime_type=%s duration_ms=%.2f",
                gmail_message_id,
                len(decoded_parts),
                len(extracted_content.text),
                len(extracted_content.links),
                extracted_content.source_mime_type,
                (perf_counter() - operation_started_at) * 1_000,
            )
            return stored_content

        except (
            MessageContentError,
            ContentParsingError,
            MessageContentStoreError,
        ) as exc:
            processing_error = MessageContentProcessingError(
                "Failed to process Gmail "
                "message content"
            )

            if is_error_logged(exc):
                mark_error_logged(
                    processing_error
                )

            raise processing_error from exc
