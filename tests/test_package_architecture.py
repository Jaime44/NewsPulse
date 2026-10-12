"""Test package imports and guard against legacy desktop OAuth integration."""

from __future__ import annotations

import importlib
import unittest

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

GMAIL_MODULES = (
    "app.tools.gmail.gmail_client",
    "app.tools.gmail.messages_client",
    "app.tools.gmail.user_client",
)

LEGACY_MARKERS = (
    "from tools",
    "import tools",
    "InstalledAppFlow",
    "GOOGLE_APPLICATION_CREDENTIALS_DESKTOP_APP",
    "GOOGLE_APPLICATION_TOKENS_DESKTOP_APP",
    "sys.path.append",
)

REQUIRED_LOG_IGNORE_PATTERNS = frozenset(
    {
        "logs/*.log",
        "logs/*.log.[0-9]*",
        "logs/*.log.lock",
    }
)
REQUIRED_DATABASE_BACKUP_IGNORE_PATTERNS = (
    frozenset(
        {
            "data/*.db.bak*",
            "data/backups/",
        }
    )
)


class PackageArchitectureTests(unittest.TestCase):
    """Verify package-boundary and OAuth architecture invariants."""

    def test_gmail_modules_import_from_project_root(self) -> None:
        """Gmail modules must use the real app package."""

        for module_name in GMAIL_MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                self.assertIsNotNone(module)

    def test_application_has_no_legacy_oauth_markers(self) -> None:
        """Legacy desktop OAuth must not return unnoticed."""

        violations: dict[str, list[str]] = {}

        for source_path in sorted(
            (PROJECT_ROOT / "app").rglob("*.py")
        ):
            source = source_path.read_text(encoding="utf-8")
            matches = [
                marker
                for marker in LEGACY_MARKERS
                if marker in source
            ]

            if matches:
                relative_path = source_path.relative_to(
                    PROJECT_ROOT
                )
                violations[str(relative_path)] = matches

        self.assertEqual(violations, {})

    def test_runtime_log_artifacts_are_ignored(self) -> None:
        """Active logs, numeric archives, and lock files must stay untracked."""

        ignore_patterns = {
            line.strip()
            for line in (
                PROJECT_ROOT / ".gitignore"
            ).read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
            and not line.lstrip().startswith("#")
        }

        self.assertTrue(
            REQUIRED_LOG_IGNORE_PATTERNS
            <= ignore_patterns
        )

    def test_database_backups_are_ignored(self) -> None:
        """Local SQLite backups containing private data must stay untracked."""

        ignore_patterns = {
            line.strip()
            for line in (
                PROJECT_ROOT / ".gitignore"
            ).read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
            and not line.lstrip().startswith("#")
        }

        self.assertTrue(
            REQUIRED_DATABASE_BACKUP_IGNORE_PATTERNS
            <= ignore_patterns
        )


if __name__ == "__main__":
    unittest.main()
