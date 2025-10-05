import sys

from typing import Any, Dict, List
from googleapiclient.errors import HttpError
from googleapiclient.discovery import Resource


from app.tools.logger import AppLogger
from app.tools.gmail.gmail_authenticator import GmailAuthenticator
from app.tools.gmail.messages_client import MessagesClient as GmailMessagesClient
from app.tools.gmail.user_client import UsersClient as GmailUsersClient


class GmailClientError(Exception):
    """
    Custom exception class for GmailClient errors.
    Raised when GmailClient encounters an unexpected error.
    """
    pass


class GmailClient:
    """
    High-level wrapper for interacting with Gmail API clients (Messages, Labels, Drafts, Users, etc.).

    This class orchestrates lower-level clients such as MessagesClient and provides
    a unified interface for Gmail operations.
    """

    def __init__(self, gmail_authenticator: GmailAuthenticator) -> None:
        """
        Initialize GmailClient with an authenticator.

        Args:
            gmail_authenticator (GmailAuthenticator): Authenticator instance that provides Gmail API service.
        """
        service: Resource = gmail_authenticator
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
          
    def list_messages(self, user_id: str = "me", max_results: int = 10) -> List[Dict[str, Any]]:
        """
        List messages from the user's Gmail inbox.

        Args:
            user_id (str): The user ID. Use "me" to indicate the authenticated user.
            max_results (int): Maximum number of messages to retrieve.

        Returns:
            List[Dict[str, Any]]: A list of message metadata dictionaries.

        Raises:
            GmailClientError: If listing messages fails.
        """
        try:
            results = self.messages.list_messages(user_id=user_id, max_results=max_results)
            return results.get("messages", [])
        except HttpError as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"HttpError in '{function_name}' at line {line_number}: {e}")
            raise GmailClientError("Failed to list messages") from e
        except Exception as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"Unexpected error in '{function_name}' at line {line_number}: {e}")
            raise GmailClientError("Unexpected error while listing messages") from e

    def get_message(self, message_id: str, user_id: str = "me") -> Dict[str, Any]:
        """
        Retrieve a single Gmail message by ID.

        Args:
            message_id (str): The unique Gmail message ID.
            user_id (str): The user ID. Use "me" for the authenticated user.

        Returns:
            Dict[str, Any]: The message details.

        Raises:
            GmailClientError: If retrieving the message fails.
        """
        try:
            return self.messages.get_message(message_id=message_id, user_id=user_id)
        except Exception as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"Error in '{function_name}' at line {line_number}: {e}")
            raise GmailClientError("Failed to retrieve message") from e
