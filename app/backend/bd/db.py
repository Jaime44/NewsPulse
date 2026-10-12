"""Own the SQLite connection policy and NewsPulse relational schema.

The :class:`Database` factory enables foreign-key enforcement on every
connection and migrates the versioned single-account OAuth, ingestion,
classification, newsletter-source, and retained-content schema used by the
storage services.
"""

from __future__ import annotations

import json
import os
import sqlite3

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

from app.tools.logger import AppLogger


CURRENT_SCHEMA_VERSION = 4
DEFAULT_BUSY_TIMEOUT_MS = 5_000
PRIVATE_DATABASE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
SCHEMA_VERSION_1_COLUMNS = {
    "oauth_credentials": (
        "id",
        "email",
        "credentials_encrypted",
        "revoked",
        "connected_at",
        "updated_at",
    ),
    "ingestion_state": (
        "account_id",
        "started_at",
        "last_successful_scan_at",
        "created_at",
        "updated_at",
    ),
    "gmail_messages": (
        "id",
        "account_id",
        "gmail_message_id",
        "gmail_thread_id",
        "internal_date_ms",
        "status",
        "discovered_at",
        "updated_at",
        "processed_at",
        "last_error_code",
    ),
    "newsletter_sources": (
        "id",
        "account_id",
        "source_type",
        "source_value",
        "decision",
        "origin",
        "confidence",
        "active",
        "created_at",
        "updated_at",
        "last_matched_at",
    ),
    "message_classifications": (
        "gmail_message_row_id",
        "verdict",
        "score",
        "reasons_json",
        "classifier_version",
        "classified_at",
    ),
    "message_contents": (
        "gmail_message_row_id",
        "text_content",
        "links_json",
        "source_mime_type",
        "extracted_at",
        "expires_at",
    ),
}
SCHEMA_VERSION_1_INDEXES = frozenset(
    {
        "idx_gmail_messages_account_date",
        "idx_gmail_messages_account_status",
        "idx_message_classifications_verdict",
        "idx_message_contents_expires_at",
        "idx_newsletter_sources_account_active",
    }
)
SCHEMA_VERSION_2_TRIGGERS = frozenset(
    {
        "validate_oauth_credentials_insert",
        "validate_oauth_credentials_update",
        "validate_ingestion_state_insert",
        "validate_ingestion_state_update",
        "validate_gmail_messages_insert",
        "validate_gmail_messages_update",
        "validate_newsletter_sources_insert",
        "validate_newsletter_sources_update",
        "validate_message_classifications_insert",
        "validate_message_classifications_update",
        "validate_message_classifications_delete",
        "validate_message_contents_insert",
        "validate_message_contents_update",
    }
)
SCHEMA_VERSION_3_COLUMNS = {
    "ingestion_runs": (
        "id",
        "account_id",
        "status",
        "started_at",
        "finished_at",
        "cursor_before",
        "cursor_after",
        "listed_count",
        "discovered_count",
        "already_known_count",
        "ignored_before_start_count",
        "newsletter_count",
        "review_count",
        "not_newsletter_count",
        "purged_content_count",
        "error_code",
    ),
    "message_status_history": (
        "id",
        "gmail_message_row_id",
        "from_status",
        "to_status",
        "changed_at",
        "error_code",
    ),
    "message_classification_history": (
        "id",
        "gmail_message_row_id",
        "verdict",
        "score",
        "reasons_json",
        "classifier_version",
        "classified_at",
    ),
}
SCHEMA_VERSION_3_INDEXES = frozenset(
    {
        "idx_ingestion_runs_account_started",
        "idx_message_status_history_message_changed",
        "idx_message_classification_history_message_classified",
    }
)
SCHEMA_VERSION_3_TRIGGERS = frozenset(
    {
        "validate_ingestion_runs_insert",
        "validate_ingestion_runs_update",
        "validate_message_status_history_insert",
        "validate_message_status_history_update",
        "validate_message_classification_history_insert",
        "validate_message_classification_history_update",
        "record_gmail_message_status_insert",
        "record_gmail_message_status_update",
        "record_message_classification_insert",
        "record_message_classification_update",
    }
)
SCHEMA_VERSION_4_COLUMNS = {
    "ingestion_runs": (
        *SCHEMA_VERSION_3_COLUMNS["ingestion_runs"],
        "lease_heartbeat_at",
        "lease_expires_at",
    ),
}
SCHEMA_VERSION_4_INDEXES = frozenset(
    {
        "idx_ingestion_runs_single_active_account",
        "idx_ingestion_runs_lease_expiry",
    }
)
SCHEMA_VERSION_4_TRIGGERS = frozenset(
    {
        "validate_ingestion_run_lease_insert",
        "validate_ingestion_run_lease_update",
    }
)


class DatabaseSchemaError(RuntimeError):
    """Raised when a stored schema cannot be migrated or safely loaded."""


def _invalid_utc_timestamp_sql(
    column_name: str,
    *,
    nullable: bool = False,
) -> str:
    """Build a trusted trigger predicate for one UTC timestamp column.

    Args:
        column_name: Trusted schema column referenced through ``NEW``.
        nullable: Whether ``NULL`` is a valid stored value.

    Returns:
        SQL that is true when the candidate timestamp is not a parseable
        ISO-8601 value with an explicit UTC offset.
    """

    invalid_expression = (
        "("
        f"typeof(NEW.{column_name}) <> 'text' "
        f"OR NEW.{column_name} NOT GLOB "
        "'????-??-??T??:??:??*+00:00' "
        f"OR julianday(NEW.{column_name}) IS NULL "
        f"OR substr(NEW.{column_name}, 1, 19) <> "
        "strftime('%Y-%m-%dT%H:%M:%S', "
        f"NEW.{column_name})"
        ")"
    )

    if nullable:
        return (
            f"(NEW.{column_name} IS NOT NULL "
            f"AND {invalid_expression})"
        )

    return invalid_expression


