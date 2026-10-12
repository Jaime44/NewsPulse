"""Persist incremental Gmail discovery, processing, and audit state.

This module maps SQLite rows to immutable domain records and implements the
single-account ingestion cursor, durable run outcomes, message lifecycle, and
classifier-result storage used by
:class:`~app.backend.services.gmail_ingestion.GmailIngestionService`.
"""

from __future__ import annotations

import json
import sqlite3

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.backend.bd.db import Database
from app.backend.bd.oauth_store import OAuthCredentialStore


class IngestionStoreError(RuntimeError):
    """Raised when ingestion state cannot be persisted."""


class IngestionRunAlreadyActiveError(IngestionStoreError):
    """Raised when another process owns the current ingestion lease."""


class IngestionLeaseLostError(IngestionStoreError):
    """Raised when a run no longer owns a valid ingestion lease."""


@dataclass(frozen=True, slots=True)
class IngestionState:
    """Represent the durable cursor and timestamps for one Gmail account."""

    account_id: int
    started_at: datetime
    last_successful_scan_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionRunCounters:
    """Hold non-sensitive counters recorded for one ingestion execution.

    Attributes:
        listed_count: Gmail references returned by the complete listing.
        discovered_count: Previously unseen messages registered by the run.
        already_known_count: References already represented in storage.
        ignored_before_start_count: Old messages excluded by the start boundary.
        newsletter_count: New classifications resolved as newsletters.
        review_count: New classifications requiring review.
        not_newsletter_count: New classifications rejected as newsletters.
        purged_content_count: Expired retained-content rows removed before scan.
    """

    listed_count: int = 0
    discovered_count: int = 0
    already_known_count: int = 0
    ignored_before_start_count: int = 0
    newsletter_count: int = 0
    review_count: int = 0
    not_newsletter_count: int = 0
    purged_content_count: int = 0


@dataclass(frozen=True, slots=True)
class StoredGmailMessage:
    """Represent one Gmail message and its current processing state."""

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
    """Represent the persisted classifier decision for one Gmail message."""

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

INGESTION_COUNTER_FIELDS = (
    "listed_count",
    "discovered_count",
    "already_known_count",
    "ignored_before_start_count",
    "newsletter_count",
    "review_count",
    "not_newsletter_count",
    "purged_content_count",
)

def _as_utc(value: datetime) -> datetime:
    """Validate and normalize an aware timestamp to UTC.

    Args:
        value: Timestamp to normalize.

    Returns:
        The equivalent timezone-aware UTC timestamp.

    Raises:
        IngestionStoreError: If ``value`` has no timezone information.
    """

    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise IngestionStoreError(
            "Ingestion timestamps must include a timezone"
        )

    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


def _from_iso(value: str) -> datetime:
    """Parse an ISO timestamp and normalize it to UTC.

    Args:
        value: ISO-formatted timestamp loaded from SQLite.

    Returns:
        The parsed timezone-aware UTC timestamp.

    Raises:
        ValueError: If the value is not valid ISO datetime text.
        IngestionStoreError: If the parsed timestamp is timezone-naive.
    """

    return _as_utc(datetime.fromisoformat(value))


def _optional_from_iso(
    value: str | None,
) -> datetime | None:
    """Parse an optional stored ISO timestamp.

    Args:
        value: ISO-formatted timestamp or ``None``.

    Returns:
        A timezone-aware UTC timestamp, or ``None``.
    """

    if value is None:
        return None

    return _from_iso(value)


def _state_from_row(
    row: sqlite3.Row,
) -> IngestionState:
    """Map an ingestion-state row to its immutable domain record.

    Args:
        row: SQLite row containing all ingestion-state columns.

    Returns:
        The reconstructed ingestion state.
    """

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
    """Validate and normalize an external identifier.

    Args:
        value: Identifier supplied by the caller.
        field_name: Name included in validation errors.

    Returns:
        The stripped non-empty identifier.

    Raises:
        IngestionStoreError: If the identifier is empty.
    """

    normalized_value = value.strip()

    if not normalized_value:
        raise IngestionStoreError(
            f"{field_name} is required"
        )

    return normalized_value


