from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Dict
from googleapiclient.discovery import Resource


from app.tools.logger import AppLogger
from app.tools.gmail.messages_client import (
    MessagesClient as GmailMessagesClient,
    MessagesClientError,
)
from app.tools.gmail.user_client import UsersClient as GmailUsersClient


class GmailClientError(Exception):
    """
    Raised when a high-level Gmail operation fails.
    """

    pass


class GmailListingLimitReached(GmailClientError):
    """Raised when more Gmail results remain after the limit."""


@dataclass(frozen=True, slots=True)
class GmailMessageReference:
    message_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class GmailMessageMetadata:
    message_id: str
    thread_id: str
    internal_date_ms: int
    label_ids: tuple[str, ...]
    subject: str | None
    sender: str | None
    date_header: str | None
    list_id: str | None
    list_unsubscribe: str | None
    precedence: str | None
    auto_submitted: str | None

@dataclass(frozen=True, slots=True)
class GmailMimePart:
    """One validated part of a Gmail MIME tree."""

    mime_type: str
    filename: str | None
    headers: tuple[tuple[str, str], ...]
    body_size: int
    body_data: str | None
    attachment_id: str | None
    parts: tuple["GmailMimePart", ...]

@dataclass(frozen=True, slots=True)
class GmailFullMessage:
    """A validated Gmail message with its MIME tree."""

    message_id: str
    thread_id: str
    root_part: GmailMimePart

@dataclass(frozen=True, slots=True)
class GmailAttachmentData:
    """Validated encoded data from one Gmail attachment."""

    size: int
    encoded_data: str


def _parse_mime_part(
    raw_part: object,
    *,
    depth: int,
    max_depth: int,
    part_counter: list[int],
    max_parts: int,
) -> GmailMimePart:
    """Validate one MIME part and all its children."""

    if not isinstance(raw_part, dict):
        raise GmailClientError(
            "Gmail returned an invalid MIME part"
        )

    if depth > max_depth:
        raise GmailClientError(
            "Gmail MIME nesting limit exceeded"
        )

    part_counter[0] += 1

    if part_counter[0] > max_parts:
        raise GmailClientError(
            "Gmail MIME part limit exceeded"
        )

    raw_mime_type = raw_part.get(
        "mimeType"
    )

    if (
        not isinstance(raw_mime_type, str)
        or not raw_mime_type.strip()
    ):
        raise GmailClientError(
            "Gmail returned an invalid MIME type"
        )

    raw_filename = raw_part.get(
        "filename",
        "",
    )

    if not isinstance(raw_filename, str):
        raise GmailClientError(
            "Gmail returned an invalid MIME filename"
        )

    raw_headers = raw_part.get(
        "headers",
        [],
    )

    if not isinstance(raw_headers, list):
        raise GmailClientError(
            "Gmail returned invalid MIME headers"
        )

    headers: list[tuple[str, str]] = []

    for raw_header in raw_headers:
        if not isinstance(raw_header, dict):
            raise GmailClientError(
                "Gmail returned an invalid MIME header"
            )

        name = raw_header.get("name")
        value = raw_header.get("value")

        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(value, str)
        ):
            raise GmailClientError(
                "Gmail returned an invalid MIME header"
            )

        headers.append(
            (
                name.strip().casefold(),
                value.strip(),
            )
        )

    raw_body = raw_part.get(
        "body",
        {},
    )

    if not isinstance(raw_body, dict):
        raise GmailClientError(
            "Gmail returned an invalid MIME body"
        )

    raw_size = raw_body.get(
        "size",
        0,
    )

    if (
        isinstance(raw_size, bool)
        or not isinstance(raw_size, int)
        or raw_size < 0
    ):
        raise GmailClientError(
            "Gmail returned an invalid MIME body size"
        )

    raw_data = raw_body.get("data")

    if (
        raw_data is not None
        and not isinstance(raw_data, str)
    ):
        raise GmailClientError(
            "Gmail returned invalid MIME body data"
        )

    raw_attachment_id = raw_body.get(
        "attachmentId"
    )

    if raw_attachment_id is not None:
        if (
            not isinstance(raw_attachment_id, str)
            or not raw_attachment_id.strip()
        ):
            raise GmailClientError(
                "Gmail returned an invalid attachment ID"
            )

        attachment_id = (
            raw_attachment_id.strip()
        )
    else:
        attachment_id = None

    raw_parts = raw_part.get(
        "parts",
        [],
    )

    if not isinstance(raw_parts, list):
        raise GmailClientError(
            "Gmail returned invalid nested MIME parts"
        )

    parts = tuple(
        _parse_mime_part(
            child,
            depth=depth + 1,
            max_depth=max_depth,
            part_counter=part_counter,
            max_parts=max_parts,
        )
        for child in raw_parts
    )

    normalized_filename = (
        raw_filename.strip() or None
    )

    return GmailMimePart(
        mime_type=(
            raw_mime_type.strip().casefold()
        ),
        filename=normalized_filename,
        headers=tuple(headers),
        body_size=raw_size,
        body_data=raw_data,
        attachment_id=attachment_id,
        parts=parts,
    )

