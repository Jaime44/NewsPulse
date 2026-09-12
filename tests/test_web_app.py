from __future__ import annotations

import importlib
import sys
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from app.config import AppConfig
from app.backend.services.oauth_credentials import (
    OAuthReauthenticationRequired,
    OAuthRefreshTemporarilyUnavailable,
)


class GmailRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = TemporaryDirectory()
        directory = Path(cls.temporary_directory.name)

        google_credentials_path = directory / "google.json"
        flask_secret_key_path = directory / "flask.key"
        token_encryption_key_path = directory / "token.key"

        google_credentials_path.write_text(
            "{}",
            encoding="utf-8",
        )
        flask_secret_key_path.write_text(
            "t" * 64,
            encoding="utf-8",
        )
        token_encryption_key_path.write_bytes(
            Fernet.generate_key()
        )

        settings = AppConfig(
            environment="testing",
            host="localhost",
            port=5000,
            timezone="Europe/Madrid",
            debug=False,
            google_credentials_path=google_credentials_path,
            google_redirect_uri=(
                "http://localhost:5000/auth/google/callback"
            ),
            google_scopes=(
                "https://www.googleapis.com/auth/gmail.readonly",
            ),
            flask_secret_key_path=flask_secret_key_path,
            token_encryption_key_path=token_encryption_key_path,
            database_path=directory / "test.db",
            retention_days=30,
        )

        sys.modules.pop("app.backend.web_app", None)

        with (
            patch.object(
                AppConfig,
                "load",
                return_value=settings,
            ),
            patch("app.tools.logger.AppLogger"),
        ):
            cls.web_app = importlib.import_module(
                "app.backend.web_app"
            )

        cls.web_app.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop("app.backend.web_app", None)
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.client = self.web_app.app.test_client()

    def authenticate_session(self) -> None:
        with self.client.session_transaction() as current_session:
            current_session["account_id"] = 1

    def test_gmail_routes_reject_unauthenticated_requests(self) -> None:
        endpoints = (
            "/api/gmail/profile",
            "/api/gmail/messages",
        )

        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                with patch.object(
                    self.web_app,
                    "get_authenticated_gmail_service",
                    return_value=None,
                ):
                    response = self.client.get(endpoint)

                self.assertEqual(response.status_code, 401)
                self.assertEqual(
                    response.get_json(),
                    {"error": "Not authenticated"},
                )

    def test_reauthentication_error_clears_session(self) -> None:
        endpoints = (
            "/api/gmail/profile",
            "/api/gmail/messages",
        )

        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                self.authenticate_session()

                with patch.object(
                    self.web_app,
                    "get_authenticated_gmail_service",
                    side_effect=OAuthReauthenticationRequired(
                        "Authentication required"
                    ),
                ):
                    response = self.client.get(endpoint)

                self.assertEqual(response.status_code, 401)

                with self.client.session_transaction() as current_session:
                    self.assertNotIn(
                        "account_id",
                        current_session,
                    )

    def test_temporary_refresh_error_preserves_session(self) -> None:
        endpoints = (
            "/api/gmail/profile",
            "/api/gmail/messages",
        )

        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                self.authenticate_session()

                with patch.object(
                    self.web_app,
                    "get_authenticated_gmail_service",
                    side_effect=OAuthRefreshTemporarilyUnavailable(
                        "Temporary error"
                    ),
                ):
                    response = self.client.get(endpoint)

                self.assertEqual(response.status_code, 503)

                with self.client.session_transaction() as current_session:
                    self.assertEqual(
                        current_session.get("account_id"),
                        1,
                    )

    def test_profile_route_returns_gmail_profile(self) -> None:
        gmail_service = Mock()
        gmail_client = Mock()
        gmail_client.get_profile.return_value = {
            "emailAddress": "owner@example.com",
        }

        with (
            patch.object(
                self.web_app,
                "get_authenticated_gmail_service",
                return_value=gmail_service,
            ),
            patch.object(
                self.web_app,
                "GmailClient",
                return_value=gmail_client,
            ),
        ):
            response = self.client.get("/api/gmail/profile")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"emailAddress": "owner@example.com"},
        )

    def test_messages_route_returns_gmail_messages(self) -> None:
        gmail_service = Mock()

        execute = (
            gmail_service
            .users
            .return_value
            .messages
            .return_value
            .list
            .return_value
            .execute
        )
        execute.return_value = {
            "messages": [
                {"id": "message-1"},
                {"id": "message-2"},
            ]
        }

        with patch.object(
            self.web_app,
            "get_authenticated_gmail_service",
            return_value=gmail_service,
        ):
            response = self.client.get("/api/gmail/messages")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "messages": [
                    {"id": "message-1"},
                    {"id": "message-2"},
                ]
            },
        )

        (
            gmail_service
            .users
            .return_value
            .messages
            .return_value
            .list
            .assert_called_once_with(
                userId="me",
                maxResults=10,
            )
        )


if __name__ == "__main__":
    unittest.main()
