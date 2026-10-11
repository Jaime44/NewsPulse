"""Coordinate one incremental Gmail discovery and processing scan.

The ingestion service derives a Gmail ``after:`` query from the durable cursor,
registers unseen messages, resolves newsletter classifications, processes
content for newsletters, and advances the cursor only after a complete scan.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from app.backend.bd.ingestion_store import (
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
from app.tools.logger import AppLogger


class GmailIngestionError(RuntimeError):
    """Raised when an incremental Gmail scan fails."""


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
        """

        scan_started_at = _as_utc(
            self.clock()
        )

        try:
            (
                self.content_processing_service
                .purge_expired(
                    expired_at=scan_started_at
                )
            )

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

            application_start_ms = int(
                state.started_at.timestamp() * 1_000
            )

            discovered_count = 0
            already_known_count = 0
            ignored_before_start_count = 0
            newsletter_count = 0
            review_count = 0
            not_newsletter_count = 0

            for reference in references:
                stored_classification = (
                    self.store
                    .load_message_classification(
                        reference.message_id
                    )
                )

                if stored_classification is not None:
                    already_known_count += 1

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

            self.store.mark_scan_successful(
                completed_at=scan_started_at
            )

            return IngestionScanResult(
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
        except GmailIngestionError:
            self.logger.error(
                "Gmail ingestion scan failed: "
                "inconsistent metadata"
            )
            raise
        except GmailListingLimitReached as exc:
            self.logger.error(
                "Gmail ingestion scan failed: "
                "result limit reached"
            )
            raise GmailIngestionError(
                "Gmail ingestion result limit reached"
            ) from exc
        except (
            GmailClientError,
            IngestionStoreError,
            MessageContentProcessingError,
        ) as exc:
            self.logger.error(
                "Gmail ingestion scan failed: "
                f"{type(exc).__name__}"
            )
            raise GmailIngestionError(
                "Gmail ingestion scan failed"
            ) from exc
        except Exception as exc:
            self.logger.error(
                "Gmail ingestion scan failed: "
                f"{type(exc).__name__}"
            )
            raise GmailIngestionError(
                "Gmail ingestion scan failed"
            ) from exc
