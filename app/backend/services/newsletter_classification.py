from __future__ import annotations

from email.utils import parseaddr

from app.backend.bd.newsletter_source_store import (
    NewsletterSource,
    NewsletterSourceStore,
    NewsletterSourceStoreError,
    normalize_source_value,
)
from app.backend.services.classifier import (
    NewsletterClassification,
    NewsletterClassifier,
    NewsletterVerdict,
)
from app.tools.gmail.gmail_client import (
    GmailMessageMetadata,
)

CLASSIFIER_VERSION = "rules-v1"

ORIGIN_PRIORITY = {
    "manual": 0,
    "confirmed": 1,
}

DECISION_PRIORITY = {
    "exclude": 0,
    "include": 1,
}

SOURCE_PRIORITY = {
    "sender": 0,
    "list_id": 1,
    "gmail_label": 2,
    "domain": 3,
}


def _try_normalize_source(
    source_type: str,
    source_value: str | None,
) -> str | None:
    """Normalize message data without failing on malformed headers."""

    if (
        not isinstance(source_value, str)
        or not source_value.strip()
    ):
        return None

    try:
        return normalize_source_value(
            source_type,
            source_value,
        )
    except NewsletterSourceStoreError:
        return None


def _message_source_values(
    metadata: GmailMessageMetadata,
) -> dict[str, frozenset[str]]:
    """Extract normalized source candidates from Gmail metadata."""

    candidates: dict[str, set[str]] = {
        "sender": set(),
        "list_id": set(),
        "gmail_label": set(),
        "domain": set(),
    }

    sender_address = parseaddr(
        metadata.sender or ""
    )[1]

    normalized_sender = _try_normalize_source(
        "sender",
        sender_address,
    )

    if normalized_sender is not None:
        candidates["sender"].add(
            normalized_sender
        )

        sender_domain = normalized_sender.rpartition(
            "@"
        )[2]

        normalized_domain = _try_normalize_source(
            "domain",
            sender_domain,
        )

        if normalized_domain is not None:
            candidates["domain"].add(
                normalized_domain
            )

    normalized_list_id = _try_normalize_source(
        "list_id",
        metadata.list_id,
    )

    if normalized_list_id is not None:
        candidates["list_id"].add(
            normalized_list_id
        )

    for label_id in metadata.label_ids:
        normalized_label = _try_normalize_source(
            "gmail_label",
            label_id,
        )

        if normalized_label is not None:
            candidates["gmail_label"].add(
                normalized_label
            )

    return {
        source_type: frozenset(values)
        for source_type, values
        in candidates.items()
    }


def _rule_matches(
    source: NewsletterSource,
    candidates: dict[str, frozenset[str]],
) -> bool:
    return (
        source.source_value
        in candidates.get(
            source.source_type,
            frozenset(),
        )
    )


def _rule_priority(
    source: NewsletterSource,
) -> tuple[int, int, int]:
    return (
        ORIGIN_PRIORITY[source.origin],
        DECISION_PRIORITY[source.decision],
        SOURCE_PRIORITY[source.source_type],
    )


class NewsletterClassificationService:
    """Resolve stored source rules before heuristic classification."""

    def __init__(
        self,
        source_store: NewsletterSourceStore,
        classifier: NewsletterClassifier,
    ) -> None:
        self.source_store = source_store
        self.classifier = classifier

    def classify(
        self,
        metadata: GmailMessageMetadata,
    ) -> NewsletterClassification:
        """Return the final newsletter decision for one message."""

        heuristic_result = self.classifier.classify(
            metadata
        )

        candidates = _message_source_values(
            metadata
        )

        eligible_sources = (
            source
            for source
            in self.source_store.list_active_sources()
            if source.origin in ORIGIN_PRIORITY
        )

        matching_sources = sorted(
            (
                source
                for source in eligible_sources
                if _rule_matches(
                    source,
                    candidates,
                )
            ),
            key=_rule_priority,
        )

        for source in matching_sources:
            matched_source = (
                self.source_store.mark_source_matched(
                    source.source_type,
                    source.source_value,
                )
            )

            if matched_source is None:
                continue

            reason = (
                "stored_source:"
                f"{matched_source.origin}:"
                f"{matched_source.decision}:"
                f"{matched_source.source_type}"
            )

            if matched_source.decision == "exclude":
                return NewsletterClassification(
                    verdict=(
                        NewsletterVerdict
                        .NOT_NEWSLETTER
                    ),
                    score=0,
                    reasons=(reason,),
                )

            return NewsletterClassification(
                verdict=NewsletterVerdict.NEWSLETTER,
                score=(
                    NewsletterClassifier
                    .NEWSLETTER_SCORE
                ),
                reasons=(reason,),
            )

        return heuristic_result