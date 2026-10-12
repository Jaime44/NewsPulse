"""Test environment-backed NewsPulse logging configuration."""

from __future__ import annotations

import os
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import AppConfig, ConfigurationError


class LoggingConfigurationTests(unittest.TestCase):
    """Verify configurable levels, paths, rotation size, and retention."""

    def setUp(self) -> None:
        """Create valid non-production files required by ``AppConfig``."""

        self.temporary_directory = TemporaryDirectory()
        self.directory = Path(
            self.temporary_directory.name
        )
        self.google_credentials = (
            self.directory / "google.json"
        )
        self.flask_key = self.directory / "flask.key"
        self.encryption_key = (
            self.directory / "encryption.key"
        )
        self.google_credentials.write_text(
            "{}",
            encoding="utf-8",
        )
        self.flask_key.write_text(
            "test-key",
            encoding="utf-8",
        )
        self.encryption_key.write_text(
            "test-encryption-key",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        """Remove the isolated configuration tree."""

        self.temporary_directory.cleanup()

    def write_environment(
        self,
        *,
        log_level: str,
    ) -> Path:
        """Write a complete dotenv file with explicit logging settings.

        Args:
            log_level: Named threshold written to the environment fixture.

        Returns:
            Path of the complete temporary dotenv file.
        """

        environment_file = self.directory / ".env"
        environment_file.write_text(
            "\n".join(
                (
                    "APP_ENV=testing",
                    "APP_HOST=localhost",
                    "APP_PORT=5000",
                    "APP_TIMEZONE=Europe/Madrid",
                    "APP_DEBUG=false",
                    "GOOGLE_APPLICATION_CREDENTIALS_WEB_APP="
                    f"{self.google_credentials}",
                    "GOOGLE_REDIRECT_URI=http://localhost:5000/"
                    "auth/google/callback",
                    "GOOGLE_SCOPES=https://www.googleapis.com/"
                    "auth/gmail.readonly",
                    f"FLASK_SECRET_KEY_PATH={self.flask_key}",
                    "TOKEN_ENCRYPTION_KEY_PATH="
                    f"{self.encryption_key}",
                    f"DATABASE_PATH={self.directory / 'test.db'}",
                    "SQLITE_BUSY_TIMEOUT_MS=1234",
                    "INGESTION_LEASE_SECONDS=321",
                    "RETENTION_DAYS=30",
                    f"LOG_DIRECTORY={self.directory / 'logs'}",
                    "LOG_MAX_BYTES=4096",
                    "LOG_BACKUP_COUNT=7",
                    f"LOG_LEVEL={log_level}",
                )
            ),
            encoding="utf-8",
        )
        return environment_file

    def test_loads_debug_and_rotation_settings(self) -> None:
        """Verify that development logging needs no source-code change."""

        environment_file = self.write_environment(
            log_level="debug",
        )

        with patch.dict(os.environ, {}, clear=True):
            settings = AppConfig.load(
                environment_file
            )

        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(settings.log_max_bytes, 4096)
        self.assertEqual(settings.log_backup_count, 7)
        self.assertEqual(
            settings.sqlite_busy_timeout_ms,
            1_234,
        )
        self.assertEqual(
            settings.ingestion_lease_seconds,
            321,
        )
        self.assertEqual(
            settings.log_directory,
            (self.directory / "logs").resolve(),
        )

    def test_rejects_unknown_log_level(self) -> None:
        """Verify that a misspelled threshold fails configuration loading."""

        environment_file = self.write_environment(
            log_level="verbose",
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            AppConfig.load(environment_file)


if __name__ == "__main__":
    unittest.main()
