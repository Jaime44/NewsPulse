"""Validate and execute low-level Gmail message and attachment requests.

``MessagesClient`` builds request parameters for the Gmail discovery resource,
applies input bounds, logs request-failure diagnostics, and exposes raw
dictionary responses to the higher-level typed :mod:`gmail_client` adapter.
"""

import sys
from collections.abc import Sequence

from typing import Any, Dict
from googleapiclient.errors import HttpError
from googleapiclient.discovery import Resource


from app.tools.logger import AppLogger


class MessagesClientError(Exception):
    """Raised when a Gmail message or attachment request fails."""

    pass


class MessagesClient:
    """Wrap Gmail message listing, retrieval, sending, and deletion.

    Attributes:
        service: Authorized Gmail API discovery resource.
        logger: File logger for request diagnostics.
    """

    VALID_MESSAGE_FORMATS = frozenset(
        {
            "minimal",
            "full",
            "raw",
            "metadata",
        }
    )

    def __init__(self, service: Resource) -> None:
        """Initialize the messages-resource adapter.

        Args:
            service: Authorized Gmail API discovery resource.
        """
        self.service: Resource = service
        self.logger: AppLogger = AppLogger("messages_client.log")

    def list_messages(
        self,
        user_id: str = "me",
        max_results: int = 100,
        page_token: str | None = None,
        query: str | None = None,
        label_ids: Sequence[str] | None = None,
        include_spam_trash: bool = False,
    ) -> Dict[str, Any]:
        """Return one validated page of Gmail message references.

        Args:
            user_id: Gmail user identifier.
            max_results: Page size between 1 and 500.
            page_token: Optional token selecting a subsequent page.
            query: Optional Gmail search query.
            label_ids: Optional labels that every result must match.
            include_spam_trash: Whether spam and trash may be returned.

        Returns:
            The raw Gmail ``messages.list`` response.

        Raises:
            MessagesClientError: If inputs are invalid or the request fails.
        """

        normalized_user_id = user_id.strip()

        if not normalized_user_id:
            raise MessagesClientError(
                "user_id is required"
            )

        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= 500
        ):
            raise MessagesClientError(
                "max_results must be between 1 and 500"
            )

        if not isinstance(include_spam_trash, bool):
            raise MessagesClientError(
                "include_spam_trash must be boolean"
            )

        request_parameters: Dict[str, Any] = {
            "userId": normalized_user_id,
            "maxResults": max_results,
            "includeSpamTrash": include_spam_trash,
        }

        if page_token is not None:
            normalized_page_token = page_token.strip()

            if not normalized_page_token:
                raise MessagesClientError(
                    "page_token cannot be empty"
                )

            request_parameters["pageToken"] = (
                normalized_page_token
            )

        if query is not None:
            normalized_query = query.strip()

            if not normalized_query:
                raise MessagesClientError(
                    "query cannot be empty"
                )

            request_parameters["q"] = normalized_query

        if label_ids is not None:
            normalized_labels = [
                label_id.strip()
                for label_id in label_ids
            ]

            if (
                not normalized_labels
                or any(
                    not label_id
                    for label_id in normalized_labels
                )
            ):
                raise MessagesClientError(
                    "label_ids cannot contain empty values"
                )

            request_parameters["labelIds"] = (
                normalized_labels
            )

        try:
            return (
                self.service
                .users()
                .messages()
                .list(**request_parameters)
                .execute()
            )
        except HttpError as exc:
            status_code = getattr(
                exc.resp,
                "status",
                "unknown",
            )
            self.logger.error(
                "Gmail messages.list failed with "
                f"HTTP status {status_code}"
            )
            raise MessagesClientError(
                "Failed to list Gmail messages"
            ) from exc
        except Exception as exc:
            self.logger.error(
                "Gmail messages.list failed: "
                f"{type(exc).__name__}"
            )
            raise MessagesClientError(
                "Failed to list Gmail messages"
            ) from exc

    def get_message(
        self,
        message_id: str,
        user_id: str = "me",
        message_format: str = "metadata",
        metadata_headers: Sequence[str] | None = None,
    ) -> Dict[str, Any]:
        """Retrieve one Gmail message in a validated representation.

        Args:
            message_id: Gmail message identifier.
            user_id: Gmail user identifier.
            message_format: One of Gmail's supported message formats.
            metadata_headers: Optional headers requested with metadata format.

        Returns:
            The raw Gmail ``messages.get`` response.

        Raises:
            MessagesClientError: If inputs are invalid or the request fails.
        """

        if (
            not isinstance(message_id, str)
            or not message_id.strip()
        ):
            raise MessagesClientError(
                "message_id is required"
            )

        if (
            not isinstance(user_id, str)
            or not user_id.strip()
        ):
            raise MessagesClientError(
                "user_id is required"
            )

        if not isinstance(message_format, str):
            raise MessagesClientError(
                "message_format is invalid"
            )

        normalized_format = (
            message_format.strip().lower()
        )

        if (
            normalized_format
            not in self.VALID_MESSAGE_FORMATS
        ):
            raise MessagesClientError(
                "message_format is invalid"
            )

        request_parameters: Dict[str, Any] = {
            "userId": user_id.strip(),
            "id": message_id.strip(),
            "format": normalized_format,
        }

        if metadata_headers is not None:
            if normalized_format != "metadata":
                raise MessagesClientError(
                    "metadata_headers requires metadata format"
                )

            if isinstance(
                metadata_headers,
                (str, bytes),
            ):
                raise MessagesClientError(
                    "metadata_headers must be a sequence"
                )

            normalized_headers = [
                header.strip()
                for header in metadata_headers
            ]

            if (
                not normalized_headers
                or any(
                    not header
                    for header in normalized_headers
                )
            ):
                raise MessagesClientError(
                    "metadata_headers cannot contain "
                    "empty values"
                )

            request_parameters["metadataHeaders"] = (
                normalized_headers
            )

        try:
            return (
                self.service
                .users()
                .messages()
                .get(**request_parameters)
                .execute()
            )
        except HttpError as exc:
            status_code = getattr(
                exc.resp,
                "status",
                "unknown",
            )
            self.logger.error(
                "Gmail messages.get failed with "
                f"HTTP status {status_code}"
            )
            raise MessagesClientError(
                "Failed to retrieve Gmail message"
            ) from exc
        except Exception as exc:
            self.logger.error(
                "Gmail messages.get failed: "
                f"{type(exc).__name__}"
            )
            raise MessagesClientError(
                "Failed to retrieve Gmail message"
            ) from exc

    def get_attachment(
        self,
        message_id: str,
        attachment_id: str,
        user_id: str = "me",
    ) -> Dict[str, Any]:
        """Retrieve one Gmail message attachment.

        Args:
            message_id: Gmail message identifier.
            attachment_id: Gmail attachment identifier.
            user_id: Gmail user identifier.

        Returns:
            The raw Gmail attachment response.

        Raises:
            MessagesClientError: If identifiers are invalid or the request fails.
        """

        if (
            not isinstance(message_id, str)
            or not message_id.strip()
        ):
            raise MessagesClientError(
                "message_id is required"
            )

        if (
            not isinstance(attachment_id, str)
            or not attachment_id.strip()
        ):
            raise MessagesClientError(
                "attachment_id is required"
            )

        if (
            not isinstance(user_id, str)
            or not user_id.strip()
        ):
            raise MessagesClientError(
                "user_id is required"
            )

        try:
            return (
                self.service
                .users()
                .messages()
                .attachments()
                .get(
                    userId=user_id.strip(),
                    messageId=message_id.strip(),
                    id=attachment_id.strip(),
                )
                .execute()
            )
        except HttpError as exc:
            status_code = getattr(
                exc.resp,
                "status",
                "unknown",
            )
            self.logger.error(
                "Gmail attachments.get failed with "
                f"HTTP status {status_code}"
            )
            raise MessagesClientError(
                "Failed to retrieve Gmail attachment"
            ) from exc
        except Exception as exc:
            self.logger.error(
                "Gmail attachments.get failed: "
                f"{type(exc).__name__}"
            )
            raise MessagesClientError(
                "Failed to retrieve Gmail attachment"
            ) from exc

    def send_message(self, message: Dict[str, Any], user_id: str = "me") -> Dict[str, Any]:
        """Send a message on behalf of a Gmail user.

        Args:
            message: Gmail API message body to send.
            user_id: User identifier; ``"me"`` selects the authenticated user.

        Returns:
            The Gmail API response containing sent-message details.

        Raises:
            MessagesClientError: If the request fails.
        """
        try:
            return self.service.users().messages().send(
                userId=user_id, body=message
            ).execute()
        except Exception as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"Error in '{function_name}' at line {line_number}: {e}")
            raise MessagesClientError("Send message failed") from e

    def delete_message(self, message_id: str, user_id: str = "me") -> Dict[str, Any]:
        """Permanently delete one Gmail message.

        Args:
            message_id: Identifier of the message to delete.
            user_id: User identifier; ``"me"`` selects the authenticated user.

        Returns:
            The Gmail API delete response.

        Raises:
            MessagesClientError: If the request fails.
        """
        try:
            return self.service.users().messages().delete(
                userId=user_id, id=message_id
            ).execute()
        except Exception as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"Error in '{function_name}' at line {line_number}: {e}")
            raise MessagesClientError("Delete message failed") from e