class GmailClient:
    """
    High-level wrapper for interacting with Gmail API clients (Messages, Labels, Drafts, Users, etc.).

    This class orchestrates lower-level clients such as MessagesClient and provides
    a unified interface for Gmail operations.
    """

    MAX_TOTAL_RESULTS = 10_000
    MAX_MIME_DEPTH = 20
    MAX_MIME_PARTS = 500

    MESSAGE_METADATA_HEADERS = (
        "From",
        "Subject",
        "Date",
        "List-Id",
        "List-Unsubscribe",
        "Precedence",
        "Auto-Submitted",
    )

    def __init__(self, service: Resource) -> None:
        """
        Initialize GmailClient with an authorized Gmail API service.

        Args:
            service (Resource): Authorized Gmail API service instance.
        """
        self.logger: AppLogger = AppLogger("gmail_client.log")

        # Sub-clients for Gmail resources
        self.messages: GmailMessagesClient = GmailMessagesClient(service)
        self.users = GmailUsersClient(service)

        # self.labels = GmailLabelsClient(service)
        # self.drafts = GmailDraftsClient(service)

    def get_profile(self, user_id: str = "me") -> Dict[str, Any]:
        """
        Get Gmail profile for a user.

        Args:
            user_id (str): User identifier. Use "me" for the authenticated user.

        Returns:
            Dict[str, Any]: User profile information.
        """
        try:
            return self.users.get_profile(user_id=user_id)
        except GmailClientError as e:
            self.logger.error(f"Failed to get profile for user '{user_id}': {e}")
            raise GmailClientError("Get profile failed") from e
          
    def list_message_references(
        self,
        user_id: str = "me",
        total_limit: int = 1_000,
        page_size: int = 100,
        query: str | None = None,
        label_ids: Sequence[str] | None = None,
        include_spam_trash: bool = False,
        require_complete: bool = False,
    ) -> list[GmailMessageReference]:
        """List validated Gmail references across pages."""

        if (
            isinstance(total_limit, bool)
            or not isinstance(total_limit, int)
            or not 1 <= total_limit <= self.MAX_TOTAL_RESULTS
        ):
            raise GmailClientError(
                "total_limit must be between 1 and "
                f"{self.MAX_TOTAL_RESULTS}"
            )

        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 500
        ):
            raise GmailClientError(
                "page_size must be between 1 and 500"
            )

        if not isinstance(require_complete, bool):
            raise GmailClientError(
                "require_complete must be boolean"
            )

        references: list[GmailMessageReference] = []
        page_token: str | None = None
        seen_page_tokens: set[str] = set()

        try:
            while len(references) < total_limit:
                remaining = total_limit - len(references)
                current_page_size = min(
                    page_size,
                    remaining,
                )

                response = self.messages.list_messages(
                    user_id=user_id,
                    max_results=current_page_size,
                    page_token=page_token,
                    query=query,
                    label_ids=label_ids,
                    include_spam_trash=(
                        include_spam_trash
                    ),
                )

                if not isinstance(response, dict):
                    raise GmailClientError(
                        "Gmail returned an invalid list response"
                    )

                page_messages = response.get(
                    "messages",
                    [],
                )

                if not isinstance(page_messages, list):
                    raise GmailClientError(
                        "Gmail returned an invalid messages list"
                    )

                for message in page_messages:
                    if not isinstance(message, dict):
                        raise GmailClientError(
                            "Gmail returned an invalid message "
                            "reference"
                        )

                    message_id = message.get("id")
                    thread_id = message.get("threadId")

                    if (
                        not isinstance(message_id, str)
                        or not message_id.strip()
                        or not isinstance(thread_id, str)
                        or not thread_id.strip()
                    ):
                        raise GmailClientError(
                            "Gmail returned an incomplete message "
                            "reference"
                        )

                    references.append(
                        GmailMessageReference(
                            message_id=message_id.strip(),
                            thread_id=thread_id.strip(),
                        )
                    )

                    if len(references) >= total_limit:
                        break

                next_page_token = response.get(
                    "nextPageToken"
                )
                normalized_page_token: str | None = None

                if next_page_token is not None:
                    if (
                        not isinstance(
                            next_page_token,
                            str,
                        )
                        or not next_page_token.strip()
                    ):
                        raise GmailClientError(
                            "Gmail returned an invalid page token"
                        )

                    normalized_page_token = (
                        next_page_token.strip()
                    )

                if len(references) >= total_limit:
                    if (
                        require_complete
                        and normalized_page_token
                        is not None
                    ):
                        raise GmailListingLimitReached(
                            "Gmail result limit reached "
                            "before pagination completed"
                        )

                    break

                if normalized_page_token is None:
                    break

                if not page_messages:
                    raise GmailClientError(
                        "Gmail returned an empty paginated page"
                    )

                if (
                    normalized_page_token
                    in seen_page_tokens
                ):
                    raise GmailClientError(
                        "Gmail repeated a page token"
                    )

                seen_page_tokens.add(
                    normalized_page_token
                )
                page_token = normalized_page_token

        except MessagesClientError as exc:
            self.logger.error(
                "Gmail message pagination failed"
            )
            raise GmailClientError(
                "Failed to list Gmail messages"
            ) from exc

        return references

    def get_message_metadata(
        self,
        message_id: str,
        user_id: str = "me",
    ) -> GmailMessageMetadata:
        """Retrieve and validate metadata for one Gmail message."""

        try:
            response = self.messages.get_message(
                message_id=message_id,
                user_id=user_id,
                message_format="metadata",
                metadata_headers=(
                    self.MESSAGE_METADATA_HEADERS
                ),
            )
        except MessagesClientError as exc:
            self.logger.error(
                "Gmail message metadata retrieval failed"
            )
            raise GmailClientError(
                "Failed to retrieve Gmail message metadata"
            ) from exc
        except Exception as exc:
            self.logger.error(
                "Gmail message metadata retrieval failed: "
                f"{type(exc).__name__}"
            )
            raise GmailClientError(
                "Failed to retrieve Gmail message metadata"
            ) from exc

        if not isinstance(response, dict):
            raise GmailClientError(
                "Gmail returned invalid message metadata"
            )

        raw_message_id = response.get("id")
        raw_thread_id = response.get("threadId")

        if (
            not isinstance(raw_message_id, str)
            or not raw_message_id.strip()
            or not isinstance(raw_thread_id, str)
            or not raw_thread_id.strip()
        ):
            raise GmailClientError(
                "Gmail returned incomplete message metadata"
            )

        raw_internal_date = response.get(
            "internalDate"
        )

        if (
            not isinstance(raw_internal_date, str)
            or not raw_internal_date.strip().isdigit()
        ):
            raise GmailClientError(
                "Gmail returned an invalid internal date"
            )

        internal_date_ms = int(
            raw_internal_date.strip()
        )

        raw_label_ids = response.get(
            "labelIds",
            [],
        )

        if not isinstance(raw_label_ids, list):
            raise GmailClientError(
                "Gmail returned invalid message labels"
            )

        label_ids: list[str] = []

        for label_id in raw_label_ids:
            if (
                not isinstance(label_id, str)
                or not label_id.strip()
            ):
                raise GmailClientError(
                    "Gmail returned an invalid message label"
                )

            label_ids.append(label_id.strip())

        payload = response.get("payload")

        if not isinstance(payload, dict):
            raise GmailClientError(
                "Gmail returned an invalid message payload"
            )

        raw_headers = payload.get(
            "headers",
            [],
        )

        if not isinstance(raw_headers, list):
            raise GmailClientError(
                "Gmail returned invalid message headers"
            )

        headers: dict[str, str] = {}

        for header in raw_headers:
            if not isinstance(header, dict):
                raise GmailClientError(
                    "Gmail returned an invalid message header"
                )

            name = header.get("name")
            value = header.get("value")

            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(value, str)
            ):
                raise GmailClientError(
                    "Gmail returned an invalid message header"
                )

            headers.setdefault(
                name.strip().lower(),
                value.strip(),
            )

        return GmailMessageMetadata(
            message_id=raw_message_id.strip(),
            thread_id=raw_thread_id.strip(),
            internal_date_ms=internal_date_ms,
            label_ids=tuple(label_ids),
            subject=headers.get("subject") or None,
            sender=headers.get("from") or None,
            date_header=headers.get("date") or None,
            list_id=headers.get("list-id") or None,
            list_unsubscribe=(
                headers.get("list-unsubscribe")
                or None
            ),
            precedence=(
                headers.get("precedence")
                or None
            ),
            auto_submitted=(
                headers.get("auto-submitted")
                or None
            ),
        )

    def get_full_message(
        self,
        message_id: str,
        user_id: str = "me",
    ) -> GmailFullMessage:
        """Retrieve and validate one complete Gmail message."""

        if (
            not isinstance(message_id, str)
            or not message_id.strip()
        ):
            raise GmailClientError(
                "message_id is required"
            )

        if (
            not isinstance(user_id, str)
            or not user_id.strip()
        ):
            raise GmailClientError(
                "user_id is required"
            )

        normalized_message_id = (
            message_id.strip()
        )
        normalized_user_id = user_id.strip()

        try:
            response = self.messages.get_message(
                message_id=normalized_message_id,
                user_id=normalized_user_id,
                message_format="full",
            )
        except MessagesClientError as exc:
            self.logger.error(
                "Gmail full message retrieval failed"
            )
            raise GmailClientError(
                "Failed to retrieve Gmail full message"
            ) from exc
        except Exception as exc:
            self.logger.error(
                "Gmail full message retrieval failed: "
                f"{type(exc).__name__}"
            )
            raise GmailClientError(
                "Failed to retrieve Gmail full message"
            ) from exc

        if not isinstance(response, dict):
            raise GmailClientError(
                "Gmail returned an invalid full message"
            )

        raw_message_id = response.get("id")
        raw_thread_id = response.get(
            "threadId"
        )

        if (
            not isinstance(raw_message_id, str)
            or not raw_message_id.strip()
            or raw_message_id.strip()
            != normalized_message_id
        ):
            raise GmailClientError(
                "Gmail returned an inconsistent message ID"
            )

        if (
            not isinstance(raw_thread_id, str)
            or not raw_thread_id.strip()
        ):
            raise GmailClientError(
                "Gmail returned an invalid thread ID"
            )

        raw_payload = response.get(
            "payload"
        )

        part_counter = [0]

        root_part = _parse_mime_part(
            raw_payload,
            depth=0,
            max_depth=self.MAX_MIME_DEPTH,
            part_counter=part_counter,
            max_parts=self.MAX_MIME_PARTS,
        )

        return GmailFullMessage(
            message_id=normalized_message_id,
            thread_id=raw_thread_id.strip(),
            root_part=root_part,
        )

    def get_attachment_data(
        self,
        message_id: str,
        attachment_id: str,
        user_id: str = "me",
    ) -> GmailAttachmentData:
        """Retrieve and validate encoded attachment data."""

        if (
            not isinstance(message_id, str)
            or not message_id.strip()
        ):
            raise GmailClientError(
                "message_id is required"
            )

        if (
            not isinstance(attachment_id, str)
            or not attachment_id.strip()
        ):
            raise GmailClientError(
                "attachment_id is required"
            )

        if (
            not isinstance(user_id, str)
            or not user_id.strip()
        ):
            raise GmailClientError(
                "user_id is required"
            )

        normalized_message_id = (
            message_id.strip()
        )
        normalized_attachment_id = (
            attachment_id.strip()
        )
        normalized_user_id = user_id.strip()

        try:
            response = self.messages.get_attachment(
                message_id=normalized_message_id,
                attachment_id=(
                    normalized_attachment_id
                ),
                user_id=normalized_user_id,
            )
        except MessagesClientError as exc:
            self.logger.error(
                "Gmail attachment retrieval failed"
            )
            raise GmailClientError(
                "Failed to retrieve Gmail attachment"
            ) from exc
        except Exception as exc:
            self.logger.error(
                "Gmail attachment retrieval failed: "
                f"{type(exc).__name__}"
            )
            raise GmailClientError(
                "Failed to retrieve Gmail attachment"
            ) from exc

        if not isinstance(response, dict):
            raise GmailClientError(
                "Gmail returned invalid attachment data"
            )

        raw_size = response.get("size")
        raw_data = response.get("data")

        if (
            isinstance(raw_size, bool)
            or not isinstance(raw_size, int)
            or raw_size < 0
        ):
            raise GmailClientError(
                "Gmail returned an invalid attachment size"
            )

        if (
            not isinstance(raw_data, str)
            or (
                raw_size > 0
                and not raw_data.strip()
            )
        ):
            raise GmailClientError(
                "Gmail returned invalid attachment content"
            )

        return GmailAttachmentData(
            size=raw_size,
            encoded_data=raw_data.strip(),
        )