import sys

from typing import Any, Dict
from googleapiclient.errors import HttpError
from googleapiclient.discovery import Resource


from tools.logger import AppLogger
from tools.gmail.gmail_authenticator import GmailAuthenticator


class MessagesClientError(Exception):
    """Custom exception class for MessagesClient errors."""
    pass


class MessagesClient:
    """
    MessagesClient provides a wrapper around the Gmail API service to
    perform operations such as listing, retrieving, sending,
    and deleting messages.

    It depends on GmailAuthenticator to supply the authenticated
    Gmail API service resource.
    """

    def __init__(self, service: GmailAuthenticator) -> None:
        """
        Initialize the MessagesClient.

        Args:
            gmail_authenticator (GmailAuthenticator): Authenticator providing a Gmail API service instance.
            logger (Optional[AppLogger]): Custom logger. Defaults to AppLogger if not provided.
        """
        self.service: Resource = service
        self.logger: AppLogger = AppLogger("messages_client.log")

    def list_messages(self, user_id: str = "me", max_results: int = 10) -> Dict[str, Any]:
        """
        List messages in the user's mailbox.

        Args:
            user_id (str): The user's email address. Use "me" for the authenticated user.
            max_results (int): Maximum number of messages to retrieve.

        Returns:
            Dict[str, Any]: API response containing the list of messages.

        Raises:
            MessagesClientError: If the request fails unexpectedly.
        """
        try:
            return self.service.users().messages().list(
                userId=user_id, maxResults=max_results
            ).execute()
        except HttpError as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"HttpError in '{function_name}' at line {line_number}: {e}")
            raise
        except Exception as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"Unexpected error in '{function_name}' at line {line_number}: {e}")
            raise MessagesClientError("List messages failed") from e

    def get_message(self, message_id: str, user_id: str = "me") -> Dict[str, Any]:
        """
        Retrieve a specific message by ID.

        Args:
            message_id (str): The ID of the message to retrieve.
            user_id (str): The user's email address. Use "me" for the authenticated user.

        Returns:
            Dict[str, Any]: API response containing the message details.

        Raises:
            MessagesClientError: If the request fails.
        """
        try:
            return self.service.users().messages().get(
                userId=user_id, id=message_id
            ).execute()
        except Exception as e:
            _, _, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else "unknown"
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

            self.logger.error(
                f"Error in '{function_name}' at line {line_number}: {e}")
            raise MessagesClientError("Get message failed") from e

    def send_message(self, message: Dict[str, Any], user_id: str = "me") -> Dict[str, Any]:
        """
        Send a message on behalf of the user.

        Args:
            message (Dict[str, Any]): The message body to send.
            user_id (str): The user's email address. Use "me" for the authenticated user.

        Returns:
            Dict[str, Any]: API response containing the sent message details.

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
        """
        Permanently delete a message.

        Args:
            message_id (str): The ID of the message to delete.
            user_id (str): The user's email address. Use "me" for the authenticated user.

        Returns:
            Dict[str, Any]: API response from the delete operation.

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
