from __future__ import annotations

import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app.backend.bd.db import Database
from app.backend.bd.ingestion_store import (
    IngestionStore,
)
from app.backend.bd.newsletter_source_store import (
    NewsletterSourceStore,
)
from app.backend.services.classifier import (
    NewsletterClassifier,
)
from app.backend.services.gmail_ingestion import (
    GmailIngestionService,
)
from app.backend.services.ingestion_factory import (
    build_gmail_ingestion_service,
)
from app.backend.services.newsletter_classification import (
    NewsletterClassificationService,
)
from app.tools.gmail.gmail_client import GmailClient


class IngestionFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()

        database_path = (
            Path(self.temporary_directory.name)
            / "test.db"
        )

        self.database = Database(database_path)
        self.gmail_service = Mock()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_builds_complete_ingestion_service(
        self,
    ) -> None:
        service = build_gmail_ingestion_service(
            gmail_service=self.gmail_service,
            database=self.database,
        )

        self.assertIsInstance(
            service,
            GmailIngestionService,
        )
        self.assertIsInstance(
            service.gmail_client,
            GmailClient,
        )
        self.assertIsInstance(
            service.store,
            IngestionStore,
        )
        self.assertIsInstance(
            service.classification_service,
            NewsletterClassificationService,
        )
        self.assertIsInstance(
            (
                service
                .classification_service
                .source_store
            ),
            NewsletterSourceStore,
        )
        self.assertIsInstance(
            (
                service
                .classification_service
                .classifier
            ),
            NewsletterClassifier,
        )

        self.assertIs(
            service.store.database,
            self.database,
        )
        self.assertIs(
            (
                service
                .classification_service
                .source_store
                .database
            ),
            self.database,
        )
        self.assertIs(
            (
                service
                .gmail_client
                .messages
                .service
            ),
            self.gmail_service,
        )
        self.assertIs(
            (
                service
                .gmail_client
                .users
                .service
            ),
            self.gmail_service,
        )


if __name__ == "__main__":
    unittest.main()
