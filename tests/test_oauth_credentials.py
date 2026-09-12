from __future__ import annotations

import unittest

from unittest.mock import Mock, patch

from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials

from app.backend.bd.oauth_store import (
    OAuthCredentialStore,
    StoredOAuthCredentials,
)
from app.backend.services.oauth_credentials import (
    OAuthReauthenticationRequired,
    OAuthRefreshTemporarilyUnavailable,
    ensure_valid_credentials,
)


class OAuthCredentialRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.credentials = Mock(spec=Credentials)
        self.store = Mock(spec=OAuthCredentialStore)

        self.account = StoredOAuthCredentials(
            account_id=1,
            email="owner@example.com",
            credentials=self.credentials,
            revoked=False,
            connected_at="2026-09-12T00:00:00+00:00",
            updated_at="2026-09-12T00:00:00+00:00",
        )

    def test_returns_credentials_when_they_are_valid(self) -> None:
        self.credentials.valid = True

        result = ensure_valid_credentials(
            account=self.account,
            store=self.store,
        )

        self.assertIs(result, self.credentials)
        self.credentials.refresh.assert_not_called()
        self.store.save.assert_not_called()
        self.store.mark_revoked.assert_not_called()

    @patch("app.backend.services.oauth_credentials.Request")
    def test_refreshes_and_persists_expired_credentials(
        self,
        request_class,
    ) -> None:
        self.credentials.valid = False
        self.credentials.refresh_token = "refresh-token"

        result = ensure_valid_credentials(
            account=self.account,
            store=self.store,
        )

        self.assertIs(result, self.credentials)
        request_class.assert_called_once_with()
        self.credentials.refresh.assert_called_once_with(
            request_class.return_value
        )
        self.store.save.assert_called_once_with(
            email="owner@example.com",
            credentials=self.credentials,
        )
        self.store.mark_revoked.assert_not_called()

    def test_missing_refresh_token_requires_authentication(self) -> None:
        self.credentials.valid = False
        self.credentials.refresh_token = None

        with self.assertRaises(OAuthReauthenticationRequired):
            ensure_valid_credentials(
                account=self.account,
                store=self.store,
            )

        self.credentials.refresh.assert_not_called()
        self.store.mark_revoked.assert_called_once_with()
        self.store.save.assert_not_called()

    def test_retryable_refresh_error_keeps_authorization(self) -> None:
        self.credentials.valid = False
        self.credentials.refresh_token = "refresh-token"
        self.credentials.refresh.side_effect = RefreshError(
            "Temporary Google error",
            retryable=True,
        )

        with self.assertRaises(OAuthRefreshTemporarilyUnavailable):
            ensure_valid_credentials(
                account=self.account,
                store=self.store,
            )

        self.store.mark_revoked.assert_not_called()
        self.store.save.assert_not_called()

    def test_permanent_refresh_error_revokes_authorization(self) -> None:
        self.credentials.valid = False
        self.credentials.refresh_token = "refresh-token"
        self.credentials.refresh.side_effect = RefreshError(
            "invalid_grant",
            retryable=False,
        )

        with self.assertRaises(OAuthReauthenticationRequired):
            ensure_valid_credentials(
                account=self.account,
                store=self.store,
            )

        self.store.mark_revoked.assert_called_once_with()
        self.store.save.assert_not_called()

    def test_transport_error_keeps_authorization(self) -> None:
        self.credentials.valid = False
        self.credentials.refresh_token = "refresh-token"
        self.credentials.refresh.side_effect = TransportError(
            "Connection failed"
        )

        with self.assertRaises(OAuthRefreshTemporarilyUnavailable):
            ensure_valid_credentials(
                account=self.account,
                store=self.store,
            )

        self.store.mark_revoked.assert_not_called()
        self.store.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
