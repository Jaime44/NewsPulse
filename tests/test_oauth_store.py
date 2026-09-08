from __future__ import annotations

import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials

from app.backend.bd.db import Database
from app.backend.bd.oauth_store import OAuthCredentialStore


class OAuthCredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.directory = Path(
            self.temporary_directory.name
        )

        self.database_path = self.directory / "test.db"
        self.encryption_key_path = (
            self.directory / "encryption.key"
        )

        self.encryption_key_path.write_bytes(
            Fernet.generate_key()
        )

        self.database = Database(self.database_path)
        self.store = OAuthCredentialStore(
            database=self.database,
            encryption_key_path=self.encryption_key_path,
        )

        self.credentials = Credentials(
            token="test-access-token",
            refresh_token="test-refresh-token",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="test-client-id",
            client_secret="test-client-secret",
            scopes=[
                "https://www.googleapis.com/auth/gmail.readonly"
            ],
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_save_and_load_credentials(self) -> None:
        self.store.save(
            email="Owner@Example.com",
            credentials=self.credentials,
        )

        stored = self.store.load()

        self.assertIsNotNone(stored)
        assert stored is not None

        self.assertEqual(stored.account_id, 1)
        self.assertEqual(stored.email, "owner@example.com")
        self.assertFalse(stored.revoked)
        self.assertEqual(
            stored.credentials.refresh_token,
            "test-refresh-token",
        )

    def test_sensitive_values_are_encrypted_at_rest(self) -> None:
        self.store.save(
            email="owner@example.com",
            credentials=self.credentials,
        )

        database_contents = self.database_path.read_bytes()

        self.assertNotIn(
            b"test-access-token",
            database_contents,
        )
        self.assertNotIn(
            b"test-refresh-token",
            database_contents,
        )
        self.assertNotIn(
            b"test-client-secret",
            database_contents,
        )

    def test_mark_credentials_as_revoked(self) -> None:
        self.store.save(
            email="owner@example.com",
            credentials=self.credentials,
        )

        self.store.mark_revoked()
        stored = self.store.load()

        self.assertIsNotNone(stored)
        assert stored is not None

        self.assertTrue(stored.revoked)

    def test_delete_credentials(self) -> None:
        self.store.save(
            email="owner@example.com",
            credentials=self.credentials,
        )

        self.store.delete()

        self.assertIsNone(self.store.load())


if __name__ == "__main__":
    unittest.main()
