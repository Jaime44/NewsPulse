from __future__ import annotations

import json

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from google.oauth2.credentials import Credentials

from app.backend.bd.db import Database


class OAuthCredentialStoreError(RuntimeError):
    """Raised when OAuth credentials cannot be stored or recovered."""


@dataclass(frozen=True, slots=True)
class StoredOAuthCredentials:
    account_id: int
    email: str
    credentials: Credentials
    revoked: bool
    connected_at: str
    updated_at: str


class OAuthCredentialStore:
    """Persist encrypted OAuth credentials for the single NewsPulse account."""

    ACCOUNT_ID = 1

    def __init__(
        self,
        database: Database,
        encryption_key_path: Path,
    ) -> None:
        self.database = database

        try:
            encryption_key = (
                encryption_key_path
                .read_bytes()
                .strip()
            )
            self.cipher = Fernet(encryption_key)
        except OSError as exc:
            raise OAuthCredentialStoreError(
                "The OAuth encryption key could not be read"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise OAuthCredentialStoreError(
                "The OAuth encryption key is not a valid Fernet key"
            ) from exc

        self.database.initialize()

    def save(
        self,
        email: str,
        credentials: Credentials,
    ) -> None:
        """Encrypt and persist Google OAuth credentials."""

        normalized_email = email.strip().lower()

        if not normalized_email:
            raise OAuthCredentialStoreError(
                "An email address is required"
            )

        if not credentials.refresh_token:
            raise OAuthCredentialStoreError(
                "Google did not provide a refresh token"
            )

        credentials_json = credentials.to_json().encode("utf-8")
        credentials_encrypted = self.cipher.encrypt(
            credentials_json
        )

        now = datetime.now(timezone.utc).isoformat()

        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO oauth_credentials (
                    id,
                    email,
                    credentials_encrypted,
                    revoked,
                    connected_at,
                    updated_at
                )
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    email = excluded.email,
                    credentials_encrypted =
                        excluded.credentials_encrypted,
                    revoked = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    self.ACCOUNT_ID,
                    normalized_email,
                    credentials_encrypted,
                    now,
                    now,
                ),
            )

    def load(self) -> StoredOAuthCredentials | None:
        """Load and decrypt the configured Google account."""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    email,
                    credentials_encrypted,
                    revoked,
                    connected_at,
                    updated_at
                FROM oauth_credentials
                WHERE id = ?
                """,
                (self.ACCOUNT_ID,),
            ).fetchone()

        if row is None:
            return None

        try:
            encrypted_value = bytes(
                row["credentials_encrypted"]
            )
            decrypted_value = self.cipher.decrypt(
                encrypted_value
            )
            credentials_data = json.loads(
                decrypted_value.decode("utf-8")
            )
            credentials = Credentials.from_authorized_user_info(
                credentials_data,
                scopes=credentials_data.get("scopes"),
            )
        except InvalidToken as exc:
            raise OAuthCredentialStoreError(
                "Stored OAuth credentials cannot be decrypted"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise OAuthCredentialStoreError(
                "Stored OAuth credentials are invalid"
            ) from exc

        return StoredOAuthCredentials(
            account_id=row["id"],
            email=row["email"],
            credentials=credentials,
            revoked=bool(row["revoked"]),
            connected_at=row["connected_at"],
            updated_at=row["updated_at"],
        )

    def mark_revoked(self) -> None:
        """Mark the stored Google authorization as revoked."""

        now = datetime.now(timezone.utc).isoformat()

        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE oauth_credentials
                SET revoked = 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    now,
                    self.ACCOUNT_ID,
                ),
            )

    def delete(self) -> None:
        """Delete the local OAuth authorization."""

        with self.database.connect() as connection:
            connection.execute(
                """
                DELETE FROM oauth_credentials
                WHERE id = ?
                """,
                (self.ACCOUNT_ID,),
            )
