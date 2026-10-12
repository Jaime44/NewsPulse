"""Test real logging integration at the Flask application boundary."""

from __future__ import annotations

import importlib
import sys
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.backend.bd.db import CURRENT_SCHEMA_VERSION
from app.config import AppConfig
from app.tools.logger import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_DIRECTORY,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_MAX_BYTES,
    AppLogger,
)


class WebLoggingIntegrationTests(unittest.TestCase):
    """Verify startup and request traces without touching project logs."""

    def test_startup_and_request_use_configured_rotating_logger(self) -> None:
        """Verify actual Flask hooks, correlation, format, and log location."""

        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            google_credentials_path = directory / "google.json"
            flask_secret_key_path = directory / "flask.key"
            token_encryption_key_path = directory / "token.key"
            log_directory = directory / "logs"

            google_credentials_path.write_text(
                "{}",
                encoding="utf-8",
            )
            flask_secret_key_path.write_text(
                "t" * 64,
                encoding="utf-8",
            )
            token_encryption_key_path.write_bytes(
                Fernet.generate_key()
            )

            settings = AppConfig(
                environment="testing",
                host="localhost",
                port=5000,
                timezone="Europe/Madrid",
                debug=False,
                google_credentials_path=(
                    google_credentials_path
                ),
                google_redirect_uri=(
                    "http://localhost:5000/auth/google/callback"
                ),
                google_scopes=(
                    "https://www.googleapis.com/auth/gmail.readonly",
                ),
                flask_secret_key_path=flask_secret_key_path,
                token_encryption_key_path=(
                    token_encryption_key_path
                ),
                database_path=directory / "test.db",
                retention_days=30,
                log_directory=log_directory,
                log_max_bytes=100_000,
                log_backup_count=2,
                log_level="INFO",
            )

            sys.modules.pop(
                "app.backend.web_app",
                None,
            )

            try:
                with patch.object(
                    AppConfig,
                    "load",
                    return_value=settings,
                ):
                    web_app = importlib.import_module(
                        "app.backend.web_app"
                    )

                web_app.app.config.update(
                    TESTING=True
                )
                response = web_app.app.test_client().get(
                    "/"
                )
                self.assertEqual(
                    response.status_code,
                    200,
                )
            finally:
                AppLogger.configure(
                    log_directory=(
                        DEFAULT_LOG_DIRECTORY
                    ),
                    max_bytes=(
                        DEFAULT_LOG_MAX_BYTES
                    ),
                    backup_count=(
                        DEFAULT_LOG_BACKUP_COUNT
                    ),
                    level=DEFAULT_LOG_LEVEL,
                )
                sys.modules.pop(
                    "app.backend.web_app",
                    None,
                )

            content = (
                log_directory / "web_app.log"
            ).read_text(encoding="utf-8")
            self.assertIn(
                "NewsPulse application initialized successfully",
                content,
            )
            self.assertIn(
                "Database schema migration started: "
                "current_version=0 target_version="
                f"{CURRENT_SCHEMA_VERSION}",
                content,
            )
            self.assertIn(
                "Database schema migration completed: "
                "initial_version=0 final_version="
                f"{CURRENT_SCHEMA_VERSION}",
                content,
            )
            self.assertIn(
                "HTTP request started: method=GET path=/",
                content,
            )
            self.assertIn(
                "HTTP request completed: method=GET path=/ status=200",
                content,
            )
            self.assertNotIn(
                "[correlation_id=-] HTTP request started",
                content,
            )


if __name__ == "__main__":
    unittest.main()
