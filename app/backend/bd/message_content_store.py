from __future__ import annotations

import json
import sqlite3

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import (
    datetime,
    timedelta,
    timezone,
)

from app.backend.bd.db import Database
from app.backend.bd.oauth_store import (
    OAuthCredentialStore,
)


class MessageContentStoreError(RuntimeError):
    """Raised when extracted message content cannot be persisted."""


@dataclass(frozen=True, slots=True)
class StoredMessageContent:
    """Readable content persisted for one Gmail message."""

    gmail_message_row_id: int
    gmail_message_id: str
    text: str
    links: tuple[str, ...]
    source_mime_type: str
    extracted_at: datetime
    expires_at: datetime


SUPPORTED_MIME_TYPES = frozenset(
    {
        "text/plain",
        "text/html",
    }
)


def _as_utc(
    value: datetime,
) -> datetime:
    """Validate and normalize an aware timestamp to UTC."""

    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MessageContentStoreError(
            "Content timestamps must include "
            "a timezone"
        )

    return value.astimezone(
        timezone.utc
    )


def _from_iso(
    value: object,
) -> datetime:
    """Load and validate one stored ISO timestamp."""

    if not isinstance(value, str):
        raise MessageContentStoreError(
            "Stored content timestamp is invalid"
        )

    try:
        parsed_value = datetime.fromisoformat(
            value
        )
    except ValueError as exc:
        raise MessageContentStoreError(
            "Stored content timestamp is invalid"
        ) from exc

    return _as_utc(
        parsed_value
    )


def _normalize_identifier(
    value: object,
) -> str:
    """Validate one Gmail message identifier."""

    if not isinstance(value, str):
        raise MessageContentStoreError(
            "gmail_message_id must be a string"
        )

    normalized_value = value.strip()

    if not normalized_value:
        raise MessageContentStoreError(
            "gmail_message_id is required"
        )

    return normalized_value


def _normalize_text(
    value: object,
) -> str:
    """Validate readable message text."""

    if not isinstance(value, str):
        raise MessageContentStoreError(
            "Content text must be a string"
        )

    normalized_value = value.strip()

    if not normalized_value:
        raise MessageContentStoreError(
            "Content text is required"
        )

    return normalized_value


def _normalize_links(
    links: Sequence[str],
) -> tuple[str, ...]:
    """Validate and deduplicate extracted links."""

    if (
        isinstance(links, (str, bytes))
        or not isinstance(links, Sequence)
    ):
        raise MessageContentStoreError(
            "Content links must be a sequence"
        )

    normalized_links: list[str] = []
    seen_links: set[str] = set()

    for link in links:
        if not isinstance(link, str):
            raise MessageContentStoreError(
                "Content links must contain strings"
            )

        normalized_link = link.strip()

        if not normalized_link:
            raise MessageContentStoreError(
                "Content links cannot contain "
                "empty values"
            )

        if normalized_link in seen_links:
            continue

        seen_links.add(
            normalized_link
        )
        normalized_links.append(
            normalized_link
        )

    return tuple(
        normalized_links
    )


def _normalize_mime_type(
    value: object,
) -> str:
    """Validate the MIME type selected by the parser."""

    if not isinstance(value, str):
        raise MessageContentStoreError(
            "source_mime_type must be a string"
        )

    normalized_value = (
        value.strip().casefold()
    )

    if (
        normalized_value
        not in SUPPORTED_MIME_TYPES
    ):
        raise MessageContentStoreError(
            "Unsupported source MIME type"
        )

    return normalized_value


