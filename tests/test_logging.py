"""Test NewsPulse logging levels, safety, handlers, and file rotation."""

from __future__ import annotations

import io
import logging
import multiprocessing
import re
import unittest

from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.tools.log_rotation import LogRotationManager
from app.tools.logger import (
    AppLogger,
    is_error_logged,
    mark_error_logged,
)


def _write_from_process(
    log_directory: str,
    worker_name: str,
    event_count: int,
) -> None:
    """Write deterministic events from one isolated child process.

    Args:
        log_directory: Temporary directory shared by all worker processes.
        worker_name: Stable name included in each worker event.
        event_count: Number of events emitted by this process.
    """

    logger = AppLogger(
        "multiprocess.log",
        log_directory=Path(log_directory),
        max_bytes=650,
        backup_count=20,
        level="INFO",
    )

    for event_number in range(event_count):
        logger.info(
            "Worker event: worker=%s event=%d",
            worker_name,
            event_number,
        )

    AppLogger.shutdown()


class AppLoggerTests(unittest.TestCase):
    """Verify the compatibility API and centralized log behavior."""

    def setUp(self) -> None:
        """Create an isolated log directory for every test."""

        self.temporary_directory = TemporaryDirectory()
        self.log_directory = Path(
            self.temporary_directory.name
        )

    def tearDown(self) -> None:
        """Close handlers before removing the temporary directory."""

        AppLogger.shutdown()
        self.temporary_directory.cleanup()

    def make_logger(
        self,
        *,
        level: str = "DEBUG",
        max_bytes: int = 100_000,
        backup_count: int = 3,
    ) -> AppLogger:
        """Return a logger whose files are scoped to this test.

        Args:
            level: Minimum severity accepted by the handler.
            max_bytes: Active-file rollover threshold.
            backup_count: Maximum numeric archives retained.

        Returns:
            An isolated logger targeting ``test.log``.
        """

        return AppLogger(
            "test.log",
            log_directory=self.log_directory,
            max_bytes=max_bytes,
            backup_count=backup_count,
            level=level,
        )

    def read_log(self, name: str = "test.log") -> str:
        """Read one completed UTF-8 test log.

        Args:
            name: Log file name inside the temporary directory.

        Returns:
            Current UTF-8 contents of the selected log.
        """

        return (
            self.log_directory / name
        ).read_text(encoding="utf-8")

    def test_all_supported_levels_are_written_with_context(self) -> None:
        """Verify levels, component, caller, and correlation formatting."""

        logger = self.make_logger()
        token = AppLogger.set_correlation_id(
            "scan-123"
        )

        try:
            logger.debug("Debug event")
            logger.info("Info event")
            logger.warning("Warning event")
            logger.error("Error event")
            logger.critical("Critical event")
        finally:
            AppLogger.reset_correlation_id(token)

        content = self.read_log()

        for level in (
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        ):
            self.assertIn(f"[{level}]", content)

        self.assertIn("[test:test_all_supported_levels_are_written", content)
        self.assertIn("[correlation_id=scan-123]", content)
        self.assertRegex(
            content,
            re.compile(
                r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}"
                r"[+-]\d{4}\]"
            ),
        )

    def test_legacy_log_reports_its_direct_caller(self) -> None:
        """Verify the original ``log`` API preserves caller attribution."""

        logger = self.make_logger(level="INFO")

        def emit_legacy_event() -> None:
            """Emit one event through the original two-argument API."""

            logger.log(
                "Legacy direct event",
                "INFO",
            )

        emit_legacy_event()

        self.assertIn(
            "[test:emit_legacy_event]",
            self.read_log(),
        )

    def test_configured_level_filters_lower_severities(self) -> None:
        """Verify that INFO prevents DEBUG formatting and output."""

        class FailsIfFormatted:
            """Fail the test if a filtered argument is rendered eagerly."""

            def __str__(self) -> str:
                """Reject formatting of the filtered diagnostic value."""

                raise AssertionError(
                    "filtered argument was formatted"
                )

        logger = self.make_logger(level="INFO")
        logger.debug(
            "Hidden value: %s",
            FailsIfFormatted(),
        )
        logger.info("Visible value")
        logger.log(
            "Legacy API event: %s",
            "INFO",
            "visible",
        )

        content = self.read_log()
        self.assertFalse(
            logger.is_enabled_for("DEBUG")
        )
        self.assertTrue(
            logger.is_enabled_for("INFO")
        )
        self.assertNotIn("Hidden value", content)
        self.assertIn("Visible value", content)
        self.assertIn(
            "Legacy API event: visible",
            content,
        )

    def test_repeated_instances_do_not_duplicate_messages(self) -> None:
        """Verify that one file receives one shared process-local handler."""

        first = self.make_logger(level="INFO")
        second = self.make_logger(level="INFO")

        first.info("Single event")
        second.info("Second event")

        content = self.read_log()
        self.assertEqual(content.count("Single event"), 1)
        self.assertEqual(content.count("Second event"), 1)
        self.assertEqual(len(first._logger.handlers), 1)

    def test_handler_failure_does_not_interrupt_application_flow(self) -> None:
        """Verify handler failures omit records and cannot expose secrets."""

        logger = self.make_logger(level="INFO")
        handler = logger._logger.handlers[0]
        captured_stderr = io.StringIO()

        with (
            patch.object(
                handler,
                "_reopen_if_rotated",
                side_effect=OSError(
                    "simulated rotation failure"
                ),
            ),
            patch.object(
                logging,
                "raiseExceptions",
                True,
            ),
            redirect_stderr(captured_stderr),
        ):
            logger.info(
                "password=HANDLER_FAILURE_SECRET"
            )

        diagnostic = captured_stderr.getvalue()
        self.assertIn(
            "NewsPulse logging handler failure; record omitted",
            diagnostic,
        )
        self.assertNotIn(
            "HANDLER_FAILURE_SECRET",
            diagnostic,
        )

    def test_sensitive_assignments_and_tracebacks_are_redacted(self) -> None:
        """Verify central masking without changing exception propagation."""

        logger = self.make_logger(level="ERROR")

        try:
            raise RuntimeError(
                "access_token=top-secret-value"
            )
        except RuntimeError:
            logger.error(
                "Authorization: Basic basic-secret\n"
                "Cookie: session=cookie-one; preference=cookie-two\n"
                "credentials=credential-secret\n"
                "Detached Bearer bearer-secret and password=hunter2; "
                "payload="
                '{"access_token": "json-secret", '
                "'client_secret': 'python-secret'}",
                exc_info=True,
            )

        content = self.read_log()
        self.assertIn("Traceback", content)
        self.assertIn("[REDACTED]", content)
        self.assertNotIn("top-secret-value", content)
        self.assertNotIn("basic-secret", content)
        self.assertNotIn("cookie-one", content)
        self.assertNotIn("cookie-two", content)
        self.assertNotIn("credential-secret", content)
        self.assertNotIn("bearer-secret", content)
        self.assertNotIn("hunter2", content)
        self.assertNotIn("json-secret", content)
        self.assertNotIn("python-secret", content)

    def test_logged_error_marker_preserves_exception_identity(self) -> None:
        """Verify centralized duplicate suppression marks the same exception."""

        error = RuntimeError(
            "sanitized failure"
        )

        marked_error = mark_error_logged(
            error
        )

        self.assertIs(marked_error, error)
        self.assertTrue(
            is_error_logged(error)
        )
        self.assertFalse(
            is_error_logged(
                RuntimeError("new failure")
            )
        )

    def test_rotation_preserves_recent_events_and_backup_limit(self) -> None:
        """Verify bounded archives and complete recent-event retention."""

        logger = self.make_logger(
            level="INFO",
            max_bytes=360,
            backup_count=5,
        )

        for event_number in range(8):
            logger.info(
                "Rotating event %02d %s",
                event_number,
                "x" * 35,
            )

        AppLogger.shutdown()
        files = sorted(
            self.log_directory.glob("test.log*"),
        )
        archives = [
            path
            for path in files
            if path.name.removeprefix("test.log.").isdigit()
        ]
        combined_content = "".join(
            path.read_text(encoding="utf-8")
            for path in [
                self.log_directory / "test.log",
                *archives,
            ]
        )

        self.assertLessEqual(len(archives), 5)
        self.assertIn("Rotating event 07", combined_content)

        for event_number in range(8):
            self.assertIn(
                f"Rotating event {event_number:02d}",
                combined_content,
            )

    def test_existing_log_is_appended_and_excess_archives_are_pruned(
        self,
    ) -> None:
        """Verify safe append behavior and numeric-archive retention."""

        active_log = self.log_directory / "test.log"
        active_log.write_text(
            "existing event\n",
            encoding="utf-8",
        )
        (self.log_directory / "test.log.1").write_text(
            "recent archive\n",
            encoding="utf-8",
        )
        excess_archive = self.log_directory / "test.log.4"
        excess_archive.write_text(
            "expired archive\n",
            encoding="utf-8",
        )
        custom_archive = self.log_directory / "test.log.saved"
        custom_archive.write_text(
            "custom archive\n",
            encoding="utf-8",
        )

        logger = self.make_logger(
            level="INFO",
            backup_count=2,
        )
        logger.info("new event")

        self.assertIn("existing event", self.read_log())
        self.assertIn("new event", self.read_log())
        self.assertFalse(excess_archive.exists())
        self.assertTrue(custom_archive.exists())

    def test_multiple_processes_write_every_event(self) -> None:
        """Verify forked processes serialize rollover without losing events."""

        manager = LogRotationManager(
            self.log_directory,
            max_bytes=650,
            backup_count=20,
        )

        if not manager.supports_multiple_processes:
            self.skipTest(
                "POSIX advisory locks are unavailable"
            )

        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest(
                "The fork start method is unavailable"
            )

        parent_logger = AppLogger(
            "multiprocess.log",
            log_directory=self.log_directory,
            max_bytes=650,
            backup_count=20,
            level="INFO",
        )
        parent_logger.info("Parent event: before fork")

        context = multiprocessing.get_context(
            "fork"
        )
        workers = [
            context.Process(
                target=_write_from_process,
                args=(
                    str(self.log_directory),
                    worker_name,
                    20,
                ),
            )
            for worker_name in ("alpha", "beta")
        ]

        for worker in workers:
            worker.start()

        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)

        parent_logger.info("Parent event: after fork")
        AppLogger.shutdown()

        log_files = [
            path
            for path in self.log_directory.glob(
                "multiprocess.log*"
            )
            if (
                path.name == "multiprocess.log"
                or path.name.removeprefix(
                    "multiprocess.log."
                ).isdigit()
            )
        ]
        content = "".join(
            path.read_text(encoding="utf-8")
            for path in log_files
        )
        self.assertGreater(
            len(log_files),
            1,
        )
        self.assertEqual(
            content.count("Worker event:"),
            40,
        )
        self.assertEqual(
            content.count("Parent event:"),
            2,
        )

    def test_rejects_log_paths_outside_configured_directory(self) -> None:
        """Verify that a component cannot escape the configured directory."""

        with self.assertRaises(ValueError):
            AppLogger(
                "../outside.log",
                log_directory=self.log_directory,
            )


if __name__ == "__main__":
    unittest.main()
