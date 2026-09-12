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


    def test_google_auth_stores_state_in_signed_session(self) -> None:
        flow = Mock()
        flow.authorization_url.return_value = (
            "https://accounts.google.com/o/oauth2/auth",
            "generated-state",
        )

        with patch.object(
            self.web_app.Flow,
            "from_client_secrets_file",
            return_value=flow,
        ):
            response = self.client.get("/auth/google")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.location,
            "https://accounts.google.com/o/oauth2/auth",
        )

        with self.client.session_transaction() as current_session:
            self.assertEqual(
                current_session.get("oauth_state"),
                "generated-state",
            )
            self.assertEqual(
                set(current_session.keys()),
                {"oauth_state"},
            )

        cookie_header = response.headers.get("Set-Cookie", "")

        self.assertIn(
            "newspulse_session=",
            cookie_header,
        )
        self.assertIn(
            "HttpOnly",
            cookie_header,
        )
        self.assertIn(
            "SameSite=Lax",
            cookie_header,
        )

        flow.authorization_url.assert_called_once_with(
            access_type="offline",
            prompt="consent",
        )

    def test_callback_without_code_is_rejected(self) -> None:
        with self.client.session_transaction() as current_session:
            current_session["oauth_state"] = "expected-state"

        with patch.object(
            self.web_app.Flow,
            "from_client_secrets_file",
        ) as flow_factory:
            response = self.client.get(
                "/auth/google/callback?state=expected-state"
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "Authorization code not received"},
        )
        flow_factory.assert_not_called()

        with self.client.session_transaction() as current_session:
            self.assertNotIn(
                "oauth_state",
                current_session,
            )

    def test_callback_rejects_invalid_state_conditions(self) -> None:
        cases = (
            (
                "missing request state",
                "/auth/google/callback?code=authorization-code",
                True,
            ),
            (
                "missing session state",
                (
                    "/auth/google/callback"
                    "?code=authorization-code"
                    "&state=expected-state"
                ),
                False,
            ),
            (
                "mismatched state",
                (
                    "/auth/google/callback"
                    "?code=authorization-code"
                    "&state=wrong-state"
                ),
                True,
            ),
        )

        for description, callback_url, store_state in cases:
            with self.subTest(case=description):
                with self.client.session_transaction() as current_session:
                    current_session.clear()

                    if store_state:
                        current_session["oauth_state"] = (
                            "expected-state"
                        )

                with patch.object(
                    self.web_app.Flow,
                    "from_client_secrets_file",
                ) as flow_factory:
                    response = self.client.get(callback_url)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.get_json(),
                    {"error": "Invalid state parameter"},
                )
                flow_factory.assert_not_called()

                with self.client.session_transaction() as current_session:
                    self.assertNotIn(
                        "oauth_state",
                        current_session,
                    )

    def test_successful_callback_replaces_previous_session(self) -> None:
        flow = Mock()
        credentials = Mock()
        flow.credentials = credentials

        gmail_service = Mock()
        gmail_client = Mock()
        gmail_client.get_profile.return_value = {
            "emailAddress": "Owner@Example.com",
        }

        with self.client.session_transaction() as current_session:
            current_session["oauth_state"] = "expected-state"
            current_session["legacy_value"] = "remove-me"
            current_session["account_id"] = 999

        with (
            patch.object(
                self.web_app.Flow,
                "from_client_secrets_file",
                return_value=flow,
            ),
            patch.object(
                self.web_app,
                "build",
                return_value=gmail_service,
            ),
            patch.object(
                self.web_app,
                "GmailClient",
                return_value=gmail_client,
            ),
            patch.object(
                self.web_app.oauth_store,
                "save",
            ) as save_credentials,
        ):
            response = self.client.get(
                "/auth/google/callback"
                "?code=authorization-code"
                "&state=expected-state"
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.location.endswith("/dashboard")
        )

        flow.fetch_token.assert_called_once_with(
            code="authorization-code"
        )
        save_credentials.assert_called_once_with(
            email="Owner@Example.com",
            credentials=credentials,
        )

        with self.client.session_transaction() as current_session:
            self.assertEqual(
                dict(current_session),
                {
                    "account_id": (
                        self.web_app
                        .OAuthCredentialStore
                        .ACCOUNT_ID
                    )
                },
            )

    def test_callback_handles_google_authorization_errors(self) -> None:
        cases = (
            (
                "access_denied",
                "google_authorization_denied",
                "warning",
            ),
            (
                "temporarily_unavailable",
                "google_authorization_failed",
                "error",
            ),
        )

        for google_error, expected_error, log_method in cases:
            with self.subTest(google_error=google_error):
                with self.client.session_transaction() as current_session:
                    current_session.clear()
                    current_session["oauth_state"] = "expected-state"

                with (
                    patch.object(
                        self.web_app.Flow,
                        "from_client_secrets_file",
                    ) as flow_factory,
                    patch.object(
                        self.web_app.logger,
                        log_method,
                    ) as log_call,
                ):
                    response = self.client.get(
                        "/auth/google/callback"
                        f"?error={google_error}"
                        "&error_description=sensitive-detail"
                        "&state=expected-state"
                    )

                self.assertEqual(response.status_code, 302)
                self.assertIn(
                    f"error={expected_error}",
                    response.location,
                )
                flow_factory.assert_not_called()
                log_call.assert_called_once()

                logged_message = log_call.call_args.args[0]

                self.assertNotIn(
                    "sensitive-detail",
                    logged_message,
                )

                with self.client.session_transaction() as current_session:
                    self.assertNotIn(
                        "oauth_state",
                        current_session,
                    )

    def test_google_auth_failure_does_not_log_exception_details(
        self,
    ) -> None:
        with (
            patch.object(
                self.web_app.Flow,
                "from_client_secrets_file",
                side_effect=RuntimeError(
                    "sensitive-auth-detail"
                ),
            ),
            patch.object(
                self.web_app.logger,
                "error",
            ) as error_log,
        ):
            response = self.client.get("/auth/google")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json(),
            {"error": "Authentication failed"},
        )

        error_log.assert_called_once()
        logged_message = error_log.call_args.args[0]

        self.assertIn(
            "RuntimeError",
            logged_message,
        )
        self.assertNotIn(
            "sensitive-auth-detail",
            logged_message,
        )

    def test_callback_failure_does_not_log_exception_details(
        self,
    ) -> None:
        with self.client.session_transaction() as current_session:
            current_session["oauth_state"] = "expected-state"

        with (
            patch.object(
                self.web_app.Flow,
                "from_client_secrets_file",
                side_effect=RuntimeError(
                    "sensitive-callback-detail"
                ),
            ),
            patch.object(
                self.web_app.logger,
                "error",
            ) as error_log,
        ):
            response = self.client.get(
                "/auth/google/callback"
                "?code=authorization-code"
                "&state=expected-state"
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json(),
            {"error": "Authentication callback failed"},
        )

        error_log.assert_called_once()
        logged_message = error_log.call_args.args[0]

        self.assertIn(
            "RuntimeError",
            logged_message,
        )
        self.assertNotIn(
            "sensitive-callback-detail",
            logged_message,
        )

    def test_logout_requires_post_and_clears_session(self) -> None:
        self.authenticate_session()

        get_response = self.client.get("/logout")

        self.assertEqual(
            get_response.status_code,
            405,
        )

        with self.client.session_transaction() as current_session:
            self.assertEqual(
                current_session.get("account_id"),
                1,
            )

        post_response = self.client.post("/logout")

        self.assertEqual(
            post_response.status_code,
            302,
        )
        self.assertTrue(
            post_response.location.endswith("/")
        )

        with self.client.session_transaction() as current_session:
            self.assertNotIn(
                "account_id",
                current_session,
            )

    def test_dashboard_uses_post_form_for_logout(self) -> None:
        stored_account = Mock(
            email="owner@example.com",
        )

        with patch.object(
            self.web_app,
            "get_authenticated_account",
            return_value=stored_account,
        ):
            response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)

        html = response.get_data(as_text=True)
        self.assertIn(
            "<title>News Pulse - Dashboard</title>",
            html,
        )
        self.assertIn(
            '<div class="logo">📧 News Pulse</div>',
            html,
        )
        self.assertNotIn(
            "Smart Newsletters",
            html,
        )
        self.assertEqual(
            html.count('action="/logout"'),
            1,
        )
        self.assertIn(
            'action="/logout"',
            html,
        )
        self.assertIn(
            'method="post"',
            html,
        )
        self.assertNotIn(
            'href="/logout"',
            html,
        )

    def test_dashboard_without_account_id_does_not_load_store(
        self,
    ) -> None:
        with patch.object(
            self.web_app.oauth_store,
            "load",
        ) as load_account:
            response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.location.endswith("/")
        )
        load_account.assert_not_called()

    def test_invalid_account_id_is_rejected_and_cleared(
        self,
    ) -> None:
        with self.client.session_transaction() as current_session:
            current_session["account_id"] = 999
            current_session["legacy_value"] = "remove-me"

        with patch.object(
            self.web_app.oauth_store,
            "load",
        ) as load_account:
            response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.location.endswith("/")
        )
        load_account.assert_not_called()

        with self.client.session_transaction() as current_session:
            self.assertEqual(
                dict(current_session),
                {},
            )

    def test_missing_or_revoked_account_clears_session(
        self,
    ) -> None:
        cases = (
            (
                "missing account",
                None,
            ),
            (
                "revoked account",
                Mock(
                    account_id=1,
                    email="owner@example.com",
                    revoked=True,
                ),
            ),
        )

        for description, stored_account in cases:
            with self.subTest(case=description):
                with self.client.session_transaction() as current_session:
                    current_session.clear()
                    current_session["account_id"] = 1
                    current_session["legacy_value"] = "remove-me"

                with patch.object(
                    self.web_app.oauth_store,
                    "load",
                    return_value=stored_account,
                ):
                    response = self.client.get("/dashboard")

                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    response.location.endswith("/")
                )

                with self.client.session_transaction() as current_session:
                    self.assertEqual(
                        dict(current_session),
                        {},
                    )

    def test_valid_account_is_loaded_from_server_storage(
        self,
    ) -> None:
        stored_account = Mock(
            account_id=1,
            email="owner@example.com",
            revoked=False,
        )

        self.authenticate_session()

        with patch.object(
            self.web_app.oauth_store,
            "load",
            return_value=stored_account,
        ) as load_account:
            response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "owner@example.com",
            response.get_data(as_text=True),
        )
        load_account.assert_called_once_with()

        with self.client.session_transaction() as current_session:
            self.assertEqual(
                current_session.get("account_id"),
                1,
            )

if __name__ == "__main__":
    unittest.main()
