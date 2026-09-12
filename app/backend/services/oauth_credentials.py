from __future__ import annotations

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from app.backend.bd.oauth_store import (
    OAuthCredentialStore,
    StoredOAuthCredentials,
)


class OAuthReauthenticationRequired(RuntimeError):
    """Google authorization must be granted again."""


class OAuthRefreshTemporarilyUnavailable(RuntimeError):
    """Google credentials could not be refreshed temporarily."""


def ensure_valid_credentials(
    account: StoredOAuthCredentials,
    store: OAuthCredentialStore,
) -> Credentials:
    """Return valid credentials, refreshing and persisting them if needed."""

    credentials = account.credentials

    if credentials.valid:
        return credentials

    if not credentials.refresh_token:
        store.mark_revoked()
        raise OAuthReauthenticationRequired(
            "The Google authorization has no refresh token"
        )

    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        if exc.retryable:
            raise OAuthRefreshTemporarilyUnavailable(
                "Google credentials could not be refreshed temporarily"
            ) from exc

        store.mark_revoked()
        raise OAuthReauthenticationRequired(
            "The Google authorization is no longer valid"
        ) from exc
    except TransportError as exc:
        raise OAuthRefreshTemporarilyUnavailable(
            "Google could not be reached to refresh credentials"
        ) from exc

    store.save(
        email=account.email,
        credentials=credentials,
    )

    return credentials
