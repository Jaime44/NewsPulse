"""Provide the centralized, rotating file logger used by NewsPulse.

``AppLogger`` preserves the project's small convenience API while delegating
level filtering, formatting, exception rendering, handler synchronization, and
rotation to Python's standard logging architecture.
"""

from __future__ import annotations

import hashlib
import logging
import re

from contextvars import ContextVar, Token
from datetime import datetime, tzinfo
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo

from app.tools.log_rotation import LogRotationManager


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIRECTORY = PROJECT_ROOT / "logs"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
DEFAULT_LOG_LEVEL = "INFO"

_CORRELATION_ID: ContextVar[str] = ContextVar(
    "newspulse_correlation_id",
    default="-",
)
_LOGGED_ERROR_ATTRIBUTE = "_newspulse_logged"

ErrorType = TypeVar(
    "ErrorType",
    bound=BaseException,
)


def mark_error_logged(
    error: ErrorType,
) -> ErrorType:
    """Mark an exception as already recorded by its owning boundary.

    Args:
        error: Exception whose logging ownership has been established.

    Returns:
        The same exception instance, suitable for use in a ``raise`` statement.
    """

    setattr(
        error,
        _LOGGED_ERROR_ATTRIBUTE,
        True,
    )
    return error


def is_error_logged(
    error: BaseException,
) -> bool:
    """Return whether an exception has already produced its error record.

    Args:
        error: Exception crossing an application boundary.

    Returns:
        ``True`` when a lower layer already owns the corresponding error log.
    """

    return bool(
        getattr(
            error,
            _LOGGED_ERROR_ATTRIBUTE,
            False,
        )
    )


