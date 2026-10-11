"""Build size-based rotating handlers for NewsPulse log files.

The manager keeps rotation policy separate from :class:`AppLogger`. Its
handler extends the standard library implementation with a small file lock and
inode check so Flask's development reloader, or multiple Linux workers, do not
rotate the same file concurrently or keep writing to an archived inode.
"""

from __future__ import annotations

import os
import sys

from contextlib import contextmanager
from logging import LogRecord
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - NewsPulse deploys on Linux.
    fcntl = None


class ProcessSafeRotatingFileHandler(RotatingFileHandler):
    """Rotate one file safely across threads and POSIX processes.

    Python's :class:`RotatingFileHandler` already serializes threads that share
    one handler. This subclass adds an advisory lock shared by processes and
    reopens its stream when another process has completed a rollover.
    """

    def __init__(
        self,
        filename: Path,
        *,
        max_bytes: int,
        backup_count: int,
        encoding: str = "utf-8",
        delay: bool = True,
    ) -> None:
        """Initialize the handler and its persistent sidecar lock file.

        Args:
            filename: Active log file path.
            max_bytes: File size that triggers a rollover.
            backup_count: Maximum number of numbered archives to retain.
            encoding: Text encoding used for every log file.
            delay: Whether to wait for the first event before opening the log.
        """

        self.lock_path = Path(f"{filename}.lock")
        self._lock_stream: IO[str] | None = None
        self._lock_pid: int | None = None
        self._retention_checked = False

        super().__init__(
            filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=encoding,
            delay=delay,
        )

    @contextmanager
    def process_lock(self) -> Iterator[None]:
        """Hold the cross-process lock around one write or maintenance pass.

        Yields:
            Control while this process owns the advisory file lock.

        Notes:
            Platforms without ``fcntl`` retain standard handler thread safety
            but cannot coordinate rotation between separate processes.
        """

        lock_stream = self._lock_stream
        current_pid = os.getpid()

        if (
            lock_stream is not None
            and self._lock_pid != current_pid
        ):
            lock_stream.close()
            lock_stream = None
            self._lock_stream = None
            self._lock_pid = None

        if lock_stream is None:
            self.lock_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            lock_stream = self.lock_path.open(
                "a",
                encoding="utf-8",
            )
            self._lock_stream = lock_stream
            self._lock_pid = current_pid

        if fcntl is None:
            yield
            return

        fcntl.flock(
            lock_stream.fileno(),
            fcntl.LOCK_EX,
        )

        try:
            yield
        finally:
            fcntl.flock(
                lock_stream.fileno(),
                fcntl.LOCK_UN,
            )

    def _reopen_if_rotated(self) -> None:
        """Reopen the active file after another process rotates it."""

        if self.stream is None:
            return

        try:
            active_stat = os.stat(
                self.baseFilename
            )
        except FileNotFoundError:
            active_stat = None

        stream_stat = os.fstat(
            self.stream.fileno()
        )

        if (
            active_stat is not None
            and active_stat.st_dev == stream_stat.st_dev
            and active_stat.st_ino == stream_stat.st_ino
        ):
            return

        self.stream.flush()
        self.stream.close()
        self.stream = None

        if not self.delay:
            self.stream = self._open()

    def emit(self, record: LogRecord) -> None:
        """Write one record while holding the process-shared rotation lock.

        Args:
            record: Standard-library record to rotate and persist.

        Notes:
            Failures are delegated to :meth:`handleError`, which never renders
            the original record and therefore cannot expose its raw values.
        """

        try:
            with self.process_lock():
                if not self._retention_checked:
                    self._retention_checked = (
                        self._prune_excess_backups()
                    )

                self._reopen_if_rotated()
                super().emit(record)
        except Exception:
            self.handleError(record)

    def handleError(self, record: LogRecord) -> None:
        """Report a constant diagnostic without exposing the failed record.

        Args:
            record: Record that could not be written. Its contents are ignored.

        Notes:
            The standard logging fallback prints the original message and
            arguments when ``logging.raiseExceptions`` is true. NewsPulse uses
            a constant message instead so handler failures remain non-fatal and
            cannot bypass the normal secret-redaction formatter.
        """

        try:
            sys.stderr.write(
                "NewsPulse logging handler failure; record omitted\n"
            )
        except Exception:
            pass

    def _prune_excess_backups(self) -> bool:
        """Remove excess numeric archives and report cleanup success.

        Returns:
            ``True`` when every excess archive was removed successfully.
        """

        log_path = Path(self.baseFilename)
        prefix = f"{log_path.name}."
        cleanup_succeeded = True

        for candidate in log_path.parent.glob(
            f"{log_path.name}.*"
        ):
            suffix = candidate.name.removeprefix(
                prefix
            )

            if (
                not suffix.isdigit()
                or int(suffix) <= self.backupCount
            ):
                continue

            try:
                candidate.unlink()
            except OSError:
                cleanup_succeeded = False

        return cleanup_succeeded

    def close(self) -> None:
        """Close both the active log stream and the sidecar lock stream.

        Notes:
            Closing is idempotent and always releases the process-local handle
            to the persistent sidecar lock file.
        """

        try:
            super().close()
        finally:
            if self._lock_stream is not None:
                self._lock_stream.close()
                self._lock_stream = None
                self._lock_pid = None


