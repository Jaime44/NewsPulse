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
    """Decode, parse and persist one Gmail message."""

    def __init__(
        self,
        content_service: (
            GmailMessageContentService
        ),
        parser: MessageContentParser,
        store: MessageContentStore,
    ) -> None:
        self.content_service = (
            content_service
        )
        self.parser = parser
        self.store = store

    def purge_expired(
        self,
        expired_at: datetime | None = None,
    ) -> int:
        """Remove readable content whose retention period ended."""

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
        """Process and persist readable content for one message."""

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