import sys

from typing import Any, Dict
from googleapiclient.discovery import Resource


from tools.logger import AppLogger
from tools.gmail.gmail_authenticator import GmailAuthenticator


class UsersClientError(Exception):
    """
    Custom exception class for UsersClient errors.
    Raised when UsersClient encounters an unexpected error.
    """
    pass


class UsersClient:
    """
    Client for interacting with Gmail 'users' resource.

    Provides methods to get the user's profile, stop push notifications,
    and configure/watch for Gmail inbox changes.
    """

    def __init__(self, service: GmailAuthenticator) -> None:
        """
        Initialize UsersClient with Gmail API service and optional logger.

        Args:
            service (Resource): Authorized Gmail API service object.
            logger (AppLogger, optional): Logger instance. Defaults to a new AppLogger.
        """
        self.service: Resource = service
        self.logger: AppLogger = AppLogger("users_client.log")

    def get_profile(self, user_id: str = "me") -> Dict[str, Any]:
        """
        Get the Gmail profile of the specified user.

        Args:
            user_id (str): User identifier. Use "me" for the authenticated user.

        Returns:
            Dict[str, Any]: User profile information.

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
        """
        Stop push notifications for the user's inbox.

        Args:
            user_id (str): User identifier. Use "me" for the authenticated user.

        Returns:
            Dict[str, Any]: Response from Gmail API.

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

    def watch_inbox(self, user_id: str = "me", body: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Configure or update a watch on the user's inbox for push notifications.

        Args:
            user_id (str): User identifier. Use "me" for the authenticated user.
            body (Dict[str, Any]): Request body for watch configuration (e.g., topic name, label filters).

        Returns:
            Dict[str, Any]: Response from Gmail API.

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