class LogRotationManager:
    """Validate and apply the NewsPulse file-rotation policy.

    Args:
        log_directory: Directory containing active and archived log files.
        max_bytes: Maximum active-file size before rotation.
        backup_count: Maximum numbered archives retained per active log.

    Notes:
        Only standard numeric archives such as ``web_app.log.3`` are pruned.
        Unrelated files and pre-existing archives with other naming schemes are
        never removed.
    """

    def __init__(
        self,
        log_directory: Path,
        *,
        max_bytes: int,
        backup_count: int,
    ) -> None:
        """Store one validated rotation policy.

        Args:
            log_directory: Directory containing active and archived log files.
            max_bytes: Positive active-file size that triggers rollover.
            backup_count: Positive number of numeric archives to retain.

        Raises:
            ValueError: If either numeric policy value is invalid.
        """

        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError(
                "max_bytes must be a positive integer"
            )

        if (
            isinstance(backup_count, bool)
            or not isinstance(backup_count, int)
            or backup_count <= 0
        ):
            raise ValueError(
                "backup_count must be a positive integer"
            )

        self.log_directory = Path(
            log_directory
        ).expanduser().resolve()
        self.max_bytes = max_bytes
        self.backup_count = backup_count

    @property
    def supports_multiple_processes(self) -> bool:
        """Return whether POSIX advisory locks are available.

        Returns:
            Whether separate processes can coordinate through ``fcntl``.
        """

        return fcntl is not None

    def resolve_log_path(
        self,
        log_file: str,
    ) -> Path:
        """Return a safe path for a plain file name inside the log directory.

        Args:
            log_file: File name without directories, such as ``web_app.log``.

        Returns:
            The selected path beneath the configured log directory.

        Raises:
            ValueError: If the value is empty, absolute, or contains a path.
        """

        candidate = Path(log_file)

        if (
            not log_file.strip()
            or candidate.is_absolute()
            or candidate.name != log_file
            or log_file in {".", ".."}
        ):
            raise ValueError(
                "log_file must be a plain file name"
            )

        return self.log_directory / candidate

    def create_handler(
        self,
        log_file: str,
    ) -> ProcessSafeRotatingFileHandler:
        """Create a UTF-8 rotating handler with bounded archive retention.

        Args:
            log_file: Plain active-log file name inside the configured directory.

        Returns:
            A delayed, process-aware rotating file handler.

        Existing active logs are opened in append mode. Excess numeric
        archives are removed on the first write after the process-shared lock
        is acquired. A cleanup failure does not prevent the handler from
        accepting new log records; the next write can retry cleanup.
        """

        self.log_directory.mkdir(
            parents=True,
            exist_ok=True,
        )
        log_path = self.resolve_log_path(
            log_file
        )
        handler = ProcessSafeRotatingFileHandler(
            log_path,
            max_bytes=self.max_bytes,
            backup_count=self.backup_count,
        )

        return handler
