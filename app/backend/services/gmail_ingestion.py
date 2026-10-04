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
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
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
    """Coordinate incremental Gmail discovery and classification."""

    CURSOR_OVERLAP_SECONDS = 1

    def __init__(
        self,
        gmail_client: GmailClient,
        store: IngestionStore,
        classification_service: (
            NewsletterClassificationService
        ),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.gmail_client = gmail_client
        self.store = store
        self.classification_service = (
            classification_service
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
        normalized_cursor = _as_utc(cursor)

        epoch_seconds = max(
            0,
            int(normalized_cursor.timestamp())
            - cls.CURSOR_OVERLAP_SECONDS,
        )

        return f"after:{epoch_seconds}"

    def scan(
        self,
        user_id: str = "me",
        total_limit: int = 1_000,
        page_size: int = 100,
        label_ids: Sequence[str] | None = None,
    ) -> IngestionScanResult:
        """Discover, classify and persist Gmail messages."""

        scan_started_at = _as_utc(
            self.clock()
        )

        try:
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