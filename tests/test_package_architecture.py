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


class PackageArchitectureTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()