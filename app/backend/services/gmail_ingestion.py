"""Coordinate one incremental Gmail discovery and processing scan.

The ingestion service derives a Gmail ``after:`` query from the durable cursor,
registers unseen messages, resolves newsletter classifications, processes
content for newsletters, records a safe operational outcome, and advances the
cursor only after a complete scan.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter

from app.backend.bd.ingestion_store import (
    IngestionRunAlreadyActiveError,
    IngestionRunCounters,
    IngestionStore,
    IngestionStoreError,
)
from app.backend.services.classifier import (
    NewsletterVerdict,
)
from app.backend.services.message_content_processing import (
    MessageContentProcessingError,
    MessageContentProcessingService,
)
from app.backend.services.newsletter_classification import (
    CLASSIFIER_VERSION,
    NewsletterClassificationService,
)
from app.tools.gmail.gmail_client import (
    GmailClient,
    GmailClientError,
    GmailListingLimitReached,
)
from app.tools.logger import (
    AppLogger,
    is_error_logged,
    mark_error_logged,
)


class GmailIngestionError(RuntimeError):
    """Raised when an incremental Gmail scan fails."""


class GmailIngestionAlreadyRunningError(GmailIngestionError):
    """Raised when another process owns the durable ingestion lease."""


@dataclass(frozen=True, slots=True)
class IngestionScanResult:
    """Summarize cursor movement and message counts for a completed scan."""

    scan_started_at: datetime
    cursor_before: datetime
    cursor_after: datetime
    gmail_query: str
    listed_count: int
    discovered_count: int
    already_known_count: int
    ignored_before_start_count: int
    newsletter_count: int
    review_count: int
    not_newsletter_count: int


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp.

    Returns:
        The current time in UTC.
    """

    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Validate an ingestion timestamp and normalize it to UTC.

    Args:
        value: Timestamp returned by the configured ingestion clock.

    Returns:
        The equivalent timezone-aware UTC timestamp.

    Raises:
        GmailIngestionError: If ``value`` is not timezone-aware.
    """

    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise GmailIngestionError(
            "The ingestion clock must return "
            "a timezone-aware datetime"
        )

    return value.astimezone(timezone.utc)


class GmailIngestionService:
    """Coordinate incremental Gmail discovery, classification, and content.

    Attributes:
        gmail_client: High-level Gmail API adapter.
        store: Durable cursor and message-state repository.
        classification_service: Stored-rule and heuristic decision service.
        content_processing_service: MIME decoding, parsing, and persistence
            pipeline used for messages classified as newsletters.
        clock: Injectable timezone-aware scan clock.
    """

    CURSOR_OVERLAP_SECONDS = 1

    def __init__(
        self,
        gmail_client: GmailClient,
        store: IngestionStore,
        classification_service: (
            NewsletterClassificationService
        ),
        content_processing_service: (
            MessageContentProcessingService
        ),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Bind the Gmail, persistence, classification, and content services.

        Args:
            gmail_client: High-level Gmail API adapter.
            store: Ingestion-state repository.
            classification_service: Newsletter decision service.
            content_processing_service: Readable-content pipeline.
            clock: Callable that supplies the scan timestamp.
        """

        self.gmail_client = gmail_client
        self.store = store
        self.classification_service = (
            classification_service
        )
        self.content_processing_service = (
            content_processing_service
        )
        self.clock = clock
        self.logger = AppLogger(
            "gmail_ingestion.log"
        )

    @classmethod
    def _build_after_query(
        cls,
        cursor: datetime,
    ) -> str:
        """Build an overlapping Gmail epoch query from the cursor.

        Args:
            cursor: Last durable scan cursor.

        Returns:
            A Gmail ``after:`` query one second before the cursor, bounded at
            the Unix epoch.

        Raises:
            GmailIngestionError: If the cursor is timezone-naive.
        """

        normalized_cursor = _as_utc(cursor)

        epoch_seconds = max(
            0,
            int(normalized_cursor.timestamp())
            - cls.CURSOR_OVERLAP_SECONDS,
        )

        return f"after:{epoch_seconds}"

    def _process_newsletter_content(
        self,
        gmail_message_id: str,
        *,
        user_id: str,
        processing_at: datetime,
    ) -> None:
        """Process one newsletter with retryable state transitions.

        Args:
            gmail_message_id: External Gmail message identifier.
            user_id: Gmail user identifier used for content retrieval.
            processing_at: Timestamp recorded for all processing transitions.

        Raises:
            MessageContentProcessingError: If content processing fails after the
                message has been marked with a safe failure code.
        """

        operation_started_at = perf_counter()
        self.logger.debug(
            "Newsletter content processing started: message_id=%s",
            gmail_message_id,
        )

        self.store.mark_message_processing(
            gmail_message_id,
            started_at=processing_at,
        )

        try:
            (
                self.content_processing_service
                .process_message(
                    gmail_message_id,
                    user_id=user_id,
                    extracted_at=processing_at,
                )
            )
        except MessageContentProcessingError:
            self.store.mark_message_failed(
                gmail_message_id,
                error_code=(
                    "content_processing_failed"
                ),
                failed_at=processing_at,
            )
            raise

        self.store.mark_message_processed(
            gmail_message_id,
            completed_at=processing_at,
        )
        self.logger.debug(
            "Newsletter content processing completed: message_id=%s "
            "duration_ms=%.2f",
            gmail_message_id,
            (perf_counter() - operation_started_at) * 1_000,
        )

    def scan(
        self,
        user_id: str = "me",
        total_limit: int = 1_000,
        page_size: int = 100,
        label_ids: Sequence[str] | None = None,
    ) -> IngestionScanResult:
        """Discover, classify, and persist Gmail messages.

        Args:
            user_id: Gmail user identifier used for all API calls.
            total_limit: Maximum references accepted for a complete scan.
            page_size: Requested Gmail page size.
            label_ids: Optional Gmail labels used to filter discovery.

        Returns:
            Cursor movement, query, and classification counts for the completed
            scan.

        Raises:
            GmailIngestionError: If any discovery, validation, persistence,
                classification, or content-processing stage fails. The durable
                cursor is not advanced on failure.

        Notes:
            Each authenticated attempt creates an ``ingestion_runs`` record.
            Successful finalization and cursor advancement are atomic; failed
            finalization records only bounded counters and a safe error code.
        """

        operation_started_at = perf_counter()
        scan_started_at = _as_utc(
            self.clock()
        )
        label_count = (
            len(label_ids)
            if label_ids is not None
            else 0
        )
        scan_run_id: int | None = None
        cursor_before: datetime | None = None
        purged_count = 0
        listed_count = 0
        discovered_count = 0
        already_known_count = 0
        ignored_before_start_count = 0
        newsletter_count = 0
        review_count = 0
        not_newsletter_count = 0

        def current_counters() -> IngestionRunCounters:
            """Build the durable non-sensitive counters observed so far.

            Returns:
                A snapshot suitable for either successful or failed run
                finalization.
            """

            return IngestionRunCounters(
                listed_count=listed_count,
                discovered_count=discovered_count,
                already_known_count=(
                    already_known_count
                ),
                ignored_before_start_count=(
                    ignored_before_start_count
                ),
                newsletter_count=newsletter_count,
                review_count=review_count,
                not_newsletter_count=(
                    not_newsletter_count
                ),
                purged_content_count=(
                    purged_count
                ),
            )

        def record_failed_run(
            error_code: str,
        ) -> None:
            """Best-effort finalize the active run without masking failure.

            Args:
                error_code: Non-sensitive category for the primary failure.

            Notes:
                A secondary history-write failure is logged with its exception
                type only and never replaces the scan exception owned by the
                caller's active error handler.
            """

            if scan_run_id is None:
                return

            try:
                failure_time = _as_utc(
                    self.clock()
                )
                self.store.fail_scan_run(
                    scan_run_id,
                    failed_at=failure_time,
                    error_code=error_code,
                    counters=current_counters(),
                    cursor_before=cursor_before,
                )
                self.logger.debug(
                    "Ingestion run history finalized: "
                    "run_id=%d status=failed error_code=%s",
                    scan_run_id,
                    error_code,
                )
            except Exception as history_error:
                self.logger.warning(
                    "Ingestion run history finalization failed: "
                    "failure_type=%s",
                    type(history_error).__name__,
                )

        def renew_active_lease() -> None:
            """Refresh the current run lease around potentially slow work."""

            if scan_run_id is not None:
                self.store.renew_scan_run(scan_run_id)

        self.logger.info(
            "Gmail ingestion scan started: total_limit=%d page_size=%d "
            "label_count=%d",
            total_limit,
            page_size,
            label_count,
        )

        try:
            scan_run_id = self.store.start_scan_run(
                started_at=scan_started_at
            )
            self.logger.debug(
                "Ingestion run lease acquired: run_id=%d",
                scan_run_id,
            )
            purged_count = (
                self.content_processing_service
                .purge_expired(
                    expired_at=scan_started_at
                )
            )
            self.logger.debug(
                "Expired content purge completed: deleted_count=%d",
                purged_count,
            )
            renew_active_lease()

            state = self.store.get_or_create_state(
                started_at=scan_started_at
            )

            cursor_before = (
                state.last_successful_scan_at
                or state.started_at
            )

            gmail_query = self._build_after_query(
                cursor_before
            )
            self.logger.debug(
                "Ingestion cursor loaded: cursor_before=%s query=%s",
                cursor_before.isoformat(),
                gmail_query,
            )

            references = (
                self.gmail_client
                .list_message_references(
                    user_id=user_id,
                    total_limit=total_limit,
                    page_size=page_size,
                    query=gmail_query,
                    label_ids=label_ids,
                    include_spam_trash=False,
                    require_complete=True,
                )
            )
            self.logger.info(
                "Gmail message discovery completed: listed_count=%d",
                len(references),
            )
            listed_count = len(references)
            renew_active_lease()

            application_start_ms = int(
                state.started_at.timestamp() * 1_000
            )

            for reference in references:
                renew_active_lease()
                stored_classification = (
                    self.store
                    .load_message_classification(
                        reference.message_id
                    )
                )

                if stored_classification is not None:
                    already_known_count += 1
                    self.logger.debug(
                        "Reusing stored classification: message_id=%s "
                        "verdict=%s",
                        reference.message_id,
                        stored_classification.verdict,
                    )

                    if (
                        stored_classification.verdict
                        == "newsletter"
                    ):
                        stored_message = (
                            self.store.load_message(
                                reference.message_id
                            )
                        )

                        if stored_message is None:
                            raise IngestionStoreError(
                                "Stored classification has "
                                "no registered Gmail message"
                            )

                        if (
                            stored_message.status
                            != "processed"
                        ):
                            self._process_newsletter_content(
                                reference.message_id,
                                user_id=user_id,
                                processing_at=(
                                    scan_started_at
                                ),
                            )

                    continue

                message_was_known = (
                    self.store.is_message_known(
                        reference.message_id
                    )
                )

                if message_was_known:
                    already_known_count += 1

                metadata = (
                    self.gmail_client
                    .get_message_metadata(
                        message_id=(
                            reference.message_id
                        ),
                        user_id=user_id,
                    )
                )

                if (
                    metadata.message_id
                    != reference.message_id
                    or metadata.thread_id
                    != reference.thread_id
                ):
                    raise GmailIngestionError(
                        "Gmail returned inconsistent "
                        "message metadata"
                    )

                if (
                    not message_was_known
                    and metadata.internal_date_ms
                    < application_start_ms
                ):
                    ignored_before_start_count += 1
                    continue

                if not message_was_known:
                    was_inserted = (
                        self.store.register_message(
                            gmail_message_id=(
                                metadata.message_id
                            ),
                            gmail_thread_id=(
                                metadata.thread_id
                            ),
                            internal_date_ms=(
                                metadata.internal_date_ms
                            ),
                            discovered_at=(
                                scan_started_at
                            ),
                        )
                    )

                    if was_inserted:
                        discovered_count += 1
                    else:
                        already_known_count += 1

                classification = (
                    self.classification_service
                    .classify(metadata)
                )
                self.logger.debug(
                    "Message classification completed: message_id=%s "
                    "verdict=%s score=%d reason_count=%d",
                    metadata.message_id,
                    classification.verdict.value,
                    classification.score,
                    len(classification.reasons),
                )

                self.store.save_message_classification(
                    metadata.message_id,
                    verdict=(
                        classification.verdict.value
                    ),
                    score=classification.score,
                    reasons=classification.reasons,
                    classifier_version=(
                        CLASSIFIER_VERSION
                    ),
                    classified_at=scan_started_at,
                )

                if (
                    classification.verdict
                    is NewsletterVerdict.NEWSLETTER
                ):
                    newsletter_count += 1

                    self._process_newsletter_content(
                        metadata.message_id,
                        user_id=user_id,
                        processing_at=(
                            scan_started_at
                        ),
                    )
                elif (
                    classification.verdict
                    is NewsletterVerdict.REVIEW
                ):
                    review_count += 1
                else:
                    not_newsletter_count += 1

            result = IngestionScanResult(
                scan_started_at=scan_started_at,
                cursor_before=cursor_before,
                cursor_after=scan_started_at,
                gmail_query=gmail_query,
                listed_count=len(references),
                discovered_count=discovered_count,
                already_known_count=(
                    already_known_count
                ),
                ignored_before_start_count=(
                    ignored_before_start_count
                ),
                newsletter_count=newsletter_count,
                review_count=review_count,
                not_newsletter_count=(
                    not_newsletter_count
                ),
            )
            renew_active_lease()
            scan_finished_at = _as_utc(
                self.clock()
            )
            self.store.complete_scan_run(
                scan_run_id,
                finished_at=scan_finished_at,
                cursor_before=cursor_before,
                cursor_after=scan_started_at,
                counters=current_counters(),
            )
            self.logger.debug(
                "Ingestion cursor and run finalized: "
                "cursor_after=%s run_id=%d",
                scan_started_at.isoformat(),
                scan_run_id,
            )
            self.logger.info(
                "Gmail ingestion scan completed: listed=%d discovered=%d "
                "already_known=%d ignored_before_start=%d newsletter=%d "
                "review=%d not_newsletter=%d duration_ms=%.2f",
                result.listed_count,
                result.discovered_count,
                result.already_known_count,
                result.ignored_before_start_count,
                result.newsletter_count,
                result.review_count,
                result.not_newsletter_count,
                (perf_counter() - operation_started_at) * 1_000,
            )
            return result
        except IngestionRunAlreadyActiveError as exc:
            self.logger.warning(
                "Gmail ingestion scan skipped because another process "
                "owns the active lease"
            )
            raise GmailIngestionAlreadyRunningError(
                "An ingestion scan is already running"
            ) from exc
        except GmailIngestionError as exc:
            record_failed_run(
                "ingestion_validation_failed"
            )
            self.logger.debug(
                "Gmail ingestion scan aborted: duration_ms=%.2f",
                (perf_counter() - operation_started_at) * 1_000,
            )
            self.logger.error(
                "Gmail ingestion scan failed: "
                "inconsistent metadata"
            )
            mark_error_logged(exc)
            raise
        except GmailListingLimitReached as exc:
            record_failed_run(
                "result_limit_reached"
            )
            self.logger.debug(
                "Gmail ingestion scan aborted: duration_ms=%.2f",
                (perf_counter() - operation_started_at) * 1_000,
            )
            self.logger.error(
                "Gmail ingestion scan failed: "
                "result limit reached"
            )
            raise mark_error_logged(
                GmailIngestionError(
                    "Gmail ingestion result limit reached"
                )
            ) from exc
        except (
            GmailClientError,
            IngestionStoreError,
            MessageContentProcessingError,
        ) as exc:
            if isinstance(exc, GmailClientError):
                error_code = "gmail_client_failed"
            elif isinstance(
                exc,
                IngestionStoreError,
            ):
                error_code = "persistence_failed"
            else:
                error_code = (
                    "content_processing_failed"
                )

            record_failed_run(error_code)
            self.logger.debug(
                "Gmail ingestion scan aborted: duration_ms=%.2f",
                (perf_counter() - operation_started_at) * 1_000,
            )
            if is_error_logged(exc):
                self.logger.debug(
                    "Gmail ingestion received an already logged failure: %s",
                    type(exc).__name__,
                )
            else:
                self.logger.error(
                    "Gmail ingestion scan failed: "
                    f"{type(exc).__name__}"
                )
            raise mark_error_logged(
                GmailIngestionError(
                    "Gmail ingestion scan failed"
                )
            ) from exc
        except Exception as exc:
            record_failed_run(
                "unexpected_failure"
            )
            self.logger.debug(
                "Gmail ingestion scan aborted: duration_ms=%.2f",
                (perf_counter() - operation_started_at) * 1_000,
            )
            self.logger.error(
                "Gmail ingestion scan failed: "
                f"{type(exc).__name__}"
            )
            raise mark_error_logged(
                GmailIngestionError(
                    "Gmail ingestion scan failed"
                )
            ) from exc
