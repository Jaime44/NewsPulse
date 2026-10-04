from __future__ import annotations

import unittest

from dataclasses import replace

from app.backend.services.classifier import (
    NewsletterClassification,
    NewsletterClassifier,
    NewsletterClassifierError,
    NewsletterVerdict,
)
from app.tools.gmail.gmail_client import (
    GmailMessageMetadata,
)


class NewsletterClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
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

    def test_message_without_signals_is_not_newsletter(
        self,
    ) -> None:
        classifier = NewsletterClassifier()

        result = classifier.classify(
            self.metadata
        )

        self.assertEqual(
            result,
            NewsletterClassification(
                verdict=(
                    NewsletterVerdict
                    .NOT_NEWSLETTER
                ),
                score=0,
                reasons=(),
            ),
        )
        self.assertFalse(result.is_newsletter)
        self.assertFalse(result.requires_review)

    def test_single_list_header_requires_review(
        self,
    ) -> None:
        classifier = NewsletterClassifier()

        metadata = replace(
            self.metadata,
            list_id="<weekly.example.com>",
        )

        result = classifier.classify(metadata)

        self.assertEqual(
            result.verdict,
            NewsletterVerdict.REVIEW,
        )
        self.assertEqual(result.score, 3)
        self.assertEqual(
            result.reasons,
            ("list_id_header",),
        )
        self.assertFalse(result.is_newsletter)
        self.assertTrue(result.requires_review)

    def test_combined_list_headers_classify_newsletter(
        self,
    ) -> None:
        classifier = NewsletterClassifier()

        metadata = replace(
            self.metadata,
            list_id="<weekly.example.com>",
            list_unsubscribe=(
                "<mailto:unsubscribe@example.com>"
            ),
        )

        result = classifier.classify(metadata)

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
        self.assertTrue(result.is_newsletter)
        self.assertFalse(result.requires_review)

    def test_bulk_precedence_requires_review(
        self,
    ) -> None:
        classifier = NewsletterClassifier()

        metadata = replace(
            self.metadata,
            precedence=" BULK ",
        )

        result = classifier.classify(metadata)

        self.assertEqual(
            result.verdict,
            NewsletterVerdict.REVIEW,
        )
        self.assertEqual(result.score, 2)
        self.assertEqual(
            result.reasons,
            ("bulk_precedence",),
        )

    def test_configured_label_classifies_newsletter(
        self,
    ) -> None:
        classifier = NewsletterClassifier(
            newsletter_label_ids=(
                "Label_Newsletters",
            )
        )

        metadata = replace(
            self.metadata,
            label_ids=(
                "INBOX",
                "Label_Newsletters",
            ),
        )

        result = classifier.classify(metadata)

        self.assertEqual(
            result.verdict,
            NewsletterVerdict.NEWSLETTER,
        )
        self.assertEqual(result.score, 5)
        self.assertEqual(
            result.reasons,
            ("configured_gmail_label",),
        )

    def test_configured_sender_is_normalized(
        self,
    ) -> None:
        classifier = NewsletterClassifier(
            known_senders=(
                " NEWS@example.com ",
            )
        )

        metadata = replace(
            self.metadata,
            sender=(
                "News Team <news@EXAMPLE.com>"
            ),
        )

        result = classifier.classify(metadata)

        self.assertEqual(
            result.verdict,
            NewsletterVerdict.NEWSLETTER,
        )
        self.assertEqual(result.score, 5)
        self.assertEqual(
            result.reasons,
            ("configured_sender",),
        )

    def test_auto_submitted_is_only_a_weak_signal(
        self,
    ) -> None:
        classifier = NewsletterClassifier()

        automated_result = classifier.classify(
            replace(
                self.metadata,
                auto_submitted="auto-generated",
            )
        )

        manual_result = classifier.classify(
            replace(
                self.metadata,
                auto_submitted="no",
            )
        )

        self.assertEqual(
            automated_result.verdict,
            NewsletterVerdict.NOT_NEWSLETTER,
        )
        self.assertEqual(
            automated_result.score,
            1,
        )
        self.assertEqual(
            automated_result.reasons,
            ("auto_submitted",),
        )

        self.assertEqual(
            manual_result.score,
            0,
        )
        self.assertEqual(
            manual_result.reasons,
            (),
        )

    def test_invalid_configuration_is_rejected(
        self,
    ) -> None:
        invalid_configurations = (
            {
                "newsletter_label_ids": (
                    "Label_Newsletters"
                ),
            },
            {
                "newsletter_label_ids": (
                    "",
                ),
            },
            {
                "newsletter_label_ids": (
                    1,
                ),
            },
            {
                "known_senders": (
                    "news@example.com"
                ),
            },
            {
                "known_senders": (
                    " ",
                ),
            },
            {
                "known_senders": (
                    None,
                ),
            },
        )

        for configuration in invalid_configurations:
            with self.subTest(
                configuration=configuration
            ):
                with self.assertRaises(
                    NewsletterClassifierError
                ):
                    NewsletterClassifier(
                        **configuration
                    )

    def test_invalid_metadata_is_rejected(
        self,
    ) -> None:
        classifier = NewsletterClassifier()

        invalid_metadata = (
            None,
            {},
            "message",
        )

        for value in invalid_metadata:
            with self.subTest(value=value):
                with self.assertRaises(
                    NewsletterClassifierError
                ):
                    classifier.classify(value)
