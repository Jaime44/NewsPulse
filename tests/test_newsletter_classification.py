from __future__ import annotations

import unittest

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

from app.backend.bd.newsletter_source_store import (
    NewsletterSource,
    NewsletterSourceStore,
)
from app.backend.services.classifier import (
    NewsletterClassifier,
    NewsletterVerdict,
)
from app.backend.services.newsletter_classification import (
    NewsletterClassificationService,
)
from app.tools.gmail.gmail_client import (
    GmailMessageMetadata,
)


NOW = datetime(
    2026,
    9,
    14,
    12,
    0,
    tzinfo=timezone.utc,
)


def make_source(
    source_id: int,
    source_type: str,
    source_value: str,
    *,
    decision: str = "include",
    origin: str = "manual",
) -> NewsletterSource:
    return NewsletterSource(
        id=source_id,
        account_id=1,
        source_type=source_type,
        source_value=source_value,
        decision=decision,
        origin=origin,
        confidence=100,
        active=True,
        created_at=NOW,
        updated_at=NOW,
        last_matched_at=None,
    )


class NewsletterClassificationServiceTests(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.source_store = Mock(
            spec=NewsletterSourceStore
        )
        self.source_store.list_active_sources.return_value = ()

        self.classifier = NewsletterClassifier()
        self.service = NewsletterClassificationService(
            source_store=self.source_store,
            classifier=self.classifier,
        )

        self.metadata = GmailMessageMetadata(
            message_id="message-1",
            thread_id="thread-1",
            internal_date_ms=1_780_000_000_000,
            label_ids=("INBOX",),
            subject="Example message",
            sender="Person <person@example.com>",
            date_header=None,
            list_id=None,
            list_unsubscribe=None,
            precedence=None,
            auto_submitted=None,
        )

    def test_heuristic_result_is_used_without_rules(
        self,
    ) -> None:
        result = self.service.classify(
            self.metadata
        )

        self.assertEqual(
            result.verdict,
            NewsletterVerdict.NOT_NEWSLETTER,
        )
        self.assertEqual(result.score, 0)
        self.assertEqual(result.reasons, ())

        (
            self.source_store
            .mark_source_matched
            .assert_not_called()
        )

    def test_manual_exclusion_wins_over_inclusion(
        self,
    ) -> None:
        included_sender = make_source(
            1,
            "sender",
            "news@news.example.com",
            decision="include",
        )
        excluded_domain = make_source(
            2,
            "domain",
            "news.example.com",
            decision="exclude",
        )

        self.source_store.list_active_sources.return_value = (
            included_sender,
            excluded_domain,
        )
        self.source_store.mark_source_matched.return_value = (
            excluded_domain
        )

        metadata = replace(
            self.metadata,
            sender=(
                "News <news@news.example.com>"
            ),
            list_id="<daily.example.com>",
            list_unsubscribe=(
                "<mailto:unsubscribe@example.com>"
            ),
        )

        result = self.service.classify(metadata)

        self.assertEqual(
            result.verdict,
            NewsletterVerdict.NOT_NEWSLETTER,
        )
        self.assertEqual(result.score, 0)
        self.assertEqual(
            result.reasons,
            (
                "stored_source:"
                "manual:exclude:domain",
            ),
        )

        (
            self.source_store
            .mark_source_matched
            .assert_called_once_with(
                "domain",
                "news.example.com",
            )
        )

    def test_manual_inclusion_wins_over_confirmed_exclusion(
        self,
    ) -> None:
        confirmed_exclusion = make_source(
            1,
            "domain",
            "example.com",
            decision="exclude",
            origin="confirmed",
        )
        manual_inclusion = make_source(
            2,
            "sender",
            "person@example.com",
            decision="include",
            origin="manual",
        )

        self.source_store.list_active_sources.return_value = (
            confirmed_exclusion,
            manual_inclusion,
        )
        self.source_store.mark_source_matched.return_value = (
            manual_inclusion
        )

        result = self.service.classify(
            self.metadata
        )

        self.assertEqual(
            result.verdict,
            NewsletterVerdict.NEWSLETTER,
        )
        self.assertEqual(
            result.reasons,
            (
                "stored_source:"
                "manual:include:sender",
            ),
        )

        (
            self.source_store
            .mark_source_matched
            .assert_called_once_with(
                "sender",
                "person@example.com",
            )
        )

    def test_confirmed_list_id_is_normalized(
        self,
    ) -> None:
        confirmed_source = make_source(
            1,
            "list_id",
            "weekly.example.com",
            origin="confirmed",
        )

        self.source_store.list_active_sources.return_value = (
            confirmed_source,
        )
        self.source_store.mark_source_matched.return_value = (
            confirmed_source
        )

        metadata = replace(
            self.metadata,
            list_id=(
                "Weekly Digest "
                "<weekly.example.com>"
            ),
        )

        result = self.service.classify(metadata)

        self.assertEqual(
            result.verdict,
            NewsletterVerdict.NEWSLETTER,
        )
        self.assertEqual(
            result.reasons,
            (
                "stored_source:"
                "confirmed:include:list_id",
            ),
        )

        (
            self.source_store
            .mark_source_matched
            .assert_called_once_with(
                "list_id",
                "weekly.example.com",
            )
        )

    def test_gmail_label_matching_is_case_sensitive(
        self,
    ) -> None:
        label_source = make_source(
            1,
            "gmail_label",
            "Label_Newsletters",
        )

        self.source_store.list_active_sources.return_value = (
            label_source,
        )
        self.source_store.mark_source_matched.return_value = (
            label_source
        )

        different_case = replace(
            self.metadata,
            label_ids=("label_newsletters",),
        )

        unmatched_result = self.service.classify(
            different_case
        )

        self.assertEqual(
            unmatched_result.verdict,
            NewsletterVerdict.NOT_NEWSLETTER,
        )
        (
            self.source_store
            .mark_source_matched
            .assert_not_called()
        )

        exact_case = replace(
            self.metadata,
            label_ids=("Label_Newsletters",),
        )

        matched_result = self.service.classify(
            exact_case
        )

        self.assertEqual(
            matched_result.verdict,
            NewsletterVerdict.NEWSLETTER,
        )
        (
            self.source_store
            .mark_source_matched
            .assert_called_once_with(
                "gmail_label",
                "Label_Newsletters",
            )
        )

    def test_automatic_rule_does_not_override_heuristics(
        self,
    ) -> None:
        automatic_exclusion = make_source(
            1,
            "sender",
            "person@example.com",
            decision="exclude",
            origin="automatic",
        )

        self.source_store.list_active_sources.return_value = (
            automatic_exclusion,
        )

        metadata = replace(
            self.metadata,
            list_id="<daily.example.com>",
            list_unsubscribe=(
                "<mailto:unsubscribe@example.com>"
            ),
        )

        result = self.service.classify(metadata)

        self.assertEqual(
            result.verdict,
            NewsletterVerdict.NEWSLETTER,
        )
        self.assertEqual(result.score, 6)
        self.assertEqual(
            result.reasons,
            (
                "list_id_header",
                "list_unsubscribe_header",
            ),
        )

        (
            self.source_store
            .mark_source_matched
            .assert_not_called()
        )


if __name__ == "__main__":
    unittest.main()
