from __future__ import annotations

from googleapiclient.discovery import Resource

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
from app.backend.services.newsletter_classification import (
    NewsletterClassificationService,
)
from app.tools.gmail.gmail_client import GmailClient


def build_gmail_ingestion_service(
    gmail_service: Resource,
    database: Database,
) -> GmailIngestionService:
    """Build the complete incremental ingestion service."""

    ingestion_store = IngestionStore(
        database
    )
    source_store = NewsletterSourceStore(
        database
    )
    classifier = NewsletterClassifier()

    classification_service = (
        NewsletterClassificationService(
            source_store=source_store,
            classifier=classifier,
        )
    )

    gmail_client = GmailClient(
        gmail_service
    )

    return GmailIngestionService(
        gmail_client=gmail_client,
        store=ingestion_store,
        classification_service=(
            classification_service
        ),
    )