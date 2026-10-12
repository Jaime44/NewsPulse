"""Test durable ingestion cursors, messages, classifications, and transitions."""

from __future__ import annotations

import unittest

from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from multiprocessing.queues import Queue
from pathlib import Path
from tempfile import TemporaryDirectory

from app.backend.bd.db import Database
from app.backend.bd.ingestion_store import (
    IngestionLeaseLostError,
    IngestionRunAlreadyActiveError,
    IngestionRunCounters,
    IngestionStore,
    IngestionStoreError,
)


STARTED_AT = datetime(
    2026,
    9,
    13,
    10,
    0,
    tzinfo=timezone.utc,
)


class AdjustableClock:
    """Supply a mutable aware timestamp to deterministic lease tests."""

    def __init__(self, current: datetime) -> None:
        """Store the initial timestamp.

        Args:
            current: Time returned by the first call.
        """

        self.current = current

    def __call__(self) -> datetime:
        """Return the configured timestamp."""

        return self.current


def _attempt_scan_run_in_process(
    database_path: str,
    current_time: str,
    result_queue: Queue,
) -> None:
    """Try to acquire an existing database lease in a child process.

    Args:
        database_path: Shared SQLite database location.
        current_time: ISO timestamp used by the deterministic child clock.
        result_queue: Queue receiving only a bounded outcome code.
    """

    lease_time = datetime.fromisoformat(current_time)
    store = IngestionStore(
        Database(Path(database_path)),
        lease_seconds=60,
        clock=lambda: lease_time,
    )

    try:
        store.start_scan_run(started_at=lease_time)
    except IngestionRunAlreadyActiveError:
        result_queue.put("blocked")
    except Exception as exc:
        result_queue.put(
            f"unexpected:{type(exc).__name__}"
        )
    else:
        result_queue.put("acquired")


