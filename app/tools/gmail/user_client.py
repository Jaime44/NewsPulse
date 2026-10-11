"""Wrap Gmail API operations on the authenticated ``users`` resource.

The adapter exposes profile retrieval and push-notification controls while
converting unexpected Google client failures into ``UsersClientError`` and
recording diagnostic context in the application log.
"""

from time import perf_counter
from typing import Any, Dict
from googleapiclient.discovery import Resource


from app.tools.logger import (
    AppLogger,
    mark_error_logged,
)


class UsersClientError(Exception):
    """Raised when a Gmail users-resource request fails."""


class UsersClient:
    """Call Gmail operations scoped to the authenticated user.

    Attributes:
        service: Authorized Gmail API discovery resource.
        logger: File logger for request diagnostics.
    """

    def __init__(self, service: Resource) -> None:
        """Initialize the users-resource adapter.

        Args:
            service: Authorized Gmail API discovery resource.
        """
        self.service: Resource = service
        self.logger: AppLogger = AppLogger("users_client.log")

    def get_profile(self, user_id: str = "me") -> Dict[str, Any]:
        """Retrieve the Gmail profile for a user identifier.

        Args:
            user_id: User identifier; ``"me"`` selects the authenticated user.

        Returns:
            The Gmail API profile response.

        Raises:
            UsersClientError: If the request fails.
        """
        operation_started_at = perf_counter()
        self.logger.debug(
            "Gmail users.getProfile request started"
        )

        try:
            response = (
                self.service
                .users()
                .getProfile(userId=user_id)
                .execute()
            )
            self.logger.debug(
                "Gmail users.getProfile request completed: duration_ms=%.2f",
                (perf_counter() - operation_started_at) * 1_000,
            )
            return response
        except Exception as exc:
            self.logger.error(
                "Gmail users.getProfile failed: %s",
                type(exc).__name__,
            )
            raise mark_error_logged(
                UsersClientError(
                    "Failed to get user profile"
                )
            ) from exc

    def stop_notifications(self, user_id: str = "me") -> Dict[str, Any]:
        """Stop Gmail push notifications for a user.

        Args:
            user_id: User identifier; ``"me"`` selects the authenticated user.

        Returns:
            The Gmail API stop response.

        Raises:
            UsersClientError: If stopping notifications fails.
        """
        operation_started_at = perf_counter()
        self.logger.debug(
            "Gmail users.stop request started"
        )

        try:
            response = (
                self.service
                .users()
                .stop(userId=user_id)
                .execute()
            )
            self.logger.info(
                "Gmail users.stop request completed: duration_ms=%.2f",
                (perf_counter() - operation_started_at) * 1_000,
            )
            return response
        except Exception as exc:
            self.logger.error(
                "Gmail users.stop failed: %s",
                type(exc).__name__,
            )
            raise mark_error_logged(
                UsersClientError(
                    "Failed to stop notifications"
                )
            ) from exc

    def watch_inbox(
        self,
        user_id: str = "me",
        body: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Configure or update Gmail push notifications for a user.

        Args:
            user_id: User identifier; ``"me"`` selects the authenticated user.
            body: Watch configuration, such as topic and label filters. An
                omitted value sends an empty request body.

        Returns:
            The Gmail API watch response.

        Raises:
            UsersClientError: If setting up the watch fails.
        """
        body = body or {}
        operation_started_at = perf_counter()
        self.logger.debug(
            "Gmail users.watch request started"
        )

        try:
            response = (
                self.service
                .users()
                .watch(
                    userId=user_id,
                    body=body,
                )
                .execute()
            )
            self.logger.info(
                "Gmail users.watch request completed: duration_ms=%.2f",
                (perf_counter() - operation_started_at) * 1_000,
            )
            return response
        except Exception as exc:
            self.logger.error(
                "Gmail users.watch failed: %s",
                type(exc).__name__,
            )
            raise mark_error_logged(
                UsersClientError(
                    "Failed to watch inbox"
                )
            ) from exc
