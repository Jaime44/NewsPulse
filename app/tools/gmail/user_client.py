"""Wrap Gmail API operations on the authenticated ``users`` resource.

The adapter exposes profile retrieval and push-notification controls while
converting unexpected Google client failures into ``UsersClientError`` and
recording diagnostic context in the application log.
"""

import sys

from typing import Any, Dict
from googleapiclient.discovery import Resource


from app.tools.logger import AppLogger


class UsersClientError(Exception):
    """Raised when a Gmail users-resource request fails."""

    pass


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
        try:
            return self.service.users().getProfile(userId=user_id).execute()
        except Exception as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"Unexpected error in '{function_name}' at line {line_number}: {e}")
            raise UsersClientError("Failed to get user profile") from e

    def stop_notifications(self, user_id: str = "me") -> Dict[str, Any]:
        """Stop Gmail push notifications for a user.

        Args:
            user_id: User identifier; ``"me"`` selects the authenticated user.

        Returns:
            The Gmail API stop response.

        Raises:
            UsersClientError: If stopping notifications fails.
        """
        try:
            return self.service.users().stop(userId=user_id).execute()
        except Exception as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"Unexpected error in '{function_name}' at line {line_number}: {e}")
            raise UsersClientError("Failed to stop notifications") from e

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
        try:
            return self.service.users().watch(userId=user_id, body=body).execute()
        except Exception as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"Unexpected error in '{function_name}' at line {line_number}: {e}")
            raise UsersClientError("Failed to watch inbox") from e
