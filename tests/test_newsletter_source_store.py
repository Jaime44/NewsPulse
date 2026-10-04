from __future__ import annotations

import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.backend.bd.db import Database
from app.backend.bd.newsletter_source_store import (
    NewsletterSourceStore,
    NewsletterSourceStoreError,
)


STARTED_AT = datetime(
    2026,
    9,
    14,
    10,
    0,
    tzinfo=timezone.utc,
)


class NewsletterSourceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = (
            Path(self.temporary_directory.name)
            / "test.db"
        )

        self.database = Database(database_path)
        self.store = NewsletterSourceStore(
            self.database
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def insert_account(self) -> None:
        timestamp = STARTED_AT.isoformat()

        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO oauth_credentials (
                    id,
                    email,
                    credentials_encrypted,
                    revoked,
                    connected_at,
                    updated_at
                )
                VALUES (1, ?, ?, 0, ?, ?)
                """,
                (
                    "owner@example.com",
                    b"encrypted-credentials",
                    timestamp,
                    timestamp,
                ),
            )

    def test_source_requires_authenticated_account(
        self,
    ) -> None:
        with self.assertRaises(
            NewsletterSourceStoreError
        ):
            self.store.save_source(
                "sender",
                "newsletter@example.com",
                observed_at=STARTED_AT,
            )

    def test_sender_is_normalized_and_loaded(
        self,
    ) -> None:
        self.insert_account()

        saved_source = self.store.save_source(
            "sender",
            "Daily News <NEWS@Example.com>",
            observed_at=STARTED_AT,
        )

        loaded_source = self.store.load_source(
            "sender",
            "news@example.com",
        )

        self.assertIsNotNone(loaded_source)
        assert loaded_source is not None

        self.assertEqual(
            loaded_source,
            saved_source,
        )
        self.assertEqual(
            loaded_source.account_id,
            1,
        )
        self.assertEqual(
            loaded_source.source_type,
            "sender",
        )
        self.assertEqual(
            loaded_source.source_value,
            "news@example.com",
        )
        self.assertEqual(
            loaded_source.decision,
            "include",
        )
        self.assertEqual(
            loaded_source.origin,
            "manual",
        )
        self.assertEqual(
            loaded_source.confidence,
            100,
        )
        self.assertTrue(loaded_source.active)
        self.assertEqual(
            loaded_source.created_at,
            STARTED_AT,
        )
        self.assertEqual(
            loaded_source.updated_at,
            STARTED_AT,
        )
        self.assertIsNone(
            loaded_source.last_matched_at
        )

    def test_existing_source_is_updated(
        self,
    ) -> None:
        self.insert_account()

        original_source = self.store.save_source(
            "sender",
            "news@example.com",
            observed_at=STARTED_AT,
        )

        updated_at = (
            STARTED_AT + timedelta(hours=1)
        )

        updated_source = self.store.save_source(
            "sender",
            "NEWS@example.com",
            decision="exclude",
            origin="confirmed",
            confidence=95,
            observed_at=updated_at,
        )

        with self.database.connect() as connection:
            source_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM newsletter_sources
                """
            ).fetchone()["total"]

        self.assertEqual(source_count, 1)
        self.assertEqual(
            updated_source.id,
            original_source.id,
        )
        self.assertEqual(
            updated_source.created_at,
            STARTED_AT,
        )
        self.assertEqual(
            updated_source.updated_at,
            updated_at,
        )
        self.assertEqual(
            updated_source.decision,
            "exclude",
        )
        self.assertEqual(
            updated_source.origin,
            "confirmed",
        )
        self.assertEqual(
            updated_source.confidence,
            95,
        )

    def test_active_sources_can_be_listed_by_decision(
        self,
    ) -> None:
        self.insert_account()

        included_sender = self.store.save_source(
            "sender",
            "news@example.com",
            decision="include",
            observed_at=STARTED_AT,
        )
        excluded_domain = self.store.save_source(
            "domain",
            "@example.org.",
            decision="exclude",
            observed_at=STARTED_AT,
        )

        all_sources = (
            self.store.list_active_sources()
        )
        included_sources = (
            self.store.list_active_sources(
                decision="include"
            )
        )
        excluded_sources = (
            self.store.list_active_sources(
                decision="exclude"
            )
        )

        self.assertEqual(
            all_sources,
            (
                excluded_domain,
                included_sender,
            ),
        )
        self.assertEqual(
            included_sources,
            (included_sender,),
        )
        self.assertEqual(
            excluded_sources,
            (excluded_domain,),
        )

    def test_source_can_be_deactivated_and_reactivated(
        self,
    ) -> None:
        self.insert_account()

        original_source = self.store.save_source(
            "sender",
            "news@example.com",
            observed_at=STARTED_AT,
        )

        deactivated_at = (
            STARTED_AT + timedelta(hours=1)
        )

        was_deactivated = (
            self.store.deactivate_source(
                "sender",
                "NEWS@example.com",
                changed_at=deactivated_at,
            )
        )

        inactive_source = self.store.load_source(
            "sender",
            "news@example.com",
        )

        self.assertTrue(was_deactivated)
        self.assertIsNotNone(inactive_source)
        assert inactive_source is not None
        self.assertFalse(inactive_source.active)
        self.assertEqual(
            inactive_source.updated_at,
            deactivated_at,
        )
        self.assertEqual(
            self.store.list_active_sources(),
            (),
        )

        self.assertFalse(
            self.store.deactivate_source(
                "sender",
                "news@example.com",
                changed_at=deactivated_at,
            )
        )

        reactivated_at = (
            STARTED_AT + timedelta(hours=2)
        )

        reactivated_source = self.store.save_source(
            "sender",
            "news@example.com",
            observed_at=reactivated_at,
        )

        self.assertEqual(
            reactivated_source.id,
            original_source.id,
        )
        self.assertTrue(
            reactivated_source.active
        )
        self.assertEqual(
            reactivated_source.updated_at,
            reactivated_at,
        )

    def test_active_source_match_is_recorded(
        self,
    ) -> None:
        self.insert_account()

        self.store.save_source(
            "list_id",
            "<daily.example.com>",
            observed_at=STARTED_AT,
        )

        matched_at = (
            STARTED_AT + timedelta(minutes=30)
        )

        matched_source = (
            self.store.mark_source_matched(
                "list_id",
                "DAILY.EXAMPLE.COM",
                matched_at=matched_at,
            )
        )

        self.assertIsNotNone(matched_source)
        assert matched_source is not None
        self.assertEqual(
            matched_source.source_value,
            "daily.example.com",
        )
        self.assertEqual(
            matched_source.last_matched_at,
            matched_at,
        )
        self.assertEqual(
            matched_source.updated_at,
            matched_at,
        )

        unknown_source = (
            self.store.mark_source_matched(
                "sender",
                "unknown@example.com",
                matched_at=matched_at,
            )
        )

        self.assertIsNone(unknown_source)

    def test_invalid_source_data_is_rejected(
        self,
    ) -> None:
        self.insert_account()

        naive_timestamp = datetime(
            2026,
            9,
            14,
            10,
            0,
        )

        invalid_cases = (
            (
                "unknown",
                "value",
                {},
            ),
            (
                "sender",
                "not-an-email",
                {},
            ),
            (
                "sender",
                "",
                {},
            ),
            (
                "domain",
                "@",
                {},
            ),
            (
                "sender",
                "news@example.com",
                {"decision": "ignore"},
            ),
            (
                "sender",
                "news@example.com",
                {"origin": "unknown"},
            ),
            (
                "sender",
                "news@example.com",
                {"confidence": -1},
            ),
            (
                "sender",
                "news@example.com",
                {"confidence": 101},
            ),
            (
                "sender",
                "news@example.com",
                {"confidence": True},
            ),
            (
                "sender",
                "news@example.com",
                {"observed_at": naive_timestamp},
            ),
        )

        for (
            source_type,
            source_value,
            keyword_arguments,
        ) in invalid_cases:
            with self.subTest(
                source_type=source_type,
                source_value=source_value,
                keyword_arguments=keyword_arguments,
            ):
                with self.assertRaises(
                    NewsletterSourceStoreError
                ):
                    self.store.save_source(
                        source_type,
                        source_value,
                        **keyword_arguments,
                    )


if __name__ == "__main__":
    unittest.main()