def _content_from_row(
    row: sqlite3.Row,
) -> StoredMessageContent:
    """Convert one database row into stored content."""

    try:
        links_data = json.loads(
            row["links_json"]
        )
    except (
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise MessageContentStoreError(
            "Stored content links are invalid"
        ) from exc

    if (
        not isinstance(links_data, list)
        or any(
            not isinstance(link, str)
            or not link.strip()
            for link in links_data
        )
    ):
        raise MessageContentStoreError(
            "Stored content links are invalid"
        )

    extracted_at = _from_iso(
        row["extracted_at"]
    )
    expires_at = _from_iso(
        row["expires_at"]
    )

    if expires_at <= extracted_at:
        raise MessageContentStoreError(
            "Stored content expiration is invalid"
        )

    return StoredMessageContent(
        gmail_message_row_id=(
            row["gmail_message_row_id"]
        ),
        gmail_message_id=(
            row["gmail_message_id"]
        ),
        text=_normalize_text(
            row["text_content"]
        ),
        links=tuple(
            link.strip()
            for link in links_data
        ),
        source_mime_type=(
            _normalize_mime_type(
                row["source_mime_type"]
            )
        ),
        extracted_at=extracted_at,
        expires_at=expires_at,
    )


class MessageContentStore:
    """Persist readable message content with bounded retention."""

    ACCOUNT_ID = (
        OAuthCredentialStore.ACCOUNT_ID
    )

    def __init__(
        self,
        database: Database,
        *,
        retention_days: int,
    ) -> None:
        if (
            isinstance(retention_days, bool)
            or not isinstance(
                retention_days,
                int,
            )
            or retention_days <= 0
        ):
            raise ValueError(
                "retention_days must be "
                "a positive integer"
            )

        self.database = database
        self.retention_days = (
            retention_days
        )
        self.database.initialize()

    def save_content(
        self,
        gmail_message_id: str,
        *,
        text: str,
        links: Sequence[str],
        source_mime_type: str,
        extracted_at: datetime | None = None,
    ) -> StoredMessageContent:
        """Create or replace the content for one registered message."""

        message_id = _normalize_identifier(
            gmail_message_id
        )
        normalized_text = _normalize_text(
            text
        )
        normalized_links = _normalize_links(
            links
        )
        normalized_mime_type = (
            _normalize_mime_type(
                source_mime_type
            )
        )

        extraction_time = _as_utc(
            extracted_at
            or datetime.now(
                timezone.utc
            )
        )
        expiration_time = (
            extraction_time
            + timedelta(
                days=self.retention_days
            )
        )

        links_json = json.dumps(
            normalized_links,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        with self.database.connect() as connection:
            message_row = connection.execute(
                """
                SELECT id
                FROM gmail_messages
                WHERE account_id = ?
                  AND gmail_message_id = ?
                """,
                (
                    self.ACCOUNT_ID,
                    message_id,
                ),
            ).fetchone()

            if message_row is None:
                raise MessageContentStoreError(
                    "The Gmail message has not "
                    "been registered"
                )

            gmail_message_row_id = (
                message_row["id"]
            )

            connection.execute(
                """
                INSERT INTO message_contents (
                    gmail_message_row_id,
                    text_content,
                    links_json,
                    source_mime_type,
                    extracted_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(gmail_message_row_id)
                DO UPDATE SET
                    text_content = excluded.text_content,
                    links_json = excluded.links_json,
                    source_mime_type = excluded.source_mime_type,
                    extracted_at = excluded.extracted_at,
                    expires_at = excluded.expires_at
                """,
                (
                    gmail_message_row_id,
                    normalized_text,
                    links_json,
                    normalized_mime_type,
                    extraction_time.isoformat(),
                    expiration_time.isoformat(),
                ),
            )

        return StoredMessageContent(
            gmail_message_row_id=(
                gmail_message_row_id
            ),
            gmail_message_id=message_id,
            text=normalized_text,
            links=normalized_links,
            source_mime_type=(
                normalized_mime_type
            ),
            extracted_at=extraction_time,
            expires_at=expiration_time,
        )

    def load_content(
        self,
        gmail_message_id: str,
    ) -> StoredMessageContent | None:
        """Load content for one registered Gmail message."""

        message_id = _normalize_identifier(
            gmail_message_id
        )

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    message_contents.gmail_message_row_id,
                    gmail_messages.gmail_message_id,
                    message_contents.text_content,
                    message_contents.links_json,
                    message_contents.source_mime_type,
                    message_contents.extracted_at,
                    message_contents.expires_at
                FROM message_contents
                INNER JOIN gmail_messages
                    ON gmail_messages.id = (
                        message_contents.gmail_message_row_id
                    )
                WHERE gmail_messages.account_id = ?
                  AND gmail_messages.gmail_message_id = ?
                """,
                (
                    self.ACCOUNT_ID,
                    message_id,
                ),
            ).fetchone()

        if row is None:
            return None

        return _content_from_row(
            row
        )

    def purge_expired(
        self,
        expired_at: datetime | None = None,
    ) -> int:
        """Delete content whose retention period has ended."""

        expiration_limit = _as_utc(
            expired_at
            or datetime.now(
                timezone.utc
            )
        )

        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM message_contents
                WHERE expires_at <= ?
                  AND gmail_message_row_id IN (
                      SELECT id
                      FROM gmail_messages
                      WHERE account_id = ?
                  )
                """,
                (
                    expiration_limit.isoformat(),
                    self.ACCOUNT_ID,
                ),
            )

            deleted_count = cursor.rowcount

        return deleted_count