def _create_validation_trigger(
    connection: sqlite3.Connection,
    *,
    name: str,
    event: str,
    table_name: str,
    invalid_when: str,
    error_message: str,
) -> None:
    """Create one idempotent abort trigger from trusted migration SQL.

    Args:
        connection: Open migration connection receiving the trigger.
        name: Stable trigger name checked during startup.
        event: SQLite trigger event: ``INSERT``, ``UPDATE``, or ``DELETE``.
        table_name: Trusted table governed by the trigger.
        invalid_when: SQL predicate identifying invalid rows.
        error_message: Non-sensitive SQLite abort message.
    """

    connection.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS "{name}"
        BEFORE {event} ON "{table_name}"
        WHEN {invalid_when}
        BEGIN
            SELECT RAISE(
                ABORT,
                '{error_message}'
            );
        END
        """
    )


class Database:
    """Manage SQLite connections and initialize the NewsPulse schema.

    Attributes:
        path: Filesystem location of the SQLite database.
        logger: Optional application logger receiving safe migration milestones.
    """

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        logger: AppLogger | None = None,
    ) -> None:
        """Store the database path without opening a connection.

        Args:
            path: Filesystem location of the SQLite database.
            busy_timeout_ms: Positive maximum wait for a competing writer.
            logger: Optional configured application logger used for migration
                milestones without exposing the database path or stored data.

        Raises:
            ValueError: If ``busy_timeout_ms`` is not a positive integer.
        """

        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms <= 0
        ):
            raise ValueError(
                "busy_timeout_ms must be a positive integer"
            )

        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self.logger = logger

    def _prepare_storage_directory(self) -> None:
        """Create and restrict the database storage directory.

        The configured parent directory is treated as private NewsPulse storage.
        POSIX permissions are enforced after creation so an existing permissive
        directory is hardened before SQLite opens the database file.

        Raises:
            OSError: If the directory cannot be created or its POSIX permissions
                cannot be restricted to the current owner.
        """

        self.path.parent.mkdir(
            mode=PRIVATE_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )

        if os.name == "posix":
            self.path.parent.chmod(
                PRIVATE_DIRECTORY_MODE
            )

    def _restrict_database_file(self) -> None:
        """Restrict an existing database file to its POSIX owner.

        Raises:
            OSError: If the file permissions cannot be changed on POSIX.

        Notes:
            Windows access control is managed by the operating system and is not
            represented by POSIX mode bits, so no chmod operation is attempted
            there.
        """

        if os.name == "posix":
            self.path.chmod(
                PRIVATE_DATABASE_MODE
            )

    @contextmanager
    def connect(
        self,
    ) -> Iterator[sqlite3.Connection]:
        """Yield a transactional SQLite connection and always close it.

        Yields:
            A connection whose rows support name-based access and whose foreign
            keys are enforced.

        Raises:
            OSError: If the database file permissions cannot be restricted.
            sqlite3.Error: If SQLite cannot open, configure, commit, or roll back
                the database.

        Notes:
            Leaving the context normally commits the transaction. An exception
            escaping the context rolls it back. The connection is closed in both
            cases and must not be reused afterward.
        """

        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
        )

        try:
            self._restrict_database_file()
            connection.row_factory = sqlite3.Row
            connection.execute(
                "PRAGMA foreign_keys = ON"
            )
            connection.execute(
                f"PRAGMA busy_timeout = {self.busy_timeout_ms}"
            )

            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _read_schema_version(
        connection: sqlite3.Connection,
    ) -> int:
        """Return the schema version stored in SQLite.

        Args:
            connection: Open database connection used for the PRAGMA query.

        Returns:
            The non-negative integer stored in ``PRAGMA user_version``.

        Raises:
            DatabaseSchemaError: If SQLite returns an invalid version value.
        """

        row = connection.execute(
            "PRAGMA user_version"
        ).fetchone()

        if (
            row is None
            or isinstance(row[0], bool)
            or not isinstance(row[0], int)
            or row[0] < 0
        ):
            raise DatabaseSchemaError(
                "SQLite returned an invalid schema version"
            )

        return row[0]

    @staticmethod
    def _validate_version_1_schema(
        connection: sqlite3.Connection,
    ) -> None:
        """Verify that the version 1 tables, indexes, and relations exist.

        Args:
            connection: Open database connection containing the migrated schema.

        Raises:
            DatabaseSchemaError: If columns, indexes, or foreign-key relations
                do not match the supported version 1 structure.
        """

        for table_name, expected_columns in (
            SCHEMA_VERSION_1_COLUMNS.items()
        ):
            actual_columns = tuple(
                row["name"]
                for row in connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                )
            )

            if actual_columns != expected_columns:
                raise DatabaseSchemaError(
                    "SQLite schema version 1 has an "
                    "incompatible table structure"
                )

        actual_indexes = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
                """
            )
        }

        if not SCHEMA_VERSION_1_INDEXES.issubset(
            actual_indexes
        ):
            raise DatabaseSchemaError(
                "SQLite schema version 1 is missing "
                "required indexes"
            )

        if connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchone() is not None:
            raise DatabaseSchemaError(
                "SQLite schema contains foreign-key violations"
            )

    @staticmethod
    def _is_canonical_utc_timestamp(
        value: object,
    ) -> bool:
        """Return whether a value is canonical timezone-aware UTC text.

        Args:
            value: Stored value expected to use Python's ISO-8601 format.

        Returns:
            ``True`` only for parseable timestamps whose explicit offset is
            ``+00:00`` and whose text matches the canonical representation
            emitted by :meth:`datetime.isoformat`.
        """

        if (
            not isinstance(value, str)
            or not value.endswith("+00:00")
        ):
            return False

        try:
            parsed_value = datetime.fromisoformat(
                value
            )
        except ValueError:
            return False

        return (
            parsed_value.utcoffset()
            == timedelta(0)
            and parsed_value.isoformat()
            == value
        )

    @classmethod
    def _validate_version_2_data(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Validate rows governed by the version 2 data invariants.

        Args:
            connection: Open connection containing the candidate schema.

        Raises:
            DatabaseSchemaError: If a timestamp, JSON array, lifecycle state,
                or newsletter-content relation is invalid.

        Notes:
            This validation protects upgrades containing rows written before
            the version 2 triggers existed. Error messages deliberately name
            only the table and invariant and never include stored user data.
        """

        timestamp_fields = {
            "oauth_credentials": (
                ("connected_at", False),
                ("updated_at", False),
            ),
            "ingestion_state": (
                ("started_at", False),
                ("last_successful_scan_at", True),
                ("created_at", False),
                ("updated_at", False),
            ),
            "gmail_messages": (
                ("discovered_at", False),
                ("updated_at", False),
                ("processed_at", True),
            ),
            "newsletter_sources": (
                ("created_at", False),
                ("updated_at", False),
                ("last_matched_at", True),
            ),
            "message_classifications": (
                ("classified_at", False),
            ),
            "message_contents": (
                ("extracted_at", False),
                ("expires_at", False),
            ),
        }

        for table_name, fields in timestamp_fields.items():
            selected_fields = ", ".join(
                field_name
                for field_name, _ in fields
            )

            for row in connection.execute(
                f'SELECT {selected_fields} FROM "{table_name}"'
            ):
                for field_name, nullable in fields:
                    value = row[field_name]

                    if nullable and value is None:
                        continue

                    if not cls._is_canonical_utc_timestamp(
                        value
                    ):
                        raise DatabaseSchemaError(
                            "SQLite schema version 2 contains "
                            f"an invalid UTC timestamp in {table_name}"
                        )

        json_fields = (
            (
                "message_classifications",
                "reasons_json",
            ),
            (
                "message_contents",
                "links_json",
            ),
        )

        for table_name, field_name in json_fields:
            for row in connection.execute(
                f'SELECT "{field_name}" FROM "{table_name}"'
            ):
                try:
                    values = json.loads(
                        row[field_name]
                    )
                except (
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    raise DatabaseSchemaError(
                        "SQLite schema version 2 contains "
                        f"invalid JSON in {table_name}"
                    ) from exc

                if (
                    not isinstance(values, list)
                    or any(
                        not isinstance(value, str)
                        or not value.strip()
                        for value in values
                    )
                ):
                    raise DatabaseSchemaError(
                        "SQLite schema version 2 requires "
                        f"a JSON string array in {table_name}"
                    )

        invalid_lifecycle = connection.execute(
            """
            SELECT 1
            FROM gmail_messages
            WHERE NOT (
                (
                    status IN ('discovered', 'processing')
                    AND processed_at IS NULL
                    AND last_error_code IS NULL
                )
                OR (
                    status IN ('processed', 'skipped')
                    AND processed_at IS NOT NULL
                    AND last_error_code IS NULL
                )
                OR (
                    status = 'failed'
                    AND processed_at IS NULL
                    AND typeof(last_error_code) = 'text'
                    AND length(trim(last_error_code))
                        BETWEEN 1 AND 100
                )
            )
            LIMIT 1
            """
        ).fetchone()

        if invalid_lifecycle is not None:
            raise DatabaseSchemaError(
                "SQLite schema version 2 contains an "
                "incoherent message lifecycle"
            )

        invalid_content = connection.execute(
            """
            SELECT 1
            FROM message_contents AS content
            LEFT JOIN message_classifications AS classification
                ON classification.gmail_message_row_id =
                    content.gmail_message_row_id
            WHERE classification.verdict IS NULL
               OR classification.verdict <> 'newsletter'
               OR julianday(content.expires_at)
                    <= julianday(content.extracted_at)
            LIMIT 1
            """
        ).fetchone()

        if invalid_content is not None:
            raise DatabaseSchemaError(
                "SQLite schema version 2 contains content "
                "without a valid newsletter classification"
            )

    @classmethod
    def _validate_version_2_schema(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Verify the current structure, triggers, and stored invariants.

        Args:
            connection: Open database connection containing version 2.

        Raises:
            DatabaseSchemaError: If required version 1 structures, version 2
                triggers, or existing data invariants are missing or invalid.
        """

        cls._validate_version_1_schema(
            connection
        )

        actual_triggers = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'trigger'
                """
            )
        }

        if not SCHEMA_VERSION_2_TRIGGERS.issubset(
            actual_triggers
        ):
            raise DatabaseSchemaError(
                "SQLite schema version 2 is missing "
                "required data-integrity triggers"
            )

        cls._validate_version_2_data(
            connection
        )

    @classmethod
    def _validate_version_3_data(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Validate operational and message-history rows for schema 3.

        Args:
            connection: Open connection containing the history tables.

        Raises:
            DatabaseSchemaError: If timestamps, JSON values, run lifecycles, or
                the relationship between snapshots and histories is invalid.
        """

        timestamp_fields = {
            "ingestion_runs": (
                ("started_at", False),
                ("finished_at", True),
                ("cursor_before", True),
                ("cursor_after", True),
            ),
            "message_status_history": (
                ("changed_at", False),
            ),
            "message_classification_history": (
                ("classified_at", False),
            ),
        }

        for table_name, fields in timestamp_fields.items():
            selected_fields = ", ".join(
                field_name
                for field_name, _ in fields
            )

            for row in connection.execute(
                f'SELECT {selected_fields} FROM "{table_name}"'
            ):
                for field_name, nullable in fields:
                    value = row[field_name]

                    if nullable and value is None:
                        continue

                    if not cls._is_canonical_utc_timestamp(
                        value
                    ):
                        raise DatabaseSchemaError(
                            "SQLite schema version 3 contains "
                            f"an invalid UTC timestamp in {table_name}"
                        )

        for row in connection.execute(
            """
            SELECT reasons_json
            FROM message_classification_history
            """
        ):
            try:
                reasons = json.loads(
                    row["reasons_json"]
                )
            except (
                TypeError,
                ValueError,
                UnicodeError,
            ) as exc:
                raise DatabaseSchemaError(
                    "SQLite schema version 3 contains invalid "
                    "classification-history JSON"
                ) from exc

            if (
                not isinstance(reasons, list)
                or any(
                    not isinstance(reason, str)
                    or not reason.strip()
                    for reason in reasons
                )
            ):
                raise DatabaseSchemaError(
                    "SQLite schema version 3 requires JSON string "
                    "arrays in classification history"
                )

        invalid_run = connection.execute(
            """
            SELECT 1
            FROM ingestion_runs
            WHERE NOT (
                (
                    status = 'running'
                    AND finished_at IS NULL
                    AND cursor_after IS NULL
                    AND error_code IS NULL
                )
                OR (
                    status = 'succeeded'
                    AND finished_at IS NOT NULL
                    AND cursor_before IS NOT NULL
                    AND cursor_after IS NOT NULL
                    AND error_code IS NULL
                    AND julianday(finished_at)
                        >= julianday(started_at)
                    AND julianday(cursor_after)
                        >= julianday(cursor_before)
                )
                OR (
                    status = 'failed'
                    AND finished_at IS NOT NULL
                    AND cursor_after IS NULL
                    AND typeof(error_code) = 'text'
                    AND length(trim(error_code)) BETWEEN 1 AND 100
                    AND julianday(finished_at)
                        >= julianday(started_at)
                )
            )
            LIMIT 1
            """
        ).fetchone()

        if invalid_run is not None:
            raise DatabaseSchemaError(
                "SQLite schema version 3 contains an "
                "incoherent ingestion run"
            )

        invalid_status_history = connection.execute(
            """
            SELECT 1
            FROM message_status_history
            WHERE (
                to_status = 'failed'
                AND (
                    typeof(error_code) <> 'text'
                    OR length(trim(error_code)) NOT BETWEEN 1 AND 100
                )
            )
            OR (
                to_status <> 'failed'
                AND error_code IS NOT NULL
            )
            LIMIT 1
            """
        ).fetchone()

        if invalid_status_history is not None:
            raise DatabaseSchemaError(
                "SQLite schema version 3 contains an "
                "incoherent message-status history"
            )

        missing_status_history = connection.execute(
            """
            SELECT 1
            FROM gmail_messages AS message
            WHERE NOT EXISTS (
                SELECT 1
                FROM message_status_history AS history
                WHERE history.gmail_message_row_id = message.id
            )
            LIMIT 1
            """
        ).fetchone()

        if missing_status_history is not None:
            raise DatabaseSchemaError(
                "SQLite schema version 3 is missing message-status history"
            )

        missing_classification_history = connection.execute(
            """
            SELECT 1
            FROM message_classifications AS current
            WHERE NOT EXISTS (
                SELECT 1
                FROM message_classification_history AS history
                WHERE history.gmail_message_row_id =
                    current.gmail_message_row_id
                  AND history.verdict = current.verdict
                  AND history.score = current.score
                  AND history.reasons_json = current.reasons_json
                  AND history.classifier_version =
                    current.classifier_version
                  AND history.classified_at = current.classified_at
            )
            LIMIT 1
            """
        ).fetchone()

        if missing_classification_history is not None:
            raise DatabaseSchemaError(
                "SQLite schema version 3 is missing "
                "classification history"
            )

    @classmethod
    def _validate_version_3_schema(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Verify all current structures, triggers, and history invariants.

        Args:
            connection: Open database connection containing version 3.

        Raises:
            DatabaseSchemaError: If any prior schema requirement or version 3
                history structure, trigger, or stored invariant is invalid.
        """

        cls._validate_version_2_schema(
            connection
        )

        for table_name, expected_columns in (
            SCHEMA_VERSION_3_COLUMNS.items()
        ):
            actual_columns = tuple(
                row["name"]
                for row in connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                )
            )

            if actual_columns[:len(expected_columns)] != expected_columns:
                raise DatabaseSchemaError(
                    "SQLite schema version 3 has an "
                    "incompatible history table structure"
                )

        actual_indexes = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
                """
            )
        }

        if not SCHEMA_VERSION_3_INDEXES.issubset(
            actual_indexes
        ):
            raise DatabaseSchemaError(
                "SQLite schema version 3 is missing "
                "required history indexes"
            )

        actual_triggers = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'trigger'
                """
            )
        }

        if not SCHEMA_VERSION_3_TRIGGERS.issubset(
            actual_triggers
        ):
            raise DatabaseSchemaError(
                "SQLite schema version 3 is missing "
                "required history triggers"
            )

        cls._validate_version_3_data(
            connection
        )

    @classmethod
    def _validate_version_4_data(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Validate the durable single-writer lease stored by schema 4.

        Args:
            connection: Open connection containing version 4 structures.

        Raises:
            DatabaseSchemaError: If a lease timestamp or lifecycle is invalid,
                or more than one active run exists for the account.
        """

        for row in connection.execute(
            """
            SELECT
                status,
                lease_heartbeat_at,
                lease_expires_at
            FROM ingestion_runs
            """
        ):
            heartbeat = row["lease_heartbeat_at"]
            expires = row["lease_expires_at"]

            if row["status"] == "running":
                if (
                    not cls._is_canonical_utc_timestamp(
                        heartbeat
                    )
                    or not cls._is_canonical_utc_timestamp(
                        expires
                    )
                    or datetime.fromisoformat(expires)
                    <= datetime.fromisoformat(heartbeat)
                ):
                    raise DatabaseSchemaError(
                        "SQLite schema version 4 contains an invalid "
                        "active ingestion lease"
                    )
            elif heartbeat is not None or expires is not None:
                raise DatabaseSchemaError(
                    "SQLite schema version 4 retains a lease for a "
                    "finished ingestion run"
                )

        duplicate_active_run = connection.execute(
            """
            SELECT account_id
            FROM ingestion_runs
            WHERE status = 'running'
            GROUP BY account_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()

        if duplicate_active_run is not None:
            raise DatabaseSchemaError(
                "SQLite schema version 4 contains concurrent active runs"
            )

    @classmethod
    def _validate_version_4_schema(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Verify current schema structures and lease invariants.

        Args:
            connection: Open database connection containing version 4.

        Raises:
            DatabaseSchemaError: If a prior requirement or version 4 lease
                structure is absent or incompatible.
        """

        cls._validate_version_3_schema(connection)

        for table_name, expected_columns in (
            SCHEMA_VERSION_4_COLUMNS.items()
        ):
            actual_columns = tuple(
                row["name"]
                for row in connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                )
            )

            if actual_columns != expected_columns:
                raise DatabaseSchemaError(
                    "SQLite schema version 4 has an incompatible "
                    "lease table structure"
                )

        actual_indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        actual_triggers = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }

        if not SCHEMA_VERSION_4_INDEXES.issubset(actual_indexes):
            raise DatabaseSchemaError(
                "SQLite schema version 4 is missing lease indexes"
            )

        if not SCHEMA_VERSION_4_TRIGGERS.issubset(actual_triggers):
            raise DatabaseSchemaError(
                "SQLite schema version 4 is missing lease triggers"
            )

        cls._validate_version_4_data(connection)

    def initialize(self) -> None:
        """Create the database directory, tables, constraints, and indexes.

        Raises:
            DatabaseSchemaError: If the schema is newer than this application,
                no ordered migration exists, or structural validation fails.
            OSError: If the database parent directory cannot be created.
            sqlite3.Error: If schema initialization fails.

        Notes:
            Pending migrations run in order inside one immediate transaction.
            Existing unversioned databases are validated before becoming the
            current version. Version 2 adds data-integrity triggers, version 3
            adds operational histories, and version 4 adds a durable ingestion
            lease without rebuilding the original application tables.
        """

        self._prepare_storage_directory()

        with self.connect() as connection:
            def apply_version_1() -> None:
                """Create the original NewsPulse schema idempotently."""

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS oauth_credentials (
                        id INTEGER PRIMARY KEY
                            CHECK (id = 1),

                        email TEXT NOT NULL,

                        credentials_encrypted BLOB NOT NULL,

                        revoked INTEGER NOT NULL DEFAULT 0
                            CHECK (revoked IN (0, 1)),

                        connected_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ingestion_state (
                        account_id INTEGER PRIMARY KEY
                            CHECK (account_id = 1),

                        started_at TEXT NOT NULL,
                        last_successful_scan_at TEXT,

                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,

                        FOREIGN KEY (account_id)
                            REFERENCES oauth_credentials (id)
                            ON DELETE CASCADE
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS gmail_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,

                        account_id INTEGER NOT NULL
                            CHECK (account_id = 1),

                        gmail_message_id TEXT NOT NULL,
                        gmail_thread_id TEXT NOT NULL,

                        internal_date_ms INTEGER NOT NULL
                            CHECK (internal_date_ms >= 0),

                        status TEXT NOT NULL DEFAULT 'discovered'
                            CHECK (
                                status IN (
                                    'discovered',
                                    'processing',
                                    'processed',
                                    'skipped',
                                    'failed'
                                )
                            ),

                        discovered_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        processed_at TEXT,
                        last_error_code TEXT,

                        UNIQUE (account_id, gmail_message_id),

                        FOREIGN KEY (account_id)
                            REFERENCES oauth_credentials (id)
                            ON DELETE CASCADE
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS newsletter_sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,

                        account_id INTEGER NOT NULL
                            CHECK (account_id = 1),

                        source_type TEXT NOT NULL
                            CHECK (
                                source_type IN (
                                    'sender',
                                    'list_id',
                                    'gmail_label',
                                    'domain'
                                )
                            ),

                        source_value TEXT NOT NULL
                            CHECK (
                                length(trim(source_value)) > 0
                            ),

                        decision TEXT NOT NULL
                            CHECK (
                                decision IN (
                                    'include',
                                    'exclude'
                                )
                            ),

                        origin TEXT NOT NULL
                            CHECK (
                                origin IN (
                                    'manual',
                                    'confirmed',
                                    'automatic'
                                )
                            ),

                        confidence INTEGER NOT NULL DEFAULT 100
                            CHECK (
                                confidence BETWEEN 0 AND 100
                            ),

                        active INTEGER NOT NULL DEFAULT 1
                            CHECK (active IN (0, 1)),

                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_matched_at TEXT,

                        UNIQUE (
                            account_id,
                            source_type,
                            source_value
                        ),

                        FOREIGN KEY (account_id)
                            REFERENCES oauth_credentials (id)
                            ON DELETE CASCADE
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_newsletter_sources_account_active
                    ON newsletter_sources (
                        account_id,
                        active,
                        decision
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                        message_classifications (
                            gmail_message_row_id INTEGER
                                PRIMARY KEY,

                            verdict TEXT NOT NULL
                                CHECK (
                                    verdict IN (
                                        'newsletter',
                                        'review',
                                        'not_newsletter'
                                    )
                                ),

                            score INTEGER NOT NULL
                                CHECK (score >= 0),

                            reasons_json TEXT NOT NULL
                                CHECK (
                                    length(
                                        trim(reasons_json)
                                    ) > 0
                                ),

                            classifier_version TEXT NOT NULL
                                CHECK (
                                    length(
                                        trim(classifier_version)
                                    ) > 0
                                ),

                            classified_at TEXT NOT NULL,

                            FOREIGN KEY (
                                gmail_message_row_id
                            )
                                REFERENCES gmail_messages (id)
                                ON DELETE CASCADE
                        )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                        message_contents (
                            gmail_message_row_id INTEGER
                                PRIMARY KEY,

                            text_content TEXT NOT NULL
                                CHECK (
                                    length(
                                        trim(text_content)
                                    ) > 0
                                ),

                            links_json TEXT NOT NULL
                                CHECK (
                                    length(
                                        trim(links_json)
                                    ) > 0
                                ),

                            source_mime_type TEXT NOT NULL
                                CHECK (
                                    source_mime_type IN (
                                        'text/plain',
                                        'text/html'
                                    )
                                ),

                            extracted_at TEXT NOT NULL,
                            expires_at TEXT NOT NULL,

                            CHECK (
                                expires_at > extracted_at
                            ),

                            FOREIGN KEY (
                                gmail_message_row_id
                            )
                                REFERENCES gmail_messages (id)
                                ON DELETE CASCADE
                        )
                    """
                )

                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_message_contents_expires_at
                    ON message_contents (
                        expires_at
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_message_classifications_verdict
                    ON message_classifications (
                        verdict,
                        classified_at
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_gmail_messages_account_status
                    ON gmail_messages (account_id, status)
                    """
                )

                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_gmail_messages_account_date
                    ON gmail_messages (
                        account_id,
                        internal_date_ms
                    )
                    """
                )

            def apply_version_2() -> None:
                """Add row-level JSON, UTC, lifecycle, and content guards."""

                self._validate_version_1_schema(
                    connection
                )
                self._validate_version_2_data(
                    connection
                )

                timestamp_rules = {
                    "oauth_credentials": (
                        _invalid_utc_timestamp_sql(
                            "connected_at"
                        ),
                        _invalid_utc_timestamp_sql(
                            "updated_at"
                        ),
                    ),
                    "ingestion_state": (
                        _invalid_utc_timestamp_sql(
                            "started_at"
                        ),
                        _invalid_utc_timestamp_sql(
                            "last_successful_scan_at",
                            nullable=True,
                        ),
                        _invalid_utc_timestamp_sql(
                            "created_at"
                        ),
                        _invalid_utc_timestamp_sql(
                            "updated_at"
                        ),
                    ),
                    "newsletter_sources": (
                        _invalid_utc_timestamp_sql(
                            "created_at"
                        ),
                        _invalid_utc_timestamp_sql(
                            "updated_at"
                        ),
                        _invalid_utc_timestamp_sql(
                            "last_matched_at",
                            nullable=True,
                        ),
                    ),
                }

                for table_name, rules in timestamp_rules.items():
                    invalid_when = " OR ".join(
                        rules
                    )

                    for event in ("INSERT", "UPDATE"):
                        _create_validation_trigger(
                            connection,
                            name=(
                                f"validate_{table_name}_"
                                f"{event.casefold()}"
                            ),
                            event=event,
                            table_name=table_name,
                            invalid_when=invalid_when,
                            error_message=(
                                "invalid UTC timestamp"
                            ),
                        )

                message_invalid_when = " OR ".join(
                    (
                        _invalid_utc_timestamp_sql(
                            "discovered_at"
                        ),
                        _invalid_utc_timestamp_sql(
                            "updated_at"
                        ),
                        _invalid_utc_timestamp_sql(
                            "processed_at",
                            nullable=True,
                        ),
                        """
                        NOT (
                            (
                                NEW.status IN (
                                    'discovered',
                                    'processing'
                                )
                                AND NEW.processed_at IS NULL
                                AND NEW.last_error_code IS NULL
                            )
                            OR (
                                NEW.status IN (
                                    'processed',
                                    'skipped'
                                )
                                AND NEW.processed_at IS NOT NULL
                                AND NEW.last_error_code IS NULL
                            )
                            OR (
                                NEW.status = 'failed'
                                AND NEW.processed_at IS NULL
                                AND typeof(
                                    NEW.last_error_code
                                ) = 'text'
                                AND length(
                                    trim(NEW.last_error_code)
                                ) BETWEEN 1 AND 100
                            )
                        )
                        """,
                    )
                )

                for event in ("INSERT", "UPDATE"):
                    _create_validation_trigger(
                        connection,
                        name=(
                            "validate_gmail_messages_"
                            f"{event.casefold()}"
                        ),
                        event=event,
                        table_name="gmail_messages",
                        invalid_when=message_invalid_when,
                        error_message=(
                            "invalid message lifecycle"
                        ),
                    )

                classification_json_invalid = """
                    CASE
                        WHEN json_valid(NEW.reasons_json)
                         AND json_type(NEW.reasons_json) = 'array'
                        THEN EXISTS (
                            SELECT 1
                            FROM json_each(NEW.reasons_json)
                            WHERE type <> 'text'
                               OR length(trim(value)) = 0
                        )
                        ELSE 1
                    END
                """
                classification_invalid_when = " OR ".join(
                    (
                        _invalid_utc_timestamp_sql(
                            "classified_at"
                        ),
                        classification_json_invalid,
                        """
                        (
                            NEW.verdict <> 'newsletter'
                            AND EXISTS (
                                SELECT 1
                                FROM message_contents
                                WHERE gmail_message_row_id =
                                    NEW.gmail_message_row_id
                            )
                        )
                        """,
                    )
                )

                for event in ("INSERT", "UPDATE"):
                    _create_validation_trigger(
                        connection,
                        name=(
                            "validate_message_classifications_"
                            f"{event.casefold()}"
                        ),
                        event=event,
                        table_name="message_classifications",
                        invalid_when=(
                            classification_invalid_when
                        ),
                        error_message=(
                            "invalid message classification"
                        ),
                    )

                _create_validation_trigger(
                    connection,
                    name=(
                        "validate_message_classifications_delete"
                    ),
                    event="DELETE",
                    table_name="message_classifications",
                    invalid_when="""
                        EXISTS (
                            SELECT 1
                            FROM message_contents
                            WHERE gmail_message_row_id =
                                OLD.gmail_message_row_id
                        )
                    """,
                    error_message=(
                        "classification owns retained content"
                    ),
                )

                content_json_invalid = """
                    CASE
                        WHEN json_valid(NEW.links_json)
                         AND json_type(NEW.links_json) = 'array'
                        THEN EXISTS (
                            SELECT 1
                            FROM json_each(NEW.links_json)
                            WHERE type <> 'text'
                               OR length(trim(value)) = 0
                        )
                        ELSE 1
                    END
                """
                content_invalid_when = " OR ".join(
                    (
                        _invalid_utc_timestamp_sql(
                            "extracted_at"
                        ),
                        _invalid_utc_timestamp_sql(
                            "expires_at"
                        ),
                        content_json_invalid,
                        """
                        julianday(NEW.expires_at)
                            <= julianday(NEW.extracted_at)
                        """,
                        """
                        NOT EXISTS (
                            SELECT 1
                            FROM message_classifications
                            WHERE gmail_message_row_id =
                                NEW.gmail_message_row_id
                              AND verdict = 'newsletter'
                        )
                        """,
                    )
                )

                for event in ("INSERT", "UPDATE"):
                    _create_validation_trigger(
                        connection,
                        name=(
                            "validate_message_contents_"
                            f"{event.casefold()}"
                        ),
                        event=event,
                        table_name="message_contents",
                        invalid_when=content_invalid_when,
                        error_message=(
                            "invalid retained message content"
                        ),
                    )

            def apply_version_3() -> None:
                """Add durable scan, status, and classification histories."""

                self._validate_version_2_schema(
                    connection
                )

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ingestion_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,

                        account_id INTEGER NOT NULL
                            CHECK (account_id = 1),

                        status TEXT NOT NULL
                            CHECK (
                                status IN (
                                    'running',
                                    'succeeded',
                                    'failed'
                                )
                            ),

                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        cursor_before TEXT,
                        cursor_after TEXT,

                        listed_count INTEGER NOT NULL DEFAULT 0
                            CHECK (listed_count >= 0),
                        discovered_count INTEGER NOT NULL DEFAULT 0
                            CHECK (discovered_count >= 0),
                        already_known_count INTEGER NOT NULL DEFAULT 0
                            CHECK (already_known_count >= 0),
                        ignored_before_start_count INTEGER NOT NULL DEFAULT 0
                            CHECK (ignored_before_start_count >= 0),
                        newsletter_count INTEGER NOT NULL DEFAULT 0
                            CHECK (newsletter_count >= 0),
                        review_count INTEGER NOT NULL DEFAULT 0
                            CHECK (review_count >= 0),
                        not_newsletter_count INTEGER NOT NULL DEFAULT 0
                            CHECK (not_newsletter_count >= 0),
                        purged_content_count INTEGER NOT NULL DEFAULT 0
                            CHECK (purged_content_count >= 0),

                        error_code TEXT,

                        FOREIGN KEY (account_id)
                            REFERENCES oauth_credentials (id)
                            ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                        message_status_history (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,

                            gmail_message_row_id INTEGER NOT NULL,

                            from_status TEXT
                                CHECK (
                                    from_status IS NULL
                                    OR from_status IN (
                                        'discovered',
                                        'processing',
                                        'processed',
                                        'skipped',
                                        'failed'
                                    )
                                ),

                            to_status TEXT NOT NULL
                                CHECK (
                                    to_status IN (
                                        'discovered',
                                        'processing',
                                        'processed',
                                        'skipped',
                                        'failed'
                                    )
                                ),

                            changed_at TEXT NOT NULL,
                            error_code TEXT,

                            FOREIGN KEY (gmail_message_row_id)
                                REFERENCES gmail_messages (id)
                                ON DELETE CASCADE
                        )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                        message_classification_history (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,

                            gmail_message_row_id INTEGER NOT NULL,

                            verdict TEXT NOT NULL
                                CHECK (
                                    verdict IN (
                                        'newsletter',
                                        'review',
                                        'not_newsletter'
                                    )
                                ),

                            score INTEGER NOT NULL
                                CHECK (score >= 0),

                            reasons_json TEXT NOT NULL
                                CHECK (
                                    length(trim(reasons_json)) > 0
                                ),

                            classifier_version TEXT NOT NULL
                                CHECK (
                                    length(trim(classifier_version)) > 0
                                ),

                            classified_at TEXT NOT NULL,

                            FOREIGN KEY (gmail_message_row_id)
                                REFERENCES gmail_messages (id)
                                ON DELETE CASCADE
                        )
                    """
                )

                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_ingestion_runs_account_started
                    ON ingestion_runs (
                        account_id,
                        started_at DESC
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_message_status_history_message_changed
                    ON message_status_history (
                        gmail_message_row_id,
                        changed_at
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_message_classification_history_message_classified
                    ON message_classification_history (
                        gmail_message_row_id,
                        classified_at
                    )
                    """
                )

                run_invalid_when = " OR ".join(
                    (
                        _invalid_utc_timestamp_sql(
                            "started_at"
                        ),
                        _invalid_utc_timestamp_sql(
                            "finished_at",
                            nullable=True,
                        ),
                        _invalid_utc_timestamp_sql(
                            "cursor_before",
                            nullable=True,
                        ),
                        _invalid_utc_timestamp_sql(
                            "cursor_after",
                            nullable=True,
                        ),
                        """
                        NOT (
                            (
                                NEW.status = 'running'
                                AND NEW.finished_at IS NULL
                                AND NEW.cursor_after IS NULL
                                AND NEW.error_code IS NULL
                            )
                            OR (
                                NEW.status = 'succeeded'
                                AND NEW.finished_at IS NOT NULL
                                AND NEW.cursor_before IS NOT NULL
                                AND NEW.cursor_after IS NOT NULL
                                AND NEW.error_code IS NULL
                                AND julianday(NEW.finished_at)
                                    >= julianday(NEW.started_at)
                                AND julianday(NEW.cursor_after)
                                    >= julianday(NEW.cursor_before)
                            )
                            OR (
                                NEW.status = 'failed'
                                AND NEW.finished_at IS NOT NULL
                                AND NEW.cursor_after IS NULL
                                AND typeof(NEW.error_code) = 'text'
                                AND length(trim(NEW.error_code))
                                    BETWEEN 1 AND 100
                                AND julianday(NEW.finished_at)
                                    >= julianday(NEW.started_at)
                            )
                        )
                        """,
                    )
                )

                for event in ("INSERT", "UPDATE"):
                    _create_validation_trigger(
                        connection,
                        name=(
                            "validate_ingestion_runs_"
                            f"{event.casefold()}"
                        ),
                        event=event,
                        table_name="ingestion_runs",
                        invalid_when=run_invalid_when,
                        error_message=(
                            "invalid ingestion run"
                        ),
                    )

                status_history_invalid_when = " OR ".join(
                    (
                        _invalid_utc_timestamp_sql(
                            "changed_at"
                        ),
                        """
                        (
                            NEW.to_status = 'failed'
                            AND (
                                typeof(NEW.error_code) <> 'text'
                                OR length(trim(NEW.error_code))
                                    NOT BETWEEN 1 AND 100
                            )
                        )
                        """,
                        """
                        (
                            NEW.to_status <> 'failed'
                            AND NEW.error_code IS NOT NULL
                        )
                        """,
                    )
                )

                for event in ("INSERT", "UPDATE"):
                    _create_validation_trigger(
                        connection,
                        name=(
                            "validate_message_status_history_"
                            f"{event.casefold()}"
                        ),
                        event=event,
                        table_name="message_status_history",
                        invalid_when=(
                            status_history_invalid_when
                        ),
                        error_message=(
                            "invalid message-status history"
                        ),
                    )

                classification_history_invalid_when = (
                    " OR ".join(
                        (
                            _invalid_utc_timestamp_sql(
                                "classified_at"
                            ),
                            """
                            CASE
                                WHEN json_valid(NEW.reasons_json)
                                 AND json_type(NEW.reasons_json) = 'array'
                                THEN EXISTS (
                                    SELECT 1
                                    FROM json_each(NEW.reasons_json)
                                    WHERE type <> 'text'
                                       OR length(trim(value)) = 0
                                )
                                ELSE 1
                            END
                            """,
                        )
                    )
                )

                for event in ("INSERT", "UPDATE"):
                    _create_validation_trigger(
                        connection,
                        name=(
                            "validate_message_classification_history_"
                            f"{event.casefold()}"
                        ),
                        event=event,
                        table_name=(
                            "message_classification_history"
                        ),
                        invalid_when=(
                            classification_history_invalid_when
                        ),
                        error_message=(
                            "invalid classification history"
                        ),
                    )

                connection.execute(
                    """
                    INSERT INTO message_status_history (
                        gmail_message_row_id,
                        from_status,
                        to_status,
                        changed_at,
                        error_code
                    )
                    SELECT
                        message.id,
                        NULL,
                        message.status,
                        message.updated_at,
                        message.last_error_code
                    FROM gmail_messages AS message
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM message_status_history AS history
                        WHERE history.gmail_message_row_id = message.id
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO message_classification_history (
                        gmail_message_row_id,
                        verdict,
                        score,
                        reasons_json,
                        classifier_version,
                        classified_at
                    )
                    SELECT
                        current.gmail_message_row_id,
                        current.verdict,
                        current.score,
                        current.reasons_json,
                        current.classifier_version,
                        current.classified_at
                    FROM message_classifications AS current
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM message_classification_history AS history
                        WHERE history.gmail_message_row_id =
                            current.gmail_message_row_id
                          AND history.verdict = current.verdict
                          AND history.score = current.score
                          AND history.reasons_json = current.reasons_json
                          AND history.classifier_version =
                            current.classifier_version
                          AND history.classified_at =
                            current.classified_at
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS
                        record_gmail_message_status_insert
                    AFTER INSERT ON gmail_messages
                    BEGIN
                        INSERT INTO message_status_history (
                            gmail_message_row_id,
                            from_status,
                            to_status,
                            changed_at,
                            error_code
                        )
                        VALUES (
                            NEW.id,
                            NULL,
                            NEW.status,
                            NEW.updated_at,
                            NEW.last_error_code
                        );
                    END
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS
                        record_gmail_message_status_update
                    AFTER UPDATE OF
                        status,
                        processed_at,
                        last_error_code
                    ON gmail_messages
                    WHEN OLD.status IS NOT NEW.status
                      OR OLD.processed_at IS NOT NEW.processed_at
                      OR OLD.last_error_code IS NOT NEW.last_error_code
                    BEGIN
                        INSERT INTO message_status_history (
                            gmail_message_row_id,
                            from_status,
                            to_status,
                            changed_at,
                            error_code
                        )
                        VALUES (
                            NEW.id,
                            OLD.status,
                            NEW.status,
                            NEW.updated_at,
                            NEW.last_error_code
                        );
                    END
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS
                        record_message_classification_insert
                    AFTER INSERT ON message_classifications
                    BEGIN
                        INSERT INTO message_classification_history (
                            gmail_message_row_id,
                            verdict,
                            score,
                            reasons_json,
                            classifier_version,
                            classified_at
                        )
                        VALUES (
                            NEW.gmail_message_row_id,
                            NEW.verdict,
                            NEW.score,
                            NEW.reasons_json,
                            NEW.classifier_version,
                            NEW.classified_at
                        );
                    END
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS
                        record_message_classification_update
                    AFTER UPDATE ON message_classifications
                    WHEN OLD.verdict IS NOT NEW.verdict
                      OR OLD.score IS NOT NEW.score
                      OR OLD.reasons_json IS NOT NEW.reasons_json
                      OR OLD.classifier_version IS NOT
                            NEW.classifier_version
                      OR OLD.classified_at IS NOT NEW.classified_at
                    BEGIN
                        INSERT INTO message_classification_history (
                            gmail_message_row_id,
                            verdict,
                            score,
                            reasons_json,
                            classifier_version,
                            classified_at
                        )
                        VALUES (
                            NEW.gmail_message_row_id,
                            NEW.verdict,
                            NEW.score,
                            NEW.reasons_json,
                            NEW.classifier_version,
                            NEW.classified_at
                        );
                    END
                    """
                )

            def apply_version_4() -> None:
                """Add a durable, expiring single-writer ingestion lease."""

                self._validate_version_3_schema(connection)

                migration_time = datetime.now(timezone.utc).isoformat()

                connection.execute(
                    """
                    UPDATE ingestion_runs
                    SET status = 'failed',
                        finished_at = CASE
                            WHEN julianday(started_at) > julianday(?)
                                THEN started_at
                            ELSE ?
                        END,
                        cursor_after = NULL,
                        error_code = 'application_restarted'
                    WHERE status = 'running'
                    """,
                    (
                        migration_time,
                        migration_time,
                    ),
                )
                existing_columns = {
                    row["name"]
                    for row in connection.execute(
                        'PRAGMA table_info("ingestion_runs")'
                    )
                }

                if "lease_heartbeat_at" not in existing_columns:
                    connection.execute(
                        """
                        ALTER TABLE ingestion_runs
                        ADD COLUMN lease_heartbeat_at TEXT
                        """
                    )

                if "lease_expires_at" not in existing_columns:
                    connection.execute(
                        """
                        ALTER TABLE ingestion_runs
                        ADD COLUMN lease_expires_at TEXT
                        """
                    )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_ingestion_runs_single_active_account
                    ON ingestion_runs (account_id)
                    WHERE status = 'running'
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_ingestion_runs_lease_expiry
                    ON ingestion_runs (lease_expires_at)
                    WHERE status = 'running'
                    """
                )

                invalid_lease = " OR ".join(
                    (
                        """
                        (
                            NEW.status = 'running'
                            AND (
                                NEW.lease_heartbeat_at IS NULL
                                OR NEW.lease_expires_at IS NULL
                                OR
                        """
                        + _invalid_utc_timestamp_sql(
                            "lease_heartbeat_at"
                        )
                        + " OR "
                        + _invalid_utc_timestamp_sql(
                            "lease_expires_at"
                        )
                        + """
                                OR julianday(NEW.lease_expires_at)
                                    <= julianday(NEW.lease_heartbeat_at)
                            )
                        )
                        """,
                        """
                        (
                            NEW.status <> 'running'
                            AND (
                                NEW.lease_heartbeat_at IS NOT NULL
                                OR NEW.lease_expires_at IS NOT NULL
                            )
                        )
                        """,
                    )
                )

                for event in ("INSERT", "UPDATE"):
                    _create_validation_trigger(
                        connection,
                        name=(
                            "validate_ingestion_run_lease_"
                            f"{event.casefold()}"
                        ),
                        event=event,
                        table_name="ingestion_runs",
                        invalid_when=invalid_lease,
                        error_message="invalid ingestion lease",
                    )

            migrations = {
                1: apply_version_1,
                2: apply_version_2,
                3: apply_version_3,
                4: apply_version_4,
            }
            observed_version = (
                self._read_schema_version(
                    connection
                )
            )

            if observed_version > CURRENT_SCHEMA_VERSION:
                raise DatabaseSchemaError(
                    "SQLite schema is newer than this "
                    "NewsPulse version"
                )

            journal_mode_row = connection.execute(
                "PRAGMA journal_mode = WAL"
            ).fetchone()

            if (
                journal_mode_row is None
                or str(journal_mode_row[0]).casefold() != "wal"
            ):
                raise DatabaseSchemaError(
                    "SQLite WAL journal mode could not be enabled"
                )

            if observed_version == CURRENT_SCHEMA_VERSION:
                self._validate_version_4_schema(
                    connection
                )
                return

            connection.execute(
                "BEGIN IMMEDIATE"
            )
            current_version = (
                self._read_schema_version(
                    connection
                )
            )

            if current_version > CURRENT_SCHEMA_VERSION:
                raise DatabaseSchemaError(
                    "SQLite schema became newer while "
                    "waiting for the migration lock"
                )

            if current_version == CURRENT_SCHEMA_VERSION:
                self._validate_version_4_schema(
                    connection
                )
                return

            migration_started_at = perf_counter()

            if self.logger is not None:
                self.logger.info(
                    "Database schema migration started: "
                    "current_version=%d target_version=%d",
                    current_version,
                    CURRENT_SCHEMA_VERSION,
                )

            for target_version in range(
                current_version + 1,
                CURRENT_SCHEMA_VERSION + 1,
            ):
                migration = migrations.get(
                    target_version
                )

                if migration is None:
                    raise DatabaseSchemaError(
                        "No ordered migration exists for "
                        "the next schema version"
                    )

                migration()
                connection.execute(
                    "PRAGMA user_version = "
                    f"{target_version}"
                )

            self._validate_version_4_schema(
                connection
            )
            connection.commit()

            if self.logger is not None:
                self.logger.info(
                    "Database schema migration completed: "
                    "initial_version=%d final_version=%d "
                    "duration_ms=%.2f",
                    current_version,
                    CURRENT_SCHEMA_VERSION,
                    (
                        perf_counter()
                        - migration_started_at
                    )
                    * 1_000,
                )
