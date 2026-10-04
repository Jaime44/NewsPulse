from __future__ import annotations

import json
import sqlite3

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from app.backend.bd.db import Database
from app.backend.bd.oauth_store import OAuthCredentialStore


class IngestionStoreError(RuntimeError):
    """Raised when ingestion state cannot be persisted."""


@dataclass(frozen=True, slots=True)
class IngestionState:
    account_id: int
    started_at: datetime
    last_successful_scan_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredGmailMessage:
    id: int
    account_id: int
    gmail_message_id: str
    gmail_thread_id: str
    internal_date_ms: int
    status: str
    discovered_at: datetime
    updated_at: datetime
    processed_at: datetime | None
    last_error_code: str | None

@dataclass(frozen=True, slots=True)
class StoredMessageClassification:
    gmail_message_row_id: int
    gmail_message_id: str
    verdict: str
    score: int
    reasons: tuple[str, ...]
    classifier_version: str
    classified_at: datetime


CLASSIFICATION_VERDICTS = frozenset(
    {
        "newsletter",
        "review",
        "not_newsletter",
    }
)

def _as_utc(value: datetime) -> datetime:
    """Validate and normalize an aware timestamp to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise IngestionStoreError(
            "Ingestion timestamps must include a timezone"
        )

    return value.astimezone(timezone.utc)


def _from_iso(value: str) -> datetime:
    """Load an ISO timestamp and normalize it to UTC."""

    return _as_utc(datetime.fromisoformat(value))


def _optional_from_iso(
    value: str | None,
) -> datetime | None:
    if value is None:
        return None

    return _from_iso(value)


def _state_from_row(
    row: sqlite3.Row,
) -> IngestionState:
    return IngestionState(
        account_id=row["account_id"],
        started_at=_from_iso(row["started_at"]),
        last_successful_scan_at=_optional_from_iso(
            row["last_successful_scan_at"]
        ),
        created_at=_from_iso(row["created_at"]),
        updated_at=_from_iso(row["updated_at"]),
    )


def _required_identifier(
    value: str,
    field_name: str,
) -> str:
    """Validate and normalize an external identifier."""

    normalized_value = value.strip()

    if not normalized_value:
        raise IngestionStoreError(
            f"{field_name} is required"
        )

    return normalized_value


def _message_from_row(
    row: sqlite3.Row,
) -> StoredGmailMessage:
    return StoredGmailMessage(
        id=row["id"],
        account_id=row["account_id"],
        gmail_message_id=row["gmail_message_id"],
        gmail_thread_id=row["gmail_thread_id"],
        internal_date_ms=row["internal_date_ms"],
        status=row["status"],
        discovered_at=_from_iso(
            row["discovered_at"]
        ),
        updated_at=_from_iso(
            row["updated_at"]
        ),
        processed_at=_optional_from_iso(
            row["processed_at"]
        ),
        last_error_code=row["last_error_code"],
    )

def _classification_from_row(
    row: sqlite3.Row,
) -> StoredMessageClassification:
    try:
        reasons_data = json.loads(
            row["reasons_json"]
        )
    except (
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise IngestionStoreError(
            "Stored classification reasons are invalid"
        ) from exc

    if (
        not isinstance(reasons_data, list)
        or any(
            not isinstance(reason, str)
            or not reason.strip()
            for reason in reasons_data
        )
    ):
        raise IngestionStoreError(
            "Stored classification reasons are invalid"
        )

    return StoredMessageClassification(
        gmail_message_row_id=(
            row["gmail_message_row_id"]
        ),
        gmail_message_id=row["gmail_message_id"],
        verdict=row["verdict"],
        score=row["score"],
        reasons=tuple(reasons_data),
        classifier_version=(
            row["classifier_version"]
        ),
        classified_at=_from_iso(
            row["classified_at"]
        ),
    )

def _normalize_classification_reasons(
    reasons: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(reasons, (str, bytes)):
        raise IngestionStoreError(
            "classification reasons must "
            "be a sequence"
        )

    normalized_reasons: list[str] = []

    for reason in reasons:
        if (
            not isinstance(reason, str)
            or not reason.strip()
        ):
            raise IngestionStoreError(
                "classification reasons cannot "
                "contain empty values"
            )

        normalized_reasons.append(
            reason.strip()
        )

    return tuple(normalized_reasons)

class IngestionStore:
    """Persist incremental Gmail ingestion state."""

    ACCOUNT_ID = OAuthCredentialStore.ACCOUNT_ID

    def __init__(
        self,
        database: Database,
    ) -> None:
        self.database = database
        self.database.initialize()

    def load_state(self) -> IngestionState | None:
        """Load the ingestion state for the configured account."""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    account_id,
                    started_at,
                    last_successful_scan_at,
                    created_at,
                    updated_at
                FROM ingestion_state
                WHERE account_id = ?
                """,
                (self.ACCOUNT_ID,),
            ).fetchone()

        if row is None:
            return None

        return _state_from_row(row)

    def get_or_create_state(
        self,
        started_at: datetime | None = None,
    ) -> IngestionState:
        """Create the initial cursor once, or return it unchanged."""

        now = datetime.now(timezone.utc)
        initial_time = _as_utc(
            started_at or now
        )

        try:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO ingestion_state (
                        account_id,
                        started_at,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(account_id) DO NOTHING
                    """,
                    (
                        self.ACCOUNT_ID,
                        initial_time.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

                row = connection.execute(
                    """
                    SELECT
                        account_id,
                        started_at,
                        last_successful_scan_at,
                        created_at,
                        updated_at
                    FROM ingestion_state
                    WHERE account_id = ?
                    """,
                    (self.ACCOUNT_ID,),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise IngestionStoreError(
                "An authenticated account is required "
                "before starting ingestion"
            ) from exc

        if row is None:
            raise IngestionStoreError(
                "Ingestion state could not be loaded"
            )

        return _state_from_row(row)

    def mark_scan_successful(
        self,
        completed_at: datetime | None = None,
    ) -> IngestionState:
        """Advance the cursor after a complete successful scan."""

        completion_time = _as_utc(
            completed_at or datetime.now(timezone.utc)
        )

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    account_id,
                    started_at,
                    last_successful_scan_at,
                    created_at,
                    updated_at
                FROM ingestion_state
                WHERE account_id = ?
                """,
                (self.ACCOUNT_ID,),
            ).fetchone()

            if row is None:
                raise IngestionStoreError(
                    "Ingestion state has not been initialized"
                )

            current_state = _state_from_row(row)

            if completion_time < current_state.started_at:
                raise IngestionStoreError(
                    "A scan cannot finish before ingestion starts"
                )

            last_scan = (
                current_state.last_successful_scan_at
            )

            if (
                last_scan is not None
                and completion_time < last_scan
            ):
                raise IngestionStoreError(
                    "The ingestion cursor cannot move backwards"
                )

            connection.execute(
                """
                UPDATE ingestion_state
                SET last_successful_scan_at = ?,
                    updated_at = ?
                WHERE account_id = ?
                """,
                (
                    completion_time.isoformat(),
                    completion_time.isoformat(),
                    self.ACCOUNT_ID,
                ),
            )

        return IngestionState(
            account_id=current_state.account_id,
            started_at=current_state.started_at,
            last_successful_scan_at=completion_time,
            created_at=current_state.created_at,
            updated_at=completion_time,
        )

    def register_message(
        self,
        gmail_message_id: str,
        gmail_thread_id: str,
        internal_date_ms: int,
        discovered_at: datetime | None = None,
    ) -> bool:
        """Register a Gmail message once.

        Return True when inserted and False when already known.
        """

        message_id = _required_identifier(
            gmail_message_id,
            "gmail_message_id",
        )
        thread_id = _required_identifier(
            gmail_thread_id,
            "gmail_thread_id",
        )

        if (
            isinstance(internal_date_ms, bool)
            or not isinstance(internal_date_ms, int)
            or internal_date_ms < 0
        ):
            raise IngestionStoreError(
                "internal_date_ms must be a non-negative integer"
            )

        observed_at = _as_utc(
            discovered_at or datetime.now(timezone.utc)
        )

        try:
            with self.database.connect() as connection:
                ingestion_exists = connection.execute(
                    """
                    SELECT 1
                    FROM ingestion_state
                    WHERE account_id = ?
                    """,
                    (self.ACCOUNT_ID,),
                ).fetchone()

                if ingestion_exists is None:
                    raise IngestionStoreError(
                        "Ingestion state has not been initialized"
                    )

                cursor = connection.execute(
                    """
                    INSERT INTO gmail_messages (
                        account_id,
                        gmail_message_id,
                        gmail_thread_id,
                        internal_date_ms,
                        status,
                        discovered_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, 'discovered', ?, ?)
                    ON CONFLICT(
                        account_id,
                        gmail_message_id
                    ) DO NOTHING
                    """,
                    (
                        self.ACCOUNT_ID,
                        message_id,
                        thread_id,
                        internal_date_ms,
                        observed_at.isoformat(),
                        observed_at.isoformat(),
                    ),
                )

                return cursor.rowcount == 1
        except sqlite3.IntegrityError as exc:
            raise IngestionStoreError(
                "The Gmail message could not be registered"
            ) from exc

    def load_message(
        self,
        gmail_message_id: str,
    ) -> StoredGmailMessage | None:
        """Load one previously registered Gmail message."""

        message_id = _required_identifier(
            gmail_message_id,
            "gmail_message_id",
        )

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    account_id,
                    gmail_message_id,
                    gmail_thread_id,
                    internal_date_ms,
                    status,
                    discovered_at,
                    updated_at,
                    processed_at,
                    last_error_code
                FROM gmail_messages
                WHERE account_id = ?
                AND gmail_message_id = ?
                """,
                (
                    self.ACCOUNT_ID,
                    message_id,
                ),
            ).fetchone()

        if row is None:
            return None

        return _message_from_row(row)

    def is_message_known(
        self,
        gmail_message_id: str,
    ) -> bool:
        """Return whether a Gmail message was already discovered."""

        return self.load_message(
            gmail_message_id
        ) is not None

    def save_message_classification(
        self,
        gmail_message_id: str,
        *,
        verdict: str,
        score: int,
        reasons: Sequence[str],
        classifier_version: str,
        classified_at: datetime | None = None,
    ) -> StoredMessageClassification:
        """Save a classification and update message state atomically."""

        message_id = _required_identifier(
            gmail_message_id,
            "gmail_message_id",
        )

        if not isinstance(verdict, str):
            raise IngestionStoreError(
                "classification verdict must be a string"
            )

        normalized_verdict = (
            verdict.strip().casefold()
        )

        if (
            normalized_verdict
            not in CLASSIFICATION_VERDICTS
        ):
            raise IngestionStoreError(
                "Unsupported classification verdict"
            )

        if (
            isinstance(score, bool)
            or not isinstance(score, int)
            or score < 0
        ):
            raise IngestionStoreError(
                "classification score must be "
                "a non-negative integer"
            )

        normalized_reasons = (
            _normalize_classification_reasons(
                reasons
            )
        )

        if (
            not isinstance(classifier_version, str)
            or not classifier_version.strip()
        ):
            raise IngestionStoreError(
                "classifier_version is required"
            )

        normalized_version = (
            classifier_version.strip()
        )
        classification_time = _as_utc(
            classified_at
            or datetime.now(timezone.utc)
        )

        reasons_json = json.dumps(
            normalized_reasons,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        if normalized_verdict == "not_newsletter":
            message_status = "skipped"
            processed_at = (
                classification_time.isoformat()
            )
        else:
            message_status = "discovered"
            processed_at = None

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
                raise IngestionStoreError(
                    "The Gmail message has not "
                    "been registered"
                )

            message_row_id = message_row["id"]

            connection.execute(
                """
                INSERT INTO message_classifications (
                    gmail_message_row_id,
                    verdict,
                    score,
                    reasons_json,
                    classifier_version,
                    classified_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(gmail_message_row_id)
                DO UPDATE SET
                    verdict = excluded.verdict,
                    score = excluded.score,
                    reasons_json = excluded.reasons_json,
                    classifier_version =
                        excluded.classifier_version,
                    classified_at =
                        excluded.classified_at
                """,
                (
                    message_row_id,
                    normalized_verdict,
                    score,
                    reasons_json,
                    normalized_version,
                    classification_time.isoformat(),
                ),
            )

            connection.execute(
                """
                UPDATE gmail_messages
                SET status = ?,
                    updated_at = ?,
                    processed_at = ?,
                    last_error_code = NULL
                WHERE id = ?
                """,
                (
                    message_status,
                    classification_time.isoformat(),
                    processed_at,
                    message_row_id,
                ),
            )

            row = connection.execute(
                """
                SELECT
                    classifications.gmail_message_row_id,
                    messages.gmail_message_id,
                    classifications.verdict,
                    classifications.score,
                    classifications.reasons_json,
                    classifications.classifier_version,
                    classifications.classified_at
                FROM message_classifications
                    AS classifications
                JOIN gmail_messages AS messages
                  ON messages.id = (
                      classifications
                      .gmail_message_row_id
                  )
                WHERE messages.account_id = ?
                  AND messages.gmail_message_id = ?
                """,
                (
                    self.ACCOUNT_ID,
                    message_id,
                ),
            ).fetchone()

        if row is None:
            raise IngestionStoreError(
                "Message classification could not "
                "be loaded"
            )

        return _classification_from_row(row)

    def load_message_classification(
        self,
        gmail_message_id: str,
    ) -> StoredMessageClassification | None:
        """Load the latest classification for one message."""

        message_id = _required_identifier(
            gmail_message_id,
            "gmail_message_id",
        )

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    classifications.gmail_message_row_id,
                    messages.gmail_message_id,
                    classifications.verdict,
                    classifications.score,
                    classifications.reasons_json,
                    classifications.classifier_version,
                    classifications.classified_at
                FROM message_classifications
                    AS classifications
                JOIN gmail_messages AS messages
                  ON messages.id = (
                      classifications
                      .gmail_message_row_id
                  )
                WHERE messages.account_id = ?
                  AND messages.gmail_message_id = ?
                """,
                (
                    self.ACCOUNT_ID,
                    message_id,
                ),
            ).fetchone()

        if row is None:
            return None

        return _classification_from_row(row)