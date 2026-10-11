"""Validate, refresh, persist, and revoke stored Google OAuth credentials.

The service distinguishes temporary Google transport failures from permanent
authorization failures so Flask routes can preserve or clear their session as
appropriate.
"""

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
    """Return valid credentials, refreshing and persisting them if needed.

    Args:
        account: Stored account and its decrypted Google credentials.
        store: Repository used to persist refreshes or mark revocation.

    Returns:
        Existing valid credentials or successfully refreshed credentials.

    Raises:
        OAuthReauthenticationRequired: If no refresh token exists or Google
            permanently rejects the authorization.
        OAuthRefreshTemporarilyUnavailable: If a retryable or transport error
            prevents refresh without proving authorization invalid.
    """

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
