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


class GmailClient:
    """
    High-level wrapper for interacting with Gmail API clients (Messages, Labels, Drafts, Users, etc.).

    This class orchestrates lower-level clients such as MessagesClient and provides
    a unified interface for Gmail operations.
    """

    MAX_TOTAL_RESULTS = 10_000

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

                if len(references) >= total_limit:
                    break

                next_page_token = response.get(
                    "nextPageToken"
                )

                if next_page_token is None:
                    break

                if (
                    not isinstance(next_page_token, str)
                    or not next_page_token.strip()
                ):
                    raise GmailClientError(
                        "Gmail returned an invalid page token"
                    )

                normalized_page_token = (
                    next_page_token.strip()
                )

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
