import os
import sys
import traceback

from typing import Optional, List
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import Resource, build
from google_auth_oauthlib.flow import InstalledAppFlow


from tools.logger import AppLogger


class AuthenticationError(Exception):
    pass

class GmailAuthenticator:
    """
    Handles Gmail API authentication for multiple users.
    Each user has their own OAuth token.
    """

    def __init__(self, credentials_path: str, logger: Optional[AppLogger] = None):
        """
        Initialize the GmailAuthenticator.

        Args:
            credentials_path (str): Path to the OAuth client credentials JSON.
            logger (Optional[AppLogger]): Optional custom logger. Defaults to AppLogger.
        """
        self.credentials_path: str = credentials_path
        self.logger: AppLogger = logger or AppLogger("gmail_authenticator.log")
        self.creds: Optional[Credentials] = None

    def authenticate_user(self, user_email: str, scopes: List[str], token_storage_path: str) -> Credentials:
        """
        Authenticate a single user and return valid credentials.

        Args:
            user_email (str): Email of the user authorizing the app.
            scopes (list[str]): List of OAuth scopes.
            token_storage_path (str): Path to store this user's token JSON.

        Returns:
            Credentials: OAuth credentials for this user.
        """
        creds = None
        try:
            if os.path.exists(token_storage_path):
                creds = Credentials.from_authorized_user_file(token_storage_path, scopes)
                self.logger.info(f"Loaded existing token for {user_email}")

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    self.logger.info(f"Token refreshed for {user_email}")
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        self.credentials_path, scopes
                    )
                    flow.authorization_url(login_hint=user_email)
                    creds = flow.run_local_server(port=0)
                    self.logger.info(f"New token obtained for {user_email}")

                # Save credentials for this user
                with open(token_storage_path, "w") as token_file:
                    token_file.write(creds.to_json())

        except Exception as e:
            _,_, exec_tb = sys.exc_info()
            line_number = exec_tb.tb_lineno if exec_tb else 'unknown'
            function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else 'unknown'
            self.logger.error(
                f"Authentication failed in '{function_name}' at line {line_number}: {e}"
            )
            raise AuthenticationError(f"Gmail authentication failed for {user_email}") from e

        return creds


    def get_service_for_user(self, creds: Credentials, api_name: str = "gmail", api_version: str = "v1") -> Resource:
            """
            Create and return an authorized Google API service object for a specific user.

            This function uses the provided OAuth credentials to build a Gmail service
            object, which can then be used to make API calls on behalf of the user.

            Args:
                creds (Credentials): Authorized credentials object for the user.
                api_name (str, optional): Name of the Google API to access. Defaults to "gmail".
                api_version (str, optional): Version of the API to use. Defaults to "v1".

            Returns:
                Resource: An instance of googleapiclient.discovery.Resource representing
                        the authorized API service.

            Raises:
                AuthenticationError: If the service could not be created due to invalid
                                    credentials or other unexpected errors.
            """
            try:
                service: Resource = build(api_name, api_version, credentials=creds)
                self.logger.info(f"Service created successfully for {api_name} {api_version}")
                return service
            except Exception as e:
                _, _, exec_tb = sys.exc_info()
                line_number: int = exec_tb.tb_lineno if exec_tb else -1
                function_name: str = exec_tb.tb_frame.f_code.co_name if exec_tb else "unknown"

                self.logger.error(
                    f"Failed to create service in function '{function_name}' at line {line_number}: {e}",
                    exc_info=True
                )
                raise AuthenticationError(
                    f"Failed to create Gmail service for API {api_name} {api_version}"
                ) from e