def _message_from_row(
    row: sqlite3.Row,
) -> StoredGmailMessage:
    """Map a Gmail-message row to its immutable domain record.

    Args:
        row: SQLite row containing all message columns.

    Returns:
        The reconstructed stored message.
    """

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
    """Map and validate a classification row from SQLite.

    Args:
        row: Joined classification and Gmail-message row.

    Returns:
        The reconstructed classification record.

    Raises:
        IngestionStoreError: If the serialized reason list is malformed.
    """

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
    """Validate and normalize classifier reason codes.

    Args:
        reasons: Ordered reason codes produced by the classifier.

    Returns:
        A tuple of stripped, non-empty reason strings.

    Raises:
        IngestionStoreError: If the collection or an item is invalid.
    """

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


def _validated_run_id(
    run_id: int,
) -> int:
    """Validate one internal ingestion-run identifier.

    Args:
        run_id: Candidate primary key returned by :meth:`start_scan_run`.

    Returns:
        The validated positive integer identifier.

    Raises:
        IngestionStoreError: If the identifier is not a positive integer.
    """

    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
    ):
        raise IngestionStoreError(
            "run_id must be a positive integer"
        )

    return run_id


def _normalized_error_code(
    error_code: str,
) -> str:
    """Validate a bounded non-sensitive persistence error code.

    Args:
        error_code: Candidate code that must not contain exception details.

    Returns:
        The stripped code of at most 100 characters.

    Raises:
        IngestionStoreError: If the code is empty, not text, or too long.
    """

    if (
        not isinstance(error_code, str)
        or not error_code.strip()
    ):
        raise IngestionStoreError(
            "error_code is required"
        )

    normalized_error_code = error_code.strip()

    if len(normalized_error_code) > 100:
        raise IngestionStoreError(
            "error_code must not exceed "
            "100 characters"
        )

    return normalized_error_code


def _validated_run_counters(
    counters: IngestionRunCounters,
) -> tuple[int, ...]:
    """Validate and flatten one scan-counter collection for SQLite.

    Args:
        counters: Structured scan counters supplied by the ingestion service.

    Returns:
        Counter values in :data:`INGESTION_COUNTER_FIELDS` order.

    Raises:
        IngestionStoreError: If the value or any counter is invalid.
    """

    if not isinstance(
        counters,
        IngestionRunCounters,
    ):
        raise IngestionStoreError(
            "counters must be IngestionRunCounters"
        )

    values = tuple(
        getattr(counters, field_name)
        for field_name in INGESTION_COUNTER_FIELDS
    )

    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for value in values
    ):
        raise IngestionStoreError(
            "ingestion counters must be "
            "non-negative integers"
        )

    return values

