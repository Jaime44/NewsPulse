"""Compose Gmail decoding, parsing, and retained-content persistence.

This service forms the content-processing boundary used by ingestion and maps
lower-level retrieval, parsing, and storage failures to one stable exception.
"""

from __future__ import annotations

from datetime import datetime

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

        try:
            return self.store.purge_expired(
                expired_at=expired_at
            )
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

            return self.store.save_content(
                gmail_message_id,
                text=extracted_content.text,
                links=extracted_content.links,
                source_mime_type=(
                    extracted_content
                    .source_mime_type
                ),
                extracted_at=extracted_at,
            )

        except (
            MessageContentError,
            ContentParsingError,
            MessageContentStoreError,
        ) as exc:
            raise MessageContentProcessingError(
                "Failed to process Gmail "
                "message content"
            ) from exc
