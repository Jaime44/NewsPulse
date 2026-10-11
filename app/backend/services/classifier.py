"""Classify Gmail metadata with deterministic, explainable newsletter signals.

The classifier scores configured labels and senders together with standard
mailing-list headers, then returns an immutable verdict consumed by the stored
rule resolver and ingestion pipeline.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from email.utils import parseaddr
from enum import Enum

from app.tools.gmail.gmail_client import (
    GmailMessageMetadata,
)


class NewsletterClassifierError(ValueError):
    """Raised when newsletter classifier input is invalid."""


class NewsletterVerdict(str, Enum):
    """Enumerate the three outcomes produced by newsletter classification."""

    NEWSLETTER = "newsletter"
    REVIEW = "review"
    NOT_NEWSLETTER = "not_newsletter"


@dataclass(frozen=True, slots=True)
class NewsletterClassification:
    """Describe a classifier verdict, its score, and contributing reason codes."""

    verdict: NewsletterVerdict
    score: int
    reasons: tuple[str, ...]

    @property
    def is_newsletter(self) -> bool:
        """Return whether the verdict conclusively identifies a newsletter.

        Returns:
            ``True`` only for ``NewsletterVerdict.NEWSLETTER``.
        """

        return (
            self.verdict
            is NewsletterVerdict.NEWSLETTER
        )

    @property
    def requires_review(self) -> bool:
        """Return whether the verdict requires a manual decision.

        Returns:
            ``True`` only for ``NewsletterVerdict.REVIEW``.
        """

        return (
            self.verdict
            is NewsletterVerdict.REVIEW
        )


def _normalize_config_values(
    values: Sequence[str],
    field_name: str,
    *,
    case_sensitive: bool,
) -> frozenset[str]:
    """Validate and normalize configured classifier values.

    Args:
        values: Label or sender values supplied at construction time.
        field_name: Configuration field used in validation errors.
        case_sensitive: Whether normalized values preserve case.

    Returns:
        A deduplicated immutable set of stripped values.

    Raises:
        NewsletterClassifierError: If the collection contains invalid values.
    """

    if isinstance(values, (str, bytes)):
        raise NewsletterClassifierError(
            f"{field_name} must be a sequence"
        )

    normalized_values: set[str] = set()

    for value in values:
        if (
            not isinstance(value, str)
            or not value.strip()
        ):
            raise NewsletterClassifierError(
                f"{field_name} cannot contain "
                "empty values"
            )

        normalized_value = value.strip()

        if not case_sensitive:
            normalized_value = (
                normalized_value.casefold()
            )

        normalized_values.add(
            normalized_value
        )

    return frozenset(normalized_values)


class NewsletterClassifier:
    """Classify Gmail metadata using weighted and explainable signals.

    Attributes:
        newsletter_label_ids: Case-sensitive Gmail labels treated as strong
            newsletter evidence.
        known_senders: Case-folded sender addresses treated as strong evidence.
    """

    NEWSLETTER_SCORE = 5
    REVIEW_SCORE = 2

    def __init__(
        self,
        newsletter_label_ids: Sequence[str] = (),
        known_senders: Sequence[str] = (),
    ) -> None:
        """Configure strong label and sender signals.

        Args:
            newsletter_label_ids: Gmail label IDs that identify newsletters.
            known_senders: Sender addresses that identify newsletters.

        Raises:
            NewsletterClassifierError: If either sequence contains invalid data.
        """

        self.newsletter_label_ids = (
            _normalize_config_values(
                newsletter_label_ids,
                "newsletter_label_ids",
                case_sensitive=True,
            )
        )

        self.known_senders = (
            _normalize_config_values(
                known_senders,
                "known_senders",
                case_sensitive=False,
            )
        )

    def classify(
        self,
        metadata: GmailMessageMetadata,
    ) -> NewsletterClassification:
        """Score one Gmail metadata record and return its newsletter verdict.

        Args:
            metadata: Validated headers, labels, and identifiers from Gmail.

        Returns:
            The verdict, aggregate score, and ordered contributing reasons.

        Raises:
            NewsletterClassifierError: If ``metadata`` has the wrong type.
        """

        if not isinstance(
            metadata,
            GmailMessageMetadata,
        ):
            raise NewsletterClassifierError(
                "metadata must be GmailMessageMetadata"
            )

        score = 0
        reasons: list[str] = []

        message_labels = frozenset(
            metadata.label_ids
        )

        if (
            self.newsletter_label_ids
            & message_labels
        ):
            score += 5
            reasons.append(
                "configured_gmail_label"
            )

        sender_address = parseaddr(
            metadata.sender or ""
        )[1].strip().casefold()

        if (
            sender_address
            and sender_address
            in self.known_senders
        ):
            score += 5
            reasons.append(
                "configured_sender"
            )

        if metadata.list_id:
            score += 3
            reasons.append(
                "list_id_header"
            )

        if metadata.list_unsubscribe:
            score += 3
            reasons.append(
                "list_unsubscribe_header"
            )

        precedence = (
            metadata.precedence
            or ""
        ).strip().casefold()

        if precedence in {"bulk", "list"}:
            score += 2
            reasons.append(
                "bulk_precedence"
            )

        auto_submitted = (
            metadata.auto_submitted
            or ""
        ).strip().casefold()

        if (
            auto_submitted
            and auto_submitted != "no"
        ):
            score += 1
            reasons.append(
                "auto_submitted"
            )

        if score >= self.NEWSLETTER_SCORE:
            verdict = (
                NewsletterVerdict.NEWSLETTER
            )
        elif score >= self.REVIEW_SCORE:
            verdict = NewsletterVerdict.REVIEW
        else:
            verdict = (
                NewsletterVerdict.NOT_NEWSLETTER
            )

        return NewsletterClassification(
            verdict=verdict,
            score=score,
            reasons=tuple(reasons),
        )