class IngestionStore:
    """Persist Gmail ingestion state, run outcomes, and message transitions.

    The store owns the durable scan cursor and run completion transaction,
    idempotent message registration, processing statuses, and explainable
    classification records. Schema triggers preserve message histories.

    Attributes:
        database: SQLite connection and schema factory.
        lease_seconds: Lifetime granted by each successful heartbeat.
        clock: Operational clock used only for lease ownership.
    """

    ACCOUNT_ID = OAuthCredentialStore.ACCOUNT_ID

    def __init__(
        self,
        database: Database,
        *,
        lease_seconds: int = 900,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Initialize the schema used by the ingestion repository.

        Args:
            database: Configured SQLite database factory.
            lease_seconds: Positive lifetime assigned to each lease heartbeat.
            clock: Callable supplying the operational lease time in UTC.

        Raises:
            ValueError: If ``lease_seconds`` is not a positive integer.
        """

        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            raise ValueError(
                "lease_seconds must be a positive integer"
            )

        self.database = database
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.database.initialize()

    def _lease_window(self) -> tuple[datetime, datetime]:
        """Return the current lease heartbeat and exclusive expiry.

        Returns:
            A pair containing the normalized current time and lease expiry.

        Raises:
            IngestionStoreError: If the configured clock returns a naive value.
        """

        heartbeat_at = _as_utc(self.clock())
        return (
            heartbeat_at,
            heartbeat_at + timedelta(
                seconds=self.lease_seconds
            ),
        )

    def load_state(self) -> IngestionState | None:
        """Load the ingestion state for the configured account.

        Returns:
            The durable state, or ``None`` if ingestion has not been initialized.
        """

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

    def start_scan_run(
        self,
        *,
        started_at: datetime | None = None,
    ) -> int:
        """Create the durable running record for one ingestion attempt.

        Args:
            started_at: Optional timezone-aware scan-start timestamp.

        Returns:
            The generated internal ingestion-run identifier.

        Raises:
            IngestionStoreError: If the timestamp is invalid, no authenticated
                account exists, or SQLite does not create the run.
        """

        heartbeat_at, lease_expires_at = (
            self._lease_window()
        )
        start_time = _as_utc(
            started_at or heartbeat_at
        )
        recovered_count = 0

        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                recovery_cursor = connection.execute(
                    """
                    UPDATE ingestion_runs
                    SET status = 'failed',
                        finished_at = CASE
                            WHEN julianday(started_at) > julianday(?)
                                THEN started_at
                            ELSE ?
                        END,
                        cursor_after = NULL,
                        error_code = 'lease_expired',
                        lease_heartbeat_at = NULL,
                        lease_expires_at = NULL
                    WHERE account_id = ?
                      AND status = 'running'
                      AND julianday(lease_expires_at) <= julianday(?)
                    """,
                    (
                        heartbeat_at.isoformat(),
                        heartbeat_at.isoformat(),
                        self.ACCOUNT_ID,
                        heartbeat_at.isoformat(),
                    ),
                )
                recovered_count = recovery_cursor.rowcount

                active_run = connection.execute(
                    """
                    SELECT 1
                    FROM ingestion_runs
                    WHERE account_id = ?
                      AND status = 'running'
                    LIMIT 1
                    """,
                    (self.ACCOUNT_ID,),
                ).fetchone()

                if active_run is not None:
                    raise IngestionRunAlreadyActiveError(
                        "Another ingestion run owns the active lease"
                    )

                cursor = connection.execute(
                    """
                    INSERT INTO ingestion_runs (
                        account_id,
                        status,
                        started_at,
                        lease_heartbeat_at,
                        lease_expires_at
                    )
                    VALUES (?, 'running', ?, ?, ?)
                    """,
                    (
                        self.ACCOUNT_ID,
                        start_time.isoformat(),
                        heartbeat_at.isoformat(),
                        lease_expires_at.isoformat(),
                    ),
                )
                run_id = cursor.lastrowid
        except IngestionRunAlreadyActiveError:
            raise
        except sqlite3.IntegrityError as exc:
            raise IngestionStoreError(
                "An authenticated account is required "
                "before recording an ingestion run"
            ) from exc
        except sqlite3.OperationalError as exc:
            raise IngestionStoreError(
                "SQLite could not acquire the ingestion lease"
            ) from exc

        if (
            isinstance(run_id, bool)
            or not isinstance(run_id, int)
            or run_id <= 0
        ):
            raise IngestionStoreError(
                "The ingestion run could not be created"
            )

        if (
            recovered_count > 0
            and self.database.logger is not None
        ):
            self.database.logger.warning(
                "Recovered expired ingestion lease: recovered_count=%d",
                recovered_count,
            )

        return run_id

    def renew_scan_run(
        self,
        run_id: int,
    ) -> None:
        """Extend the lease held by one active ingestion run.

        Args:
            run_id: Running ingestion record whose lease will be renewed.

        Raises:
            IngestionLeaseLostError: If the run is missing, terminal, expired,
                or has already been superseded by another process.
            IngestionStoreError: If the identifier or clock is invalid.
        """

        normalized_run_id = _validated_run_id(run_id)
        heartbeat_at, lease_expires_at = self._lease_window()

        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_runs
                SET lease_heartbeat_at = ?,
                    lease_expires_at = ?
                WHERE id = ?
                  AND account_id = ?
                  AND status = 'running'
                  AND julianday(lease_expires_at) > julianday(?)
                """,
                (
                    heartbeat_at.isoformat(),
                    lease_expires_at.isoformat(),
                    normalized_run_id,
                    self.ACCOUNT_ID,
                    heartbeat_at.isoformat(),
                ),
            )

            if cursor.rowcount != 1:
                raise IngestionLeaseLostError(
                    "The ingestion lease is no longer active"
                )

    def complete_scan_run(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        cursor_before: datetime,
        cursor_after: datetime,
        counters: IngestionRunCounters,
    ) -> IngestionState:
        """Finish a run and advance its ingestion cursor atomically.

        Args:
            run_id: Running ingestion record to complete.
            finished_at: Time at which the complete scan finished.
            cursor_before: Durable cursor from which discovery started.
            cursor_after: New cursor recorded after complete processing.
            counters: Non-sensitive outcome counters for the run.

        Returns:
            The ingestion state containing the advanced cursor.

        Raises:
            IngestionStoreError: If inputs are invalid, the cursor would move
                backwards, state is missing, or the run is no longer running.

        Notes:
            Cursor advancement and the terminal ``succeeded`` run update share
            one transaction so neither can be committed independently.
        """

        normalized_run_id = _validated_run_id(
            run_id
        )
        finish_time = _as_utc(finished_at)
        previous_cursor = _as_utc(
            cursor_before
        )
        next_cursor = _as_utc(cursor_after)
        counter_values = _validated_run_counters(
            counters
        )
        lease_checked_at, _ = self._lease_window()

        with self.database.connect() as connection:
            run_row = connection.execute(
                """
                SELECT started_at, status, lease_expires_at
                FROM ingestion_runs
                WHERE id = ?
                  AND account_id = ?
                """,
                (
                    normalized_run_id,
                    self.ACCOUNT_ID,
                ),
            ).fetchone()

            if (
                run_row is None
                or run_row["status"] != "running"
                or run_row["lease_expires_at"] is None
                or _from_iso(run_row["lease_expires_at"])
                    <= lease_checked_at
            ):
                raise IngestionLeaseLostError(
                    "The ingestion lease is no longer active"
                )

            run_started_at = _from_iso(
                run_row["started_at"]
            )

            if finish_time < run_started_at:
                raise IngestionStoreError(
                    "An ingestion run cannot finish "
                    "before it starts"
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

            if row is None:
                raise IngestionStoreError(
                    "Ingestion state has not been initialized"
                )

            current_state = _state_from_row(row)

            if previous_cursor != (
                current_state.last_successful_scan_at
                or current_state.started_at
            ):
                raise IngestionStoreError(
                    "The ingestion cursor changed during the scan"
                )

            if next_cursor < current_state.started_at:
                raise IngestionStoreError(
                    "A scan cannot finish before ingestion starts"
                )

            if next_cursor < previous_cursor:
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
                    next_cursor.isoformat(),
                    finish_time.isoformat(),
                    self.ACCOUNT_ID,
                ),
            )

            run_cursor = connection.execute(
                """
                UPDATE ingestion_runs
                SET status = 'succeeded',
                    finished_at = ?,
                    cursor_before = ?,
                    cursor_after = ?,
                    listed_count = ?,
                    discovered_count = ?,
                    already_known_count = ?,
                    ignored_before_start_count = ?,
                    newsletter_count = ?,
                    review_count = ?,
                    not_newsletter_count = ?,
                    purged_content_count = ?,
                    error_code = NULL,
                    lease_heartbeat_at = NULL,
                    lease_expires_at = NULL
                WHERE id = ?
                  AND account_id = ?
                  AND status = 'running'
                  AND julianday(lease_expires_at) > julianday(?)
                """,
                (
                    finish_time.isoformat(),
                    previous_cursor.isoformat(),
                    next_cursor.isoformat(),
                    *counter_values,
                    normalized_run_id,
                    self.ACCOUNT_ID,
                    lease_checked_at.isoformat(),
                ),
            )

            if run_cursor.rowcount != 1:
                raise IngestionLeaseLostError(
                    "The ingestion lease is no longer active"
                )

        return IngestionState(
            account_id=current_state.account_id,
            started_at=current_state.started_at,
            last_successful_scan_at=next_cursor,
            created_at=current_state.created_at,
            updated_at=finish_time,
        )

    def fail_scan_run(
        self,
        run_id: int,
        *,
        failed_at: datetime,
        error_code: str,
        counters: IngestionRunCounters,
        cursor_before: datetime | None = None,
    ) -> None:
        """Finalize one running scan as failed without moving the cursor.

        Args:
            run_id: Running ingestion record to fail.
            failed_at: Time at which the scan stopped.
            error_code: Bounded non-sensitive failure category.
            counters: Partial non-sensitive counters observed before failure.
            cursor_before: Optional cursor loaded before the failed operation.

        Raises:
            IngestionStoreError: If inputs are invalid or the run is not active.
        """

        normalized_run_id = _validated_run_id(
            run_id
        )
        failure_time = _as_utc(failed_at)
        normalized_error_code = (
            _normalized_error_code(error_code)
        )
        counter_values = _validated_run_counters(
            counters
        )
        previous_cursor = (
            _as_utc(cursor_before)
            if cursor_before is not None
            else None
        )
        lease_checked_at, _ = self._lease_window()

        with self.database.connect() as connection:
            run_row = connection.execute(
                """
                SELECT started_at, status, lease_expires_at
                FROM ingestion_runs
                WHERE id = ?
                  AND account_id = ?
                """,
                (
                    normalized_run_id,
                    self.ACCOUNT_ID,
                ),
            ).fetchone()

            if (
                run_row is None
                or run_row["status"] != "running"
                or run_row["lease_expires_at"] is None
                or _from_iso(run_row["lease_expires_at"])
                    <= lease_checked_at
            ):
                raise IngestionLeaseLostError(
                    "The ingestion lease is no longer active"
                )

            if failure_time < _from_iso(
                run_row["started_at"]
            ):
                raise IngestionStoreError(
                    "An ingestion run cannot finish "
                    "before it starts"
                )

            cursor = connection.execute(
                """
                UPDATE ingestion_runs
                SET status = 'failed',
                    finished_at = ?,
                    cursor_before = ?,
                    cursor_after = NULL,
                    listed_count = ?,
                    discovered_count = ?,
                    already_known_count = ?,
                    ignored_before_start_count = ?,
                    newsletter_count = ?,
                    review_count = ?,
                    not_newsletter_count = ?,
                    purged_content_count = ?,
                    error_code = ?,
                    lease_heartbeat_at = NULL,
                    lease_expires_at = NULL
                WHERE id = ?
                  AND account_id = ?
                  AND status = 'running'
                  AND julianday(lease_expires_at) > julianday(?)
                """,
                (
                    failure_time.isoformat(),
                    (
                        previous_cursor.isoformat()
                        if previous_cursor is not None
                        else None
                    ),
                    *counter_values,
                    normalized_error_code,
                    normalized_run_id,
                    self.ACCOUNT_ID,
                    lease_checked_at.isoformat(),
                ),
            )

            if cursor.rowcount != 1:
                raise IngestionLeaseLostError(
                    "The ingestion lease is no longer active"
                )

    def get_or_create_state(
        self,
        started_at: datetime | None = None,
    ) -> IngestionState:
        """Create the initial cursor once, or return it unchanged.

        Args:
            started_at: Optional timezone-aware initial cursor. The current UTC
                time is used when omitted.

        Returns:
            The existing or newly created ingestion state.

        Raises:
            IngestionStoreError: If the timestamp is naive, no authenticated
                account exists, or the state cannot be loaded after insertion.
        """

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
        """Advance the cursor after a complete successful scan.

        Args:
            completed_at: Optional timezone-aware completion timestamp.

        Returns:
            The ingestion state containing the advanced cursor.

        Raises:
            IngestionStoreError: If state is absent, the timestamp is naive, or
                the new cursor precedes the start or previous successful scan.
        """

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

        Args:
            gmail_message_id: External Gmail message identifier.
            gmail_thread_id: External Gmail thread identifier.
            internal_date_ms: Gmail internal timestamp in epoch milliseconds.
            discovered_at: Optional timezone-aware discovery timestamp.

        Returns:
            ``True`` when inserted and ``False`` when already known.

        Raises:
            IngestionStoreError: If input is invalid, ingestion is not
                initialized, or SQLite rejects the row.
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
        """Load one previously registered Gmail message.

        Args:
            gmail_message_id: External Gmail message identifier.

        Returns:
            The stored message, or ``None`` when it is unknown.

        Raises:
            IngestionStoreError: If the identifier is empty.
        """

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
        """Return whether a Gmail message was already discovered.

        Args:
            gmail_message_id: External Gmail message identifier.

        Returns:
            Whether a matching message row exists.
        """

        return self.load_message(
            gmail_message_id
        ) is not None

    def _update_message_processing_status(
        self,
        gmail_message_id: str,
        *,
        status: str,
        changed_at: datetime,
        processed_at: datetime | None,
        last_error_code: str | None,
    ) -> StoredGmailMessage:
        """Persist one internal message processing transition.

        Args:
            gmail_message_id: External Gmail message identifier.
            status: Database status assigned to the message.
            changed_at: Timestamp recorded as the latest update.
            processed_at: Optional successful completion timestamp.
            last_error_code: Optional safe failure code.

        Returns:
            The updated stored message.

        Raises:
            IngestionStoreError: If the identifier is invalid or no registered
                message can be updated and reloaded.
        """

        message_id = _required_identifier(
            gmail_message_id,
            "gmail_message_id",
        )

        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE gmail_messages
                SET status = ?,
                    updated_at = ?,
                    processed_at = ?,
                    last_error_code = ?
                WHERE account_id = ?
                  AND gmail_message_id = ?
                """,
                (
                    status,
                    changed_at.isoformat(),
                    (
                        processed_at.isoformat()
                        if processed_at is not None
                        else None
                    ),
                    last_error_code,
                    self.ACCOUNT_ID,
                    message_id,
                ),
            )

            if cursor.rowcount != 1:
                raise IngestionStoreError(
                    "The Gmail message has not "
                    "been registered"
                )

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
            raise IngestionStoreError(
                "The Gmail message could not "
                "be loaded"
            )

        return _message_from_row(
            row
        )

    def mark_message_processing(
        self,
        gmail_message_id: str,
        *,
        started_at: datetime | None = None,
    ) -> StoredGmailMessage:
        """Mark a registered message as currently processing.

        Args:
            gmail_message_id: External Gmail message identifier.
            started_at: Optional timezone-aware processing timestamp.

        Returns:
            The updated stored message.
        """

        processing_time = _as_utc(
            started_at
            or datetime.now(timezone.utc)
        )

        return self._update_message_processing_status(
            gmail_message_id,
            status="processing",
            changed_at=processing_time,
            processed_at=None,
            last_error_code=None,
        )

    def mark_message_processed(
        self,
        gmail_message_id: str,
        *,
        completed_at: datetime | None = None,
    ) -> StoredGmailMessage:
        """Mark a registered message as successfully processed.

        Args:
            gmail_message_id: External Gmail message identifier.
            completed_at: Optional timezone-aware completion timestamp.

        Returns:
            The updated stored message.
        """

        completion_time = _as_utc(
            completed_at
            or datetime.now(timezone.utc)
        )

        return self._update_message_processing_status(
            gmail_message_id,
            status="processed",
            changed_at=completion_time,
            processed_at=completion_time,
            last_error_code=None,
        )

    def mark_message_failed(
        self,
        gmail_message_id: str,
        *,
        error_code: str,
        failed_at: datetime | None = None,
    ) -> StoredGmailMessage:
        """Mark processing as failed without storing sensitive details.

        Args:
            gmail_message_id: External Gmail message identifier.
            error_code: Non-empty safe code of at most 100 characters.
            failed_at: Optional timezone-aware failure timestamp.

        Returns:
            The updated stored message.

        Raises:
            IngestionStoreError: If the error code or timestamp is invalid, or
                the message is not registered.
        """

        normalized_error_code = (
            _normalized_error_code(
                error_code
            )
        )

        failure_time = _as_utc(
            failed_at
            or datetime.now(timezone.utc)
        )

        return self._update_message_processing_status(
            gmail_message_id,
            status="failed",
            changed_at=failure_time,
            processed_at=None,
            last_error_code=(
                normalized_error_code
            ),
        )

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
        """Save a classification and update message state atomically.

        Args:
            gmail_message_id: External Gmail message identifier.
            verdict: Supported normalized newsletter outcome.
            score: Non-negative classifier score.
            reasons: Ordered non-empty reason codes.
            classifier_version: Non-empty classifier version identifier.
            classified_at: Optional timezone-aware classification timestamp.

        Returns:
            The persisted classification joined with its Gmail identifier.

        Raises:
            IngestionStoreError: If data is invalid, the message is unknown, or
                the saved record cannot be loaded.

        Notes:
            Reclassifying a message as ``review`` or ``not_newsletter`` removes
            any previously retained body in the same transaction so content is
            never associated with a non-newsletter verdict.
        """

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

            if normalized_verdict != "newsletter":
                connection.execute(
                    """
                    DELETE FROM message_contents
                    WHERE gmail_message_row_id = ?
                    """,
                    (message_row_id,),
                )

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
        """Load the latest classification for one message.

        Args:
            gmail_message_id: External Gmail message identifier.

        Returns:
            The stored classification, or ``None`` when no decision exists.

        Raises:
            IngestionStoreError: If the identifier or stored reason data is
                invalid.
        """

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
