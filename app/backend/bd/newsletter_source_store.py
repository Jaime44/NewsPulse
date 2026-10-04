from __future__ import annotations

import sqlite3

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr

from app.backend.bd.db import Database
from app.backend.bd.oauth_store import OAuthCredentialStore


class NewsletterSourceStoreError(RuntimeError):
    """Raised when newsletter sources cannot be persisted."""


@dataclass(frozen=True, slots=True)
class NewsletterSource:
    id: int
    account_id: int
    source_type: str
    source_value: str
    decision: str
    origin: str
    confidence: int
    active: bool
    created_at: datetime
    updated_at: datetime
    last_matched_at: datetime | None


SOURCE_TYPES = frozenset(
    {
        "sender",
        "list_id",
        "gmail_label",
        "domain",
    }
)

DECISIONS = frozenset(
    {
        "include",
        "exclude",
    }
)

ORIGINS = frozenset(
    {
        "manual",
        "confirmed",
        "automatic",
    }
)


def _as_utc(value: datetime) -> datetime:
    """Validate and normalize an aware timestamp to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise NewsletterSourceStoreError(
            "Newsletter source timestamps must "
            "include a timezone"
        )

    return value.astimezone(timezone.utc)


def _from_iso(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _optional_from_iso(
    value: str | None,
) -> datetime | None:
    if value is None:
        return None

    return _from_iso(value)


def _normalize_option(
    value: str,
    allowed_values: frozenset[str],
    field_name: str,
) -> str:
    if not isinstance(value, str):
        raise NewsletterSourceStoreError(
            f"{field_name} must be a string"
        )

    normalized_value = value.strip().casefold()

    if normalized_value not in allowed_values:
        raise NewsletterSourceStoreError(
            f"Unsupported {field_name}"
        )

    return normalized_value


def normalize_source_value(
    source_type: str,
    source_value: str,
) -> str:
    """Normalize a newsletter source for reliable matching."""

    normalized_type = _normalize_option(
        source_type,
        SOURCE_TYPES,
        "source_type",
    )

    if not isinstance(source_value, str):
        raise NewsletterSourceStoreError(
            "source_value must be a string"
        )

    normalized_value = source_value.strip()

    if not normalized_value:
        raise NewsletterSourceStoreError(
            "source_value is required"
        )

    if normalized_type == "sender":
        address = parseaddr(normalized_value)[1]
        normalized_value = address.strip().casefold()

        local_part, separator, domain = (
            normalized_value.partition("@")
        )

        if (
            not separator
            or not local_part
            or not domain
            or "@" in domain
        ):
            raise NewsletterSourceStoreError(
                "sender must contain a valid email address"
            )

    elif normalized_type == "list_id":
        opening_bracket = normalized_value.rfind("<")

        if (
            opening_bracket >= 0
            and normalized_value.endswith(">")
        ):
            normalized_value = (
                normalized_value[
                    opening_bracket + 1:-1
                ].strip()
            )

        normalized_value = normalized_value.casefold()

    elif normalized_type == "domain":
        normalized_value = (
            normalized_value
            .removeprefix("@")
            .removesuffix(".")
            .casefold()
        )

    if not normalized_value:
        raise NewsletterSourceStoreError(
            "source_value is required"
        )

    return normalized_value


def _source_from_row(
    row: sqlite3.Row,
) -> NewsletterSource:
    return NewsletterSource(
        id=row["id"],
        account_id=row["account_id"],
        source_type=row["source_type"],
        source_value=row["source_value"],
        decision=row["decision"],
        origin=row["origin"],
        confidence=row["confidence"],
        active=bool(row["active"]),
        created_at=_from_iso(row["created_at"]),
        updated_at=_from_iso(row["updated_at"]),
        last_matched_at=_optional_from_iso(
            row["last_matched_at"]
        ),
    )


class NewsletterSourceStore:
    """Persist known newsletter inclusion and exclusion rules."""

    ACCOUNT_ID = OAuthCredentialStore.ACCOUNT_ID

    def __init__(
        self,
        database: Database,
    ) -> None:
        self.database = database
        self.database.initialize()

    def save_source(
        self,
        source_type: str,
        source_value: str,
        *,
        decision: str = "include",
        origin: str = "manual",
        confidence: int = 100,
        observed_at: datetime | None = None,
    ) -> NewsletterSource:
        """Create or update one newsletter source rule."""

        normalized_type = _normalize_option(
            source_type,
            SOURCE_TYPES,
            "source_type",
        )
        normalized_value = normalize_source_value(
            normalized_type,
            source_value,
        )
        normalized_decision = _normalize_option(
            decision,
            DECISIONS,
            "decision",
        )
        normalized_origin = _normalize_option(
            origin,
            ORIGINS,
            "origin",
        )

        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int)
            or not 0 <= confidence <= 100
        ):
            raise NewsletterSourceStoreError(
                "confidence must be an integer "
                "between 0 and 100"
            )

        current_time = _as_utc(
            observed_at or datetime.now(timezone.utc)
        )

        try:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO newsletter_sources (
                        account_id,
                        source_type,
                        source_value,
                        decision,
                        origin,
                        confidence,
                        active,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(
                        account_id,
                        source_type,
                        source_value
                    )
                    DO UPDATE SET
                        decision = excluded.decision,
                        origin = excluded.origin,
                        confidence = excluded.confidence,
                        active = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        self.ACCOUNT_ID,
                        normalized_type,
                        normalized_value,
                        normalized_decision,
                        normalized_origin,
                        confidence,
                        current_time.isoformat(),
                        current_time.isoformat(),
                    ),
                )

                row = connection.execute(
                    """
                    SELECT
                        id,
                        account_id,
                        source_type,
                        source_value,
                        decision,
                        origin,
                        confidence,
                        active,
                        created_at,
                        updated_at,
                        last_matched_at
                    FROM newsletter_sources
                    WHERE account_id = ?
                      AND source_type = ?
                      AND source_value = ?
                    """,
                    (
                        self.ACCOUNT_ID,
                        normalized_type,
                        normalized_value,
                    ),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise NewsletterSourceStoreError(
                "An authenticated account is required "
                "before saving newsletter sources"
            ) from exc

        if row is None:
            raise NewsletterSourceStoreError(
                "Newsletter source could not be loaded"
            )

        return _source_from_row(row)

    def load_source(
        self,
        source_type: str,
        source_value: str,
    ) -> NewsletterSource | None:
        """Load one normalized newsletter source rule."""

        normalized_type = _normalize_option(
            source_type,
            SOURCE_TYPES,
            "source_type",
        )
        normalized_value = normalize_source_value(
            normalized_type,
            source_value,
        )

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    account_id,
                    source_type,
                    source_value,
                    decision,
                    origin,
                    confidence,
                    active,
                    created_at,
                    updated_at,
                    last_matched_at
                FROM newsletter_sources
                WHERE account_id = ?
                  AND source_type = ?
                  AND source_value = ?
                """,
                (
                    self.ACCOUNT_ID,
                    normalized_type,
                    normalized_value,
                ),
            ).fetchone()

        if row is None:
            return None

        return _source_from_row(row)

    def list_active_sources(
        self,
        decision: str | None = None,
    ) -> tuple[NewsletterSource, ...]:
        """List active sources, optionally filtered by decision."""

        if decision is None:
            with self.database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        id,
                        account_id,
                        source_type,
                        source_value,
                        decision,
                        origin,
                        confidence,
                        active,
                        created_at,
                        updated_at,
                        last_matched_at
                    FROM newsletter_sources
                    WHERE account_id = ?
                      AND active = 1
                    ORDER BY
                        source_type,
                        source_value
                    """,
                    (self.ACCOUNT_ID,),
                ).fetchall()
        else:
            normalized_decision = _normalize_option(
                decision,
                DECISIONS,
                "decision",
            )

            with self.database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        id,
                        account_id,
                        source_type,
                        source_value,
                        decision,
                        origin,
                        confidence,
                        active,
                        created_at,
                        updated_at,
                        last_matched_at
                    FROM newsletter_sources
                    WHERE account_id = ?
                      AND active = 1
                      AND decision = ?
                    ORDER BY
                        source_type,
                        source_value
                    """,
                    (
                        self.ACCOUNT_ID,
                        normalized_decision,
                    ),
                ).fetchall()

        return tuple(
            _source_from_row(row)
            for row in rows
        )

    def deactivate_source(
        self,
        source_type: str,
        source_value: str,
        *,
        changed_at: datetime | None = None,
    ) -> bool:
        """Deactivate an existing source without deleting it."""

        normalized_type = _normalize_option(
            source_type,
            SOURCE_TYPES,
            "source_type",
        )
        normalized_value = normalize_source_value(
            normalized_type,
            source_value,
        )
        current_time = _as_utc(
            changed_at or datetime.now(timezone.utc)
        )

        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE newsletter_sources
                SET active = 0,
                    updated_at = ?
                WHERE account_id = ?
                  AND source_type = ?
                  AND source_value = ?
                  AND active = 1
                """,
                (
                    current_time.isoformat(),
                    self.ACCOUNT_ID,
                    normalized_type,
                    normalized_value,
                ),
            )

        return cursor.rowcount == 1

    def mark_source_matched(
        self,
        source_type: str,
        source_value: str,
        *,
        matched_at: datetime | None = None,
    ) -> NewsletterSource | None:
        """Record a match only for an active source."""

        normalized_type = _normalize_option(
            source_type,
            SOURCE_TYPES,
            "source_type",
        )
        normalized_value = normalize_source_value(
            normalized_type,
            source_value,
        )
        current_time = _as_utc(
            matched_at or datetime.now(timezone.utc)
        )

        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE newsletter_sources
                SET last_matched_at = ?,
                    updated_at = ?
                WHERE account_id = ?
                  AND source_type = ?
                  AND source_value = ?
                  AND active = 1
                """,
                (
                    current_time.isoformat(),
                    current_time.isoformat(),
                    self.ACCOUNT_ID,
                    normalized_type,
                    normalized_value,
                ),
            )

            if cursor.rowcount != 1:
                return None

            row = connection.execute(
                """
                SELECT
                    id,
                    account_id,
                    source_type,
                    source_value,
                    decision,
                    origin,
                    confidence,
                    active,
                    created_at,
                    updated_at,
                    last_matched_at
                FROM newsletter_sources
                WHERE account_id = ?
                  AND source_type = ?
                  AND source_value = ?
                """,
                (
                    self.ACCOUNT_ID,
                    normalized_type,
                    normalized_value,
                ),
            ).fetchone()

        if row is None:
            raise NewsletterSourceStoreError(
                "Matched newsletter source "
                "could not be loaded"
            )

        return _source_from_row(row)
