from __future__ import annotations

from googleapiclient.discovery import Resource

from app.backend.bd.db import Database
from app.backend.bd.ingestion_store import (
    IngestionStore,
)
from app.backend.bd.message_content_store import (
    MessageContentStore,
)
from app.backend.bd.newsletter_source_store import (
    NewsletterSourceStore,
)
from app.backend.services.classifier import (
    NewsletterClassifier,
)
from app.backend.services.content_parser import (
    MessageContentParser,
)
from app.backend.services.gmail_ingestion import (
    GmailIngestionService,
)
from app.backend.services.message_content import (
    GmailMessageContentService,
)
from app.backend.services.message_content_processing import (
    MessageContentProcessingService,
)
from app.backend.services.newsletter_classification import (
    NewsletterClassificationService,
)
from app.tools.gmail.gmail_client import GmailClient


def build_gmail_ingestion_service(
    gmail_service: Resource,
    database: Database,
    retention_days: int,
) -> GmailIngestionService:
    """Build the complete incremental ingestion service."""

    ingestion_store = IngestionStore(
        database
    )
    source_store = NewsletterSourceStore(
        database
    )
    content_store = MessageContentStore(
        database,
        retention_days=retention_days,
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

    content_service = (
        GmailMessageContentService(
            gmail_client
        )
    )
    content_parser = (
        MessageContentParser()
    )
    content_processing_service = (
        MessageContentProcessingService(
            content_service=content_service,
            parser=content_parser,
            store=content_store,
        )
    )

    return GmailIngestionService(
        gmail_client=gmail_client,
        store=ingestion_store,
        classification_service=(
            classification_service
        ),
        content_processing_service=(
            content_processing_service
        ),
    )