class _SafeLogFormatter(logging.Formatter):
    """Format structured context and redact common credential patterns."""

    _SENSITIVE_KEY = (
        r"authorization|cookie|set-cookie|access[_-]?token|"
        r"refresh[_-]?token|id[_-]?token|client[_-]?secret|"
        r"api[_-]?key|password|oauth[_-]?code|credentials?|"
        r"private[_-]?key"
    )
    _SENSITIVE_LINE_VALUE = re.compile(
        r"(?im)\b(?P<key>authorization|proxy-authorization|cookie|"
        r"set-cookie)\b(?P<separator>\s*[:=]\s*)[^\r\n]*"
    )
    _SENSITIVE_QUOTED_ASSIGNMENT = re.compile(
        rf"(?P<prefix>[\"']?(?:{_SENSITIVE_KEY})[\"']?"
        rf"\s*[:=]\s*)(?P<quote>[\"']).*?(?P=quote)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    _SENSITIVE_ASSIGNMENT = re.compile(
        rf"\b({_SENSITIVE_KEY})\b(\s*[:=]\s*)([^\s,;]+)",
        flags=re.IGNORECASE,
    )
    _BEARER_TOKEN = re.compile(
        r"(?i)\bbearer\s+[^\s,;]+"
    )

    def __init__(
        self,
        fmt: str,
        *,
        datefmt: str,
        log_timezone: tzinfo,
    ) -> None:
        """Configure the output template and application timezone.

        Args:
            fmt: Standard-library logging format string.
            datefmt: ``strftime`` template used for record timestamps.
            log_timezone: Timezone applied when rendering record creation time.
        """

        super().__init__(
            fmt=fmt,
            datefmt=datefmt,
        )
        self.log_timezone = log_timezone

    def formatTime(
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        """Format a record timestamp in the configured application timezone.

        Args:
            record: Log record whose creation time is rendered.
            datefmt: Optional override for the configured timestamp template.

        Returns:
            The timezone-aware timestamp text for the record.
        """

        timestamp = datetime.fromtimestamp(
            record.created,
            tz=self.log_timezone,
        )

        if datefmt:
            formatted_time = timestamp.strftime(
                datefmt
            )

            if "%f" in datefmt:
                formatted_time = formatted_time.replace(
                    timestamp.strftime("%f"),
                    f"{timestamp.microsecond // 1_000:03d}",
                )

            return formatted_time

        return timestamp.isoformat(
            timespec="seconds"
        )

    def format(self, record: logging.LogRecord) -> str:
        """Return one formatted record with known secret shapes masked.

        Args:
            record: Standard-library log record to render.

        Returns:
            Contextual log text with recognized sensitive values redacted.
        """

        record.component = getattr(
            record,
            "component",
            record.name,
        )
        record.correlation_id = _CORRELATION_ID.get()
        rendered = self._SENSITIVE_LINE_VALUE.sub(
            lambda match: (
                f"{match.group('key')}"
                f"{match.group('separator')}[REDACTED]"
            ),
            super().format(record),
        )
        rendered = self._SENSITIVE_QUOTED_ASSIGNMENT.sub(
            lambda match: (
                f"{match.group('prefix')}"
                f"{match.group('quote')}[REDACTED]"
                f"{match.group('quote')}"
            ),
            rendered,
        )
        rendered = self._BEARER_TOKEN.sub(
            "Bearer [REDACTED]",
            rendered,
        )
        return self._SENSITIVE_ASSIGNMENT.sub(
            lambda match: (
                f"{match.group(1)}"
                f"{match.group(2)}[REDACTED]"
            ),
            rendered,
        )


class AppLogger:
    """Write filtered, contextual events to one rotating project log.

    The existing ``debug``, ``info``, ``warning``, ``error``, ``critical``, and
    ``log`` methods remain available. They additionally accept standard
    deferred-interpolation arguments and ``exc_info``.

    Attributes:
        log_file: Absolute path of the active log file.
    """

    LogLevel = Literal[
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    ]

    _registry_lock = RLock()
    _loggers: dict[Path, logging.Logger] = {}
    _log_directory = DEFAULT_LOG_DIRECTORY
    _max_bytes = DEFAULT_LOG_MAX_BYTES
    _backup_count = DEFAULT_LOG_BACKUP_COUNT
    _level = DEFAULT_LOG_LEVEL
    _timezone: tzinfo = (
        datetime.now().astimezone().tzinfo
        or ZoneInfo("UTC")
    )

    def __init__(
        self,
        log_file: str,
        *,
        log_directory: Path | None = None,
        max_bytes: int | None = None,
        backup_count: int | None = None,
        level: str | int | None = None,
    ) -> None:
        """Bind one component to a shared handler for its selected file.

        Args:
            log_file: Plain file name, such as ``application.log``.
            log_directory: Optional per-instance directory override.
            max_bytes: Optional per-instance rollover size override.
            backup_count: Optional per-instance archive-count override.
            level: Optional numeric or named severity threshold.
        """

        selected_directory = Path(
            log_directory or self._log_directory
        )
        selected_max_bytes = (
            max_bytes
            if max_bytes is not None
            else self._max_bytes
        )
        selected_backup_count = (
            backup_count
            if backup_count is not None
            else self._backup_count
        )
        selected_level = self._normalize_level(
            level if level is not None else self._level
        )

        rotation = LogRotationManager(
            selected_directory,
            max_bytes=selected_max_bytes,
            backup_count=selected_backup_count,
        )
        log_path = rotation.resolve_log_path(
            log_file
        ).resolve()

        self.log_file = str(log_path)
        self.component = log_path.stem
        self._logger = self._get_or_create_logger(
            log_path=log_path,
            log_file=log_file,
            rotation=rotation,
            level=selected_level,
        )

    @classmethod
    def configure(
        cls,
        *,
        log_directory: Path,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
        backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
        level: str | int = DEFAULT_LOG_LEVEL,
        timezone: str | tzinfo | None = None,
    ) -> None:
        """Set defaults used by subsequently created application loggers.

        Args:
            log_directory: Directory for active and archived log files.
            max_bytes: Active-file size that triggers a rollover.
            backup_count: Maximum numbered archives retained per file.
            level: Minimum severity accepted by application handlers.
            timezone: IANA name or timezone used to render timestamps.

        Existing handlers are closed so repeated application-factory imports do
        not accumulate handlers or keep stale files open.
        """

        normalized_level = cls._normalize_level(
            level
        )
        normalized_timezone = cls._normalize_timezone(
            timezone
        )
        LogRotationManager(
            log_directory,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )

        with cls._registry_lock:
            cls.shutdown()
            cls._log_directory = Path(
                log_directory
            ).expanduser().resolve()
            cls._max_bytes = max_bytes
            cls._backup_count = backup_count
            cls._level = logging.getLevelName(
                normalized_level
            )
            cls._timezone = normalized_timezone

    @classmethod
    def set_correlation_id(
        cls,
        correlation_id: str,
    ) -> Token[str]:
        """Bind a non-sensitive execution identifier to the current context.

        Args:
            correlation_id: Identifier shared by one logical operation.

        Returns:
            Context token required to restore the preceding identifier.
        """

        normalized_value = (
            correlation_id.strip()
            if isinstance(correlation_id, str)
            else ""
        )
        return _CORRELATION_ID.set(
            normalized_value or "-"
        )

    @classmethod
    def reset_correlation_id(
        cls,
        token: Token[str],
    ) -> None:
        """Restore the correlation context represented by ``token``.

        Args:
            token: Token returned by :meth:`set_correlation_id`.
        """

        _CORRELATION_ID.reset(token)

    @classmethod
    def shutdown(cls) -> None:
        """Close and forget every handler owned by ``AppLogger``."""

        with cls._registry_lock:
            for logger in cls._loggers.values():
                for handler in tuple(
                    logger.handlers
                ):
                    logger.removeHandler(handler)
                    handler.close()

            cls._loggers.clear()

    @classmethod
    def _normalize_level(
        cls,
        level: str | int,
    ) -> int:
        """Return a supported standard logging level number.

        Args:
            level: Standard named or numeric logging severity.

        Returns:
            The corresponding standard-library numeric level.

        Raises:
            ValueError: If the level is not one of the five supported values.
        """

        if isinstance(level, bool):
            raise ValueError(
                "level must be a supported logging level"
            )

        if isinstance(level, int):
            if level in {
                logging.DEBUG,
                logging.INFO,
                logging.WARNING,
                logging.ERROR,
                logging.CRITICAL,
            }:
                return level

            raise ValueError(
                "level must be a supported logging level"
            )

        if not isinstance(level, str):
            raise ValueError(
                "level must be a supported logging level"
            )

        normalized_level = level.strip().upper()
        numeric_level = logging.getLevelName(
            normalized_level
        )

        if not isinstance(numeric_level, int):
            raise ValueError(
                "level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )

        return numeric_level

    @classmethod
    def _normalize_timezone(
        cls,
        value: str | tzinfo | None,
    ) -> tzinfo:
        """Return a concrete timezone for log timestamps.

        Args:
            value: IANA timezone name, timezone object, or local-time default.

        Returns:
            A concrete timezone used by the formatter.

        Raises:
            ValueError: If ``value`` is neither a name nor a timezone object.
            ZoneInfoNotFoundError: If an IANA timezone name is unknown.
        """

        if value is None:
            return (
                datetime.now().astimezone().tzinfo
                or ZoneInfo("UTC")
            )

        if isinstance(value, str):
            return ZoneInfo(value)

        if isinstance(value, tzinfo):
            return value

        raise ValueError(
            "timezone must be an IANA name or tzinfo instance"
        )

    def _get_or_create_logger(
        self,
        *,
        log_path: Path,
        log_file: str,
        rotation: LogRotationManager,
        level: int,
    ) -> logging.Logger:
        """Return the process-local logger shared by one absolute file path.

        Args:
            log_path: Absolute active-log path used as the registry key.
            log_file: Plain file name passed to the rotation manager.
            rotation: Validated handler factory for this log path.
            level: Numeric threshold applied to the logger and its handler.

        Returns:
            The existing or newly configured non-propagating logger.
        """

        with self._registry_lock:
            existing_logger = self._loggers.get(
                log_path
            )

            if existing_logger is not None:
                existing_logger.setLevel(level)

                for handler in existing_logger.handlers:
                    handler.setLevel(level)

                return existing_logger

            path_digest = hashlib.sha256(
                str(log_path).encode("utf-8")
            ).hexdigest()[:12]
            logger = logging.getLogger(
                f"newspulse.{self.component}.{path_digest}"
            )

            for stale_handler in tuple(
                logger.handlers
            ):
                logger.removeHandler(
                    stale_handler
                )
                stale_handler.close()

            logger.setLevel(level)
            logger.propagate = False

            handler = rotation.create_handler(
                log_file
            )
            handler.setLevel(level)
            handler.setFormatter(
                _SafeLogFormatter(
                    "[%(asctime)s] "
                    "[%(levelname)s] "
                    "[%(component)s:%(funcName)s] "
                    "[correlation_id=%(correlation_id)s] "
                    "%(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S.%f%z",
                    log_timezone=self._timezone,
                )
            )
            logger.addHandler(handler)
            self._loggers[log_path] = logger
            return logger

    def is_enabled_for(
        self,
        level: str | int,
    ) -> bool:
        """Return whether an event at ``level`` would be written.

        Args:
            level: Named or numeric severity to query.

        Returns:
            Whether the current threshold accepts the severity.

        Raises:
            ValueError: If the severity is unsupported.
        """

        return self._logger.isEnabledFor(
            self._normalize_level(level)
        )

    def log(
        self,
        message: str,
        level: LogLevel,
        *args: object,
        exc_info: (
            bool
            | tuple[
                type[BaseException],
                BaseException,
                TracebackType | None,
            ]
        ) = False,
        **kwargs: Any,
    ) -> None:
        """Write one event when its severity passes the configured threshold.

        Args:
            message: Logging template or complete event text.
            level: Supported uppercase severity name.
            *args: Deferred interpolation values for ``message``.
            exc_info: Optional traceback information accepted by ``logging``.
            **kwargs: Additional standard logging keyword arguments.

        Raises:
            ValueError: If ``level`` is unsupported.
        """

        supplied_extra = kwargs.pop(
            "extra",
            None,
        )
        record_context = (
            dict(supplied_extra)
            if supplied_extra is not None
            else {}
        )
        record_context["component"] = self.component
        stacklevel = kwargs.pop(
            "stacklevel",
            2,
        )

        self._logger.log(
            self._normalize_level(level),
            message,
            *args,
            exc_info=exc_info,
            extra=record_context,
            stacklevel=stacklevel,
            **kwargs,
        )

    def debug(
        self,
        message: str,
        *args: object,
        **kwargs: Any,
    ) -> None:
        """Log a detailed diagnostic event.

        Args:
            message: Logging template or complete event text.
            *args: Deferred interpolation values for ``message``.
            **kwargs: Additional standard logging keyword arguments.
        """

        kwargs.setdefault("stacklevel", 3)
        self.log(
            message,
            "DEBUG",
            *args,
            **kwargs,
        )

    def info(
        self,
        message: str,
        *args: object,
        **kwargs: Any,
    ) -> None:
        """Log a normal application event.

        Args:
            message: Logging template or complete event text.
            *args: Deferred interpolation values for ``message``.
            **kwargs: Additional standard logging keyword arguments.
        """

        kwargs.setdefault("stacklevel", 3)
        self.log(
            message,
            "INFO",
            *args,
            **kwargs,
        )

    def warning(
        self,
        message: str,
        *args: object,
        **kwargs: Any,
    ) -> None:
        """Log an unexpected but recoverable condition.

        Args:
            message: Logging template or complete event text.
            *args: Deferred interpolation values for ``message``.
            **kwargs: Additional standard logging keyword arguments.
        """

        kwargs.setdefault("stacklevel", 3)
        self.log(
            message,
            "WARNING",
            *args,
            **kwargs,
        )

    def error(
        self,
        message: str,
        *args: object,
        **kwargs: Any,
    ) -> None:
        """Log a failed operation.

        Args:
            message: Logging template or complete event text.
            *args: Deferred interpolation values for ``message``.
            **kwargs: Additional standard logging keyword arguments.
        """

        kwargs.setdefault("stacklevel", 3)
        self.log(
            message,
            "ERROR",
            *args,
            **kwargs,
        )

    def critical(
        self,
        message: str,
        *args: object,
        **kwargs: Any,
    ) -> None:
        """Log a failure that prevents an essential component from running.

        Args:
            message: Logging template or complete event text.
            *args: Deferred interpolation values for ``message``.
            **kwargs: Additional standard logging keyword arguments.
        """

        kwargs.setdefault("stacklevel", 3)
        self.log(
            message,
            "CRITICAL",
            *args,
            **kwargs,
        )


__all__ = [
    "AppLogger",
    "is_error_logged",
    "mark_error_logged",
]