class IngestionStoreTests(unittest.TestCase):
    """Verify durable ingestion state and message lifecycles."""

    def setUp(self) -> None:
        """Create isolated dependencies and fixtures for each test."""

        self.temporary_directory = TemporaryDirectory()
        database_path = (
            Path(self.temporary_directory.name)
            / "test.db"
        )

        self.database = Database(database_path)
        self.store = IngestionStore(self.database)

    def tearDown(self) -> None:
        """Release temporary resources created for each test."""

        self.temporary_directory.cleanup()

    def insert_account(self) -> None:
        """Insert the single authenticated account required by the fixture."""

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

    def test_state_requires_authenticated_account(
        self,
    ) -> None:
        """Verify that state requires authenticated account."""

        with self.assertRaises(IngestionStoreError):
            self.store.get_or_create_state(
                started_at=STARTED_AT
            )

    def test_initial_timestamp_is_created_only_once(
        self,
    ) -> None:
        """Verify that initial timestamp is created only once."""

        self.insert_account()

        first_state = self.store.get_or_create_state(
            started_at=STARTED_AT
        )

        later_time = STARTED_AT + timedelta(days=7)

        second_state = self.store.get_or_create_state(
            started_at=later_time
        )

        self.assertEqual(
            first_state.started_at,
            STARTED_AT,
        )
        self.assertEqual(
            second_state.started_at,
            STARTED_AT,
        )
        self.assertEqual(
            second_state.created_at,
            first_state.created_at,
        )
        self.assertIsNone(
            second_state.last_successful_scan_at
        )

    def test_timestamp_is_normalized_to_utc(
        self,
    ) -> None:
        """Verify that timestamp is normalized to UTC."""

        self.insert_account()

        madrid_offset = timezone(
            timedelta(hours=2)
        )
        local_time = datetime(
            2026,
            9,
            13,
            12,
            0,
            tzinfo=madrid_offset,
        )

        state = self.store.get_or_create_state(
            started_at=local_time
        )

        self.assertEqual(
            state.started_at,
            STARTED_AT,
        )
        self.assertEqual(
            state.started_at.tzinfo,
            timezone.utc,
        )

    def test_successful_scan_advances_cursor(
        self,
    ) -> None:
        """Verify that successful scan advances cursor."""

        self.insert_account()
        self.store.get_or_create_state(
            started_at=STARTED_AT
        )

        completed_at = (
            STARTED_AT + timedelta(hours=1)
        )

        updated_state = (
            self.store.mark_scan_successful(
                completed_at=completed_at
            )
        )
        stored_state = self.store.load_state()

        self.assertEqual(
            updated_state.last_successful_scan_at,
            completed_at,
        )
        self.assertIsNotNone(stored_state)
        assert stored_state is not None
        self.assertEqual(
            stored_state.last_successful_scan_at,
            completed_at,
        )

    def test_cursor_cannot_move_backwards(
        self,
    ) -> None:
        """Verify that cursor cannot move backwards."""

        self.insert_account()
        self.store.get_or_create_state(
            started_at=STARTED_AT
        )

        first_completion = (
            STARTED_AT + timedelta(hours=2)
        )
        earlier_completion = (
            STARTED_AT + timedelta(hours=1)
        )

        self.store.mark_scan_successful(
            completed_at=first_completion
        )

        with self.assertRaises(IngestionStoreError):
            self.store.mark_scan_successful(
                completed_at=earlier_completion
            )

        stored_state = self.store.load_state()

        self.assertIsNotNone(stored_state)
        assert stored_state is not None
        self.assertEqual(
            stored_state.last_successful_scan_at,
            first_completion,
        )

    def test_naive_timestamp_is_rejected(
        self,
    ) -> None:
        """Verify that naive timestamp is rejected."""

        self.insert_account()

        naive_timestamp = datetime(
            2026,
            9,
            13,
            10,
            0,
        )

        with self.assertRaises(IngestionStoreError):
            self.store.get_or_create_state(
                started_at=naive_timestamp
            )

    def test_scan_run_requires_authenticated_account(
        self,
    ) -> None:
        """Verify a durable scan run cannot exist without its account."""

        with self.assertRaises(
            IngestionStoreError
        ):
            self.store.start_scan_run(
                started_at=STARTED_AT
            )

    def test_active_lease_blocks_another_process(
        self,
    ) -> None:
        """Verify a second process cannot start while the lease is valid."""

        self.insert_account()
        clock = AdjustableClock(STARTED_AT)
        owner_store = IngestionStore(
            self.database,
            lease_seconds=60,
            clock=clock,
        )
        owner_store.start_scan_run(
            started_at=STARTED_AT
        )

        context = get_context("spawn")
        result_queue = context.Queue()
        process = context.Process(
            target=_attempt_scan_run_in_process,
            args=(
                str(self.database.path),
                STARTED_AT.isoformat(),
                result_queue,
            ),
        )
        process.start()
        result = result_queue.get(timeout=10)
        process.join(timeout=10)
        result_queue.close()
        result_queue.join_thread()

        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(result, "blocked")

    def test_run_start_defaults_to_operational_clock(
        self,
    ) -> None:
        """Verify an omitted run timestamp shares the lease clock."""

        self.insert_account()
        clock = AdjustableClock(STARTED_AT)
        store = IngestionStore(
            self.database,
            lease_seconds=60,
            clock=clock,
        )

        run_id = store.start_scan_run()

        with self.database.connect() as connection:
            started_at = connection.execute(
                """
                SELECT started_at
                FROM ingestion_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()["started_at"]

        self.assertEqual(
            started_at,
            STARTED_AT.isoformat(),
        )

    def test_expired_lease_is_recovered_by_next_run(
        self,
    ) -> None:
        """Verify an abandoned lease is failed before a replacement starts."""

        self.insert_account()
        clock = AdjustableClock(STARTED_AT)
        store = IngestionStore(
            self.database,
            lease_seconds=60,
            clock=clock,
        )
        store.get_or_create_state(
            started_at=STARTED_AT
        )
        abandoned_run_id = store.start_scan_run(
            started_at=STARTED_AT
        )

        clock.current = STARTED_AT + timedelta(
            seconds=61
        )
        replacement_run_id = store.start_scan_run(
            started_at=clock.current
        )

        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    status,
                    error_code,
                    lease_heartbeat_at,
                    lease_expires_at
                FROM ingestion_runs
                ORDER BY id
                """
            ).fetchall()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], abandoned_run_id)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["error_code"], "lease_expired")
        self.assertIsNone(rows[0]["lease_heartbeat_at"])
        self.assertIsNone(rows[0]["lease_expires_at"])
        self.assertEqual(rows[1]["id"], replacement_run_id)
        self.assertEqual(rows[1]["status"], "running")

        with self.assertRaises(IngestionLeaseLostError):
            store.renew_scan_run(abandoned_run_id)

        with self.assertRaises(IngestionLeaseLostError):
            store.complete_scan_run(
                abandoned_run_id,
                finished_at=clock.current,
                cursor_before=STARTED_AT,
                cursor_after=clock.current,
                counters=IngestionRunCounters(),
            )

        state = store.load_state()
        self.assertIsNotNone(state)
        assert state is not None
        self.assertIsNone(
            state.last_successful_scan_at
        )

    def test_lease_heartbeat_extends_expiry(
        self,
    ) -> None:
        """Verify a valid owner can extend its exclusive lease."""

        self.insert_account()
        clock = AdjustableClock(STARTED_AT)
        store = IngestionStore(
            self.database,
            lease_seconds=60,
            clock=clock,
        )
        run_id = store.start_scan_run(
            started_at=STARTED_AT
        )
        clock.current = STARTED_AT + timedelta(
            seconds=30
        )

        store.renew_scan_run(run_id)

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT lease_heartbeat_at, lease_expires_at
                FROM ingestion_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(
            row["lease_heartbeat_at"],
            clock.current.isoformat(),
        )
        self.assertEqual(
            row["lease_expires_at"],
            (
                clock.current
                + timedelta(seconds=60)
            ).isoformat(),
        )

    def test_successful_scan_run_advances_cursor_atomically(
        self,
    ) -> None:
        """Verify a successful run stores counters and advances its cursor."""

        self.prepare_ingestion()
        run_id = self.store.start_scan_run(
            started_at=STARTED_AT
        )
        completed_at = (
            STARTED_AT + timedelta(minutes=5)
        )
        counters = IngestionRunCounters(
            listed_count=3,
            discovered_count=2,
            already_known_count=1,
            newsletter_count=1,
            review_count=1,
            purged_content_count=4,
        )

        state = self.store.complete_scan_run(
            run_id,
            finished_at=completed_at,
            cursor_before=STARTED_AT,
            cursor_after=completed_at,
            counters=counters,
        )

        with self.database.connect() as connection:
            run = connection.execute(
                """
                SELECT *
                FROM ingestion_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()

        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(
            run["cursor_before"],
            STARTED_AT.isoformat(),
        )
        self.assertEqual(
            run["cursor_after"],
            completed_at.isoformat(),
        )
        self.assertEqual(run["listed_count"], 3)
        self.assertEqual(run["discovered_count"], 2)
        self.assertEqual(run["already_known_count"], 1)
        self.assertEqual(run["newsletter_count"], 1)
        self.assertEqual(run["review_count"], 1)
        self.assertEqual(run["purged_content_count"], 4)
        self.assertIsNone(run["error_code"])
        self.assertEqual(
            state.last_successful_scan_at,
            completed_at,
        )

    def test_failed_scan_run_preserves_cursor(
        self,
    ) -> None:
        """Verify a failed run records a safe code without cursor movement."""

        self.prepare_ingestion()
        run_id = self.store.start_scan_run(
            started_at=STARTED_AT
        )
        failed_at = (
            STARTED_AT + timedelta(minutes=2)
        )

        self.store.fail_scan_run(
            run_id,
            failed_at=failed_at,
            error_code="gmail_client_failed",
            counters=IngestionRunCounters(
                listed_count=2,
                discovered_count=1,
            ),
            cursor_before=STARTED_AT,
        )

        state = self.store.load_state()

        with self.database.connect() as connection:
            run = connection.execute(
                """
                SELECT *
                FROM ingestion_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()

        self.assertIsNotNone(state)
        assert state is not None
        self.assertIsNone(
            state.last_successful_scan_at
        )
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run["status"], "failed")
        self.assertEqual(
            run["error_code"],
            "gmail_client_failed",
        )
        self.assertIsNone(run["cursor_after"])

    def test_run_completion_failure_rolls_back_cursor(
        self,
    ) -> None:
        """Verify a missing active run cannot advance the durable cursor."""

        self.prepare_ingestion()
        completed_at = (
            STARTED_AT + timedelta(minutes=5)
        )

        with self.assertRaises(
            IngestionStoreError
        ):
            self.store.complete_scan_run(
                999,
                finished_at=completed_at,
                cursor_before=STARTED_AT,
                cursor_after=completed_at,
                counters=IngestionRunCounters(),
            )

        state = self.store.load_state()

        self.assertIsNotNone(state)
        assert state is not None
        self.assertIsNone(
            state.last_successful_scan_at
        )

    def prepare_ingestion(self) -> None:
        """Create the authenticated account and initialize ingestion state."""

        self.insert_account()
        self.store.get_or_create_state(
            started_at=STARTED_AT
        )

    def test_message_requires_initialized_ingestion(
        self,
    ) -> None:
        """Verify that message requires initialized ingestion."""

        self.insert_account()

        with self.assertRaises(IngestionStoreError):
            self.store.register_message(
                gmail_message_id="gmail-message-1",
                gmail_thread_id="gmail-thread-1",
                internal_date_ms=1_757_721_600_000,
                discovered_at=STARTED_AT,
            )

    def test_message_is_registered_and_loaded(
        self,
    ) -> None:
        """Verify that message is registered and loaded."""

        self.prepare_ingestion()

        discovered_at = (
            STARTED_AT + timedelta(minutes=5)
        )

        was_inserted = self.store.register_message(
            gmail_message_id=" gmail-message-1 ",
            gmail_thread_id=" gmail-thread-1 ",
            internal_date_ms=1_757_721_600_000,
            discovered_at=discovered_at,
        )

        stored_message = self.store.load_message(
            "gmail-message-1"
        )

        self.assertTrue(was_inserted)
        self.assertTrue(
            self.store.is_message_known(
                "gmail-message-1"
            )
        )
        self.assertIsNotNone(stored_message)
        assert stored_message is not None

        self.assertEqual(stored_message.account_id, 1)
        self.assertEqual(
            stored_message.gmail_message_id,
            "gmail-message-1",
        )
        self.assertEqual(
            stored_message.gmail_thread_id,
            "gmail-thread-1",
        )
        self.assertEqual(
            stored_message.internal_date_ms,
            1_757_721_600_000,
        )
        self.assertEqual(
            stored_message.status,
            "discovered",
        )
        self.assertEqual(
            stored_message.discovered_at,
            discovered_at,
        )
        self.assertIsNone(stored_message.processed_at)
        self.assertIsNone(stored_message.last_error_code)

    def test_duplicate_message_is_idempotent(
        self,
    ) -> None:
        """Verify that duplicate message is idempotent."""

        self.prepare_ingestion()

        first_insert = self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-original",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )

        duplicate_insert = self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-changed",
            internal_date_ms=1_757_725_200_000,
            discovered_at=(
                STARTED_AT + timedelta(hours=1)
            ),
        )

        stored_message = self.store.load_message(
            "gmail-message-1"
        )

        self.assertTrue(first_insert)
        self.assertFalse(duplicate_insert)
        self.assertIsNotNone(stored_message)
        assert stored_message is not None

        self.assertEqual(
            stored_message.gmail_thread_id,
            "gmail-thread-original",
        )
        self.assertEqual(
            stored_message.internal_date_ms,
            1_757_721_600_000,
        )

    def test_message_identifiers_are_required(
        self,
    ) -> None:
        """Verify that message identifiers are required."""

        self.prepare_ingestion()

        with self.assertRaises(IngestionStoreError):
            self.store.register_message(
                gmail_message_id="",
                gmail_thread_id="gmail-thread-1",
                internal_date_ms=1,
            )

        with self.assertRaises(IngestionStoreError):
            self.store.register_message(
                gmail_message_id="gmail-message-1",
                gmail_thread_id="   ",
                internal_date_ms=1,
            )

    def test_internal_date_must_be_valid(
        self,
    ) -> None:
        """Verify that internal date must be valid."""

        self.prepare_ingestion()

        invalid_values = (
            -1,
            True,
            1.5,
        )

        for invalid_value in invalid_values:
            with self.subTest(value=invalid_value):
                with self.assertRaises(
                    IngestionStoreError
                ):
                    self.store.register_message(
                        gmail_message_id=(
                            f"message-{invalid_value}"
                        ),
                        gmail_thread_id="thread-1",
                        internal_date_ms=invalid_value,
                    )

    def test_message_timestamp_requires_timezone(
        self,
    ) -> None:
        """Verify that message timestamp requires timezone."""

        self.prepare_ingestion()

        naive_timestamp = datetime(
            2026,
            9,
            13,
            10,
            0,
        )

        with self.assertRaises(IngestionStoreError):
            self.store.register_message(
                gmail_message_id="gmail-message-1",
                gmail_thread_id="gmail-thread-1",
                internal_date_ms=1,
                discovered_at=naive_timestamp,
            )

    def test_unknown_message_is_not_known(
        self,
    ) -> None:
        """Verify that unknown message is not known."""

        self.prepare_ingestion()

        self.assertFalse(
            self.store.is_message_known(
                "unknown-message"
            )
        )
        self.assertIsNone(
            self.store.load_message(
                "unknown-message"
            )
        )

    def test_newsletter_classification_is_saved_and_loaded(
        self,
    ) -> None:
        """Verify that newsletter classification is saved and loaded."""

        self.prepare_ingestion()

        self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-1",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )

        classified_at = (
            STARTED_AT + timedelta(minutes=10)
        )

        saved_classification = (
            self.store.save_message_classification(
                "gmail-message-1",
                verdict="newsletter",
                score=6,
                reasons=(
                    "list_id_header",
                    "list_unsubscribe_header",
                ),
                classifier_version="rules-v1",
                classified_at=classified_at,
            )
        )

        loaded_classification = (
            self.store.load_message_classification(
                "gmail-message-1"
            )
        )
        stored_message = self.store.load_message(
            "gmail-message-1"
        )

        self.assertEqual(
            loaded_classification,
            saved_classification,
        )
        self.assertEqual(
            saved_classification.gmail_message_id,
            "gmail-message-1",
        )
        self.assertEqual(
            saved_classification.verdict,
            "newsletter",
        )
        self.assertEqual(
            saved_classification.score,
            6,
        )
        self.assertEqual(
            saved_classification.reasons,
            (
                "list_id_header",
                "list_unsubscribe_header",
            ),
        )
        self.assertEqual(
            saved_classification.classifier_version,
            "rules-v1",
        )
        self.assertEqual(
            saved_classification.classified_at,
            classified_at,
        )

        self.assertIsNotNone(stored_message)
        assert stored_message is not None
        self.assertEqual(
            stored_message.status,
            "discovered",
        )
        self.assertIsNone(
            stored_message.processed_at
        )
        self.assertIsNone(
            stored_message.last_error_code
        )

    def test_not_newsletter_marks_message_skipped(
        self,
    ) -> None:
        """Verify that a non-newsletter verdict marks the message skipped."""

        self.prepare_ingestion()

        self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-1",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )

        classified_at = (
            STARTED_AT + timedelta(minutes=10)
        )

        classification = (
            self.store.save_message_classification(
                "gmail-message-1",
                verdict="not_newsletter",
                score=0,
                reasons=(),
                classifier_version="rules-v1",
                classified_at=classified_at,
            )
        )
        stored_message = self.store.load_message(
            "gmail-message-1"
        )

        self.assertEqual(
            classification.reasons,
            (),
        )
        self.assertIsNotNone(stored_message)
        assert stored_message is not None
        self.assertEqual(
            stored_message.status,
            "skipped",
        )
        self.assertEqual(
            stored_message.processed_at,
            classified_at,
        )

    def test_classification_can_be_replaced(
        self,
    ) -> None:
        """Verify that classification can be replaced."""

        self.prepare_ingestion()

        self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-1",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )

        first_classified_at = (
            STARTED_AT + timedelta(minutes=10)
        )
        second_classified_at = (
            STARTED_AT + timedelta(minutes=20)
        )

        first_classification = (
            self.store.save_message_classification(
                "gmail-message-1",
                verdict="not_newsletter",
                score=0,
                reasons=(),
                classifier_version="rules-v1",
                classified_at=first_classified_at,
            )
        )

        second_classification = (
            self.store.save_message_classification(
                "gmail-message-1",
                verdict="newsletter",
                score=5,
                reasons=("configured_sender",),
                classifier_version="rules-v2",
                classified_at=second_classified_at,
            )
        )

        stored_message = self.store.load_message(
            "gmail-message-1"
        )

        with self.database.connect() as connection:
            classification_count = (
                connection.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM message_classifications
                    """
                ).fetchone()["total"]
            )

        self.assertEqual(classification_count, 1)
        self.assertEqual(
            second_classification.gmail_message_row_id,
            first_classification.gmail_message_row_id,
        )
        self.assertEqual(
            second_classification.verdict,
            "newsletter",
        )
        self.assertEqual(
            second_classification.classifier_version,
            "rules-v2",
        )

        self.assertIsNotNone(stored_message)
        assert stored_message is not None
        self.assertEqual(
            stored_message.status,
            "discovered",
        )
        self.assertIsNone(
            stored_message.processed_at
        )

    def test_reclassification_removes_retained_content(
        self,
    ) -> None:
        """Verify a non-newsletter replacement removes retained content."""

        self.prepare_ingestion()
        self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-1",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )
        self.store.save_message_classification(
            "gmail-message-1",
            verdict="newsletter",
            score=5,
            reasons=("configured_sender",),
            classifier_version="rules-v1",
            classified_at=STARTED_AT,
        )

        expires_at = (
            STARTED_AT + timedelta(days=30)
        )

        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO message_contents (
                    gmail_message_row_id,
                    text_content,
                    links_json,
                    source_mime_type,
                    extracted_at,
                    expires_at
                )
                SELECT
                    id,
                    'Readable newsletter content',
                    '[]',
                    'text/plain',
                    ?,
                    ?
                FROM gmail_messages
                WHERE gmail_message_id = ?
                """,
                (
                    STARTED_AT.isoformat(),
                    expires_at.isoformat(),
                    "gmail-message-1",
                ),
            )

        classified_at = (
            STARTED_AT + timedelta(minutes=10)
        )
        self.store.save_message_classification(
            "gmail-message-1",
            verdict="not_newsletter",
            score=0,
            reasons=(),
            classifier_version="rules-v2",
            classified_at=classified_at,
        )

        with self.database.connect() as connection:
            content_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM message_contents
                """
            ).fetchone()["total"]

        stored_message = self.store.load_message(
            "gmail-message-1"
        )

        self.assertEqual(content_count, 0)
        self.assertIsNotNone(stored_message)
        assert stored_message is not None
        self.assertEqual(
            stored_message.status,
            "skipped",
        )
        self.assertEqual(
            stored_message.processed_at,
            classified_at,
        )

    def test_classification_requires_registered_message(
        self,
    ) -> None:
        """Verify that classification requires registered message."""

        self.prepare_ingestion()

        with self.assertRaises(
            IngestionStoreError
        ):
            self.store.save_message_classification(
                "unknown-message",
                verdict="newsletter",
                score=5,
                reasons=("configured_sender",),
                classifier_version="rules-v1",
                classified_at=STARTED_AT,
            )

    def test_invalid_classification_data_is_rejected(
        self,
    ) -> None:
        """Verify that invalid classification data is rejected."""

        self.prepare_ingestion()

        self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-1",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )

        naive_timestamp = datetime(
            2026,
            9,
            13,
            10,
            0,
        )

        valid_arguments = {
            "verdict": "newsletter",
            "score": 5,
            "reasons": ("configured_sender",),
            "classifier_version": "rules-v1",
            "classified_at": STARTED_AT,
        }

        invalid_overrides = (
            {"verdict": "unknown"},
            {"score": -1},
            {"score": True},
            {"reasons": "configured_sender"},
            {"reasons": ("",)},
            {"reasons": (1,)},
            {"classifier_version": " "},
            {"classified_at": naive_timestamp},
        )

        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                arguments = {
                    **valid_arguments,
                    **overrides,
                }

                with self.assertRaises(
                    IngestionStoreError
                ):
                    self.store.save_message_classification(
                        "gmail-message-1",
                        **arguments,
                    )

        self.assertIsNone(
            self.store.load_message_classification(
                "gmail-message-1"
            )
        )

    def test_message_processing_lifecycle_can_be_retried(
        self,
    ) -> None:
        """Verify that message processing lifecycle can be retried."""

        self.prepare_ingestion()

        self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-1",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )

        first_processing_at = (
            STARTED_AT
            + timedelta(minutes=1)
        )
        failed_at = (
            STARTED_AT
            + timedelta(minutes=2)
        )
        retry_processing_at = (
            STARTED_AT
            + timedelta(minutes=3)
        )
        completed_at = (
            STARTED_AT
            + timedelta(minutes=4)
        )

        processing_message = (
            self.store.mark_message_processing(
                "gmail-message-1",
                started_at=first_processing_at,
            )
        )

        self.assertEqual(
            processing_message.status,
            "processing",
        )
        self.assertEqual(
            processing_message.updated_at,
            first_processing_at,
        )
        self.assertIsNone(
            processing_message.processed_at
        )
        self.assertIsNone(
            processing_message.last_error_code
        )

        failed_message = (
            self.store.mark_message_failed(
                "gmail-message-1",
                error_code=(
                    "content_processing_failed"
                ),
                failed_at=failed_at,
            )
        )

        self.assertEqual(
            failed_message.status,
            "failed",
        )
        self.assertEqual(
            failed_message.updated_at,
            failed_at,
        )
        self.assertIsNone(
            failed_message.processed_at
        )
        self.assertEqual(
            failed_message.last_error_code,
            "content_processing_failed",
        )

        retry_message = (
            self.store.mark_message_processing(
                "gmail-message-1",
                started_at=retry_processing_at,
            )
        )

        self.assertEqual(
            retry_message.status,
            "processing",
        )
        self.assertEqual(
            retry_message.updated_at,
            retry_processing_at,
        )
        self.assertIsNone(
            retry_message.processed_at
        )
        self.assertIsNone(
            retry_message.last_error_code
        )

        completed_message = (
            self.store.mark_message_processed(
                "gmail-message-1",
                completed_at=completed_at,
            )
        )

        stored_message = self.store.load_message(
            "gmail-message-1"
        )

        self.assertEqual(
            completed_message.status,
            "processed",
        )
        self.assertEqual(
            completed_message.updated_at,
            completed_at,
        )
        self.assertEqual(
            completed_message.processed_at,
            completed_at,
        )
        self.assertIsNone(
            completed_message.last_error_code
        )
        self.assertEqual(
            stored_message,
            completed_message,
        )

    def test_message_transitions_and_classifications_are_historical(
        self,
    ) -> None:
        """Verify triggers preserve status and classification decisions."""

        self.prepare_ingestion()
        self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-1",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )
        self.store.save_message_classification(
            "gmail-message-1",
            verdict="newsletter",
            score=5,
            reasons=("configured_sender",),
            classifier_version="rules-v1",
            classified_at=STARTED_AT,
        )
        processing_at = (
            STARTED_AT + timedelta(minutes=1)
        )
        failed_at = (
            STARTED_AT + timedelta(minutes=2)
        )
        reclassified_at = (
            STARTED_AT + timedelta(minutes=3)
        )
        self.store.mark_message_processing(
            "gmail-message-1",
            started_at=processing_at,
        )
        self.store.mark_message_failed(
            "gmail-message-1",
            error_code="content_processing_failed",
            failed_at=failed_at,
        )
        self.store.save_message_classification(
            "gmail-message-1",
            verdict="not_newsletter",
            score=0,
            reasons=(),
            classifier_version="rules-v2",
            classified_at=reclassified_at,
        )

        with self.database.connect() as connection:
            statuses = tuple(
                (
                    row["from_status"],
                    row["to_status"],
                    row["error_code"],
                )
                for row in connection.execute(
                    """
                    SELECT
                        from_status,
                        to_status,
                        error_code
                    FROM message_status_history
                    ORDER BY id
                    """
                )
            )
            classifications = tuple(
                (
                    row["verdict"],
                    row["score"],
                    row["classifier_version"],
                )
                for row in connection.execute(
                    """
                    SELECT
                        verdict,
                        score,
                        classifier_version
                    FROM message_classification_history
                    ORDER BY id
                    """
                )
            )

        self.assertEqual(
            statuses,
            (
                (None, "discovered", None),
                ("discovered", "processing", None),
                (
                    "processing",
                    "failed",
                    "content_processing_failed",
                ),
                ("failed", "skipped", None),
            ),
        )
        self.assertEqual(
            classifications,
            (
                ("newsletter", 5, "rules-v1"),
                ("not_newsletter", 0, "rules-v2"),
            ),
        )

    def test_processing_status_requires_registered_message(
        self,
    ) -> None:
        """Verify that processing status requires registered message."""

        self.prepare_ingestion()

        with self.assertRaises(
            IngestionStoreError
        ):
            self.store.mark_message_processing(
                "unknown-message",
                started_at=STARTED_AT,
            )

        with self.assertRaises(
            IngestionStoreError
        ):
            self.store.mark_message_processed(
                "unknown-message",
                completed_at=STARTED_AT,
            )

        with self.assertRaises(
            IngestionStoreError
        ):
            self.store.mark_message_failed(
                "unknown-message",
                error_code=(
                    "content_processing_failed"
                ),
                failed_at=STARTED_AT,
            )

    def test_failed_status_requires_safe_error_code(
        self,
    ) -> None:
        """Verify that failed status requires safe error code."""

        self.prepare_ingestion()

        self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-1",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )

        invalid_error_codes = (
            "",
            "   ",
            None,
            "x" * 101,
        )

        for error_code in invalid_error_codes:
            with self.subTest(
                error_code=error_code
            ):
                with self.assertRaises(
                    IngestionStoreError
                ):
                    self.store.mark_message_failed(
                        "gmail-message-1",
                        error_code=error_code,
                        failed_at=STARTED_AT,
                    )

        stored_message = self.store.load_message(
            "gmail-message-1"
        )

        self.assertIsNotNone(
            stored_message
        )
        assert stored_message is not None
        self.assertEqual(
            stored_message.status,
            "discovered",
        )
        self.assertIsNone(
            stored_message.last_error_code
        )

    def test_processing_timestamps_require_timezone(
        self,
    ) -> None:
        """Verify that processing timestamps require timezone."""

        self.prepare_ingestion()

        self.store.register_message(
            gmail_message_id="gmail-message-1",
            gmail_thread_id="gmail-thread-1",
            internal_date_ms=1_757_721_600_000,
            discovered_at=STARTED_AT,
        )

        naive_timestamp = datetime(
            2026,
            9,
            13,
            10,
            0,
        )

        with self.assertRaises(
            IngestionStoreError
        ):
            self.store.mark_message_processing(
                "gmail-message-1",
                started_at=naive_timestamp,
            )

        with self.assertRaises(
            IngestionStoreError
        ):
            self.store.mark_message_processed(
                "gmail-message-1",
                completed_at=naive_timestamp,
            )

        with self.assertRaises(
            IngestionStoreError
        ):
            self.store.mark_message_failed(
                "gmail-message-1",
                error_code=(
                    "content_processing_failed"
                ),
                failed_at=naive_timestamp,
            )

if __name__ == "__main__":
    unittest.main()
