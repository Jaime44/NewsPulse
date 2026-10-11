"""Load and validate the environment-backed NewsPulse configuration.

This module resolves application paths relative to the project root, validates
runtime settings, and exposes the immutable :class:`AppConfig` consumed by the
Flask entry point and persistence services.
"""

from __future__ import annotations

import os

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_LOG_DIRECTORY = PROJECT_ROOT / "logs"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
DEFAULT_LOG_LEVEL = "INFO"
SUPPORTED_LOG_LEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


class ConfigurationError(RuntimeError):
    """Raised when application configuration is missing or invalid."""


def _required(name: str) -> str:
    """Return a required, non-empty environment variable.

    Args:
        name: Name of the environment variable to read.

    Returns:
        The stripped environment value.

    Raises:
        ConfigurationError: If the variable is unset or empty.
    """

    value = os.getenv(name, "").strip()

    if not value:
        raise ConfigurationError(
            f"Required environment variable is missing: {name}"
        )

    return value


def _resolve_path(name: str) -> Path:
    """Resolve a configured path against the project root.

    Args:
        name: Name of the required path environment variable.

    Returns:
        An absolute, normalized path.

    Raises:
        ConfigurationError: If the variable is unset or empty.
    """

    path = Path(_required(name)).expanduser()

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.resolve()


def _optional_path(
    name: str,
    default: Path,
) -> Path:
    """Resolve an optional environment path against the project root.

    Args:
        name: Name of the environment variable to read.
        default: Path used when the variable is unset or empty.

    Returns:
        An absolute, normalized path.
    """

    raw_value = os.getenv(name, "").strip()
    path = Path(raw_value) if raw_value else default

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.expanduser().resolve()


def _required_file(name: str) -> Path:
    """Resolve a required configuration value to an existing file.

    Args:
        name: Name of the environment variable containing the path.

    Returns:
        The absolute path to the configured file.

    Raises:
        ConfigurationError: If the setting is missing or is not a file.
    """

    path = _resolve_path(name)

    if not path.is_file():
        raise ConfigurationError(
            f"{name} does not point to an existing file: {path}"
        )

    return path


def _positive_int(name: str, default: str) -> int:
    """Read a positive integer from the environment.

    Args:
        name: Name of the environment variable to read.
        default: String value used when the variable is absent.

    Returns:
        The parsed positive integer.

    Raises:
        ConfigurationError: If the value is not a positive integer.
    """

    raw_value = os.getenv(name, default).strip()

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(
            f"{name} must be an integer"
        ) from exc

    if value <= 0:
        raise ConfigurationError(
            f"{name} must be greater than zero"
        )

    return value


def _boolean(name: str, default: str = "false") -> bool:
    """Read a strict ``true`` or ``false`` environment option.

    Args:
        name: Name of the environment variable to read.
        default: String value used when the variable is absent.

    Returns:
        ``True`` only when the normalized value is ``"true"``.

    Raises:
        ConfigurationError: If the value is neither ``true`` nor ``false``.
    """

    value = os.getenv(name, default).strip().lower()

    if value not in {"true", "false"}:
        raise ConfigurationError(
            f"{name} must be true or false"
        )

    return value == "true"


def _log_level(
    name: str = "LOG_LEVEL",
    default: str = DEFAULT_LOG_LEVEL,
) -> str:
    """Read and validate a standard application logging threshold.

    Args:
        name: Name of the environment variable containing the threshold.
        default: Severity used when the environment variable is absent.

    Returns:
        The normalized uppercase logging level.

    Raises:
        ConfigurationError: If the value is not a supported severity.
    """

    value = os.getenv(name, default).strip().upper()

    if value not in SUPPORTED_LOG_LEVELS:
        raise ConfigurationError(
            f"{name} must be one of: "
            f"{', '.join(sorted(SUPPORTED_LOG_LEVELS))}"
        )

    return value


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Hold validated runtime settings for one NewsPulse process.

    Attributes:
        environment: Deployment environment name.
        host: Interface on which the development server listens.
        port: TCP port used by the development server.
        timezone: Valid IANA timezone name used by the application.
        debug: Whether Flask debug mode is enabled.
        google_credentials_path: OAuth web-client JSON file.
        google_redirect_uri: Authorized OAuth callback URI.
        google_scopes: OAuth scopes requested from Google.
        flask_secret_key_path: File containing the Flask signing key.
        token_encryption_key_path: File containing the Fernet key.
        database_path: SQLite database location.
        retention_days: Lifetime of persisted readable message content.
        log_directory: Directory containing active and archived log files.
        log_max_bytes: Active-file size that triggers log rotation.
        log_backup_count: Number of numbered archives retained per log.
        log_level: Minimum severity written by application loggers.
    """

    environment: str
    host: str
    port: int
    timezone: str
    debug: bool

    google_credentials_path: Path
    google_redirect_uri: str
    google_scopes: tuple[str, ...]

    flask_secret_key_path: Path
    token_encryption_key_path: Path

    database_path: Path
    retention_days: int
    log_directory: Path = DEFAULT_LOG_DIRECTORY
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT
    log_level: str = DEFAULT_LOG_LEVEL

    @classmethod
    def load(
        cls,
        env_file: Path | None = None,
    ) -> "AppConfig":
        """Load an environment file and validate all application settings.

        Args:
            env_file: Optional dotenv file; defaults to the project ``.env``.

        Returns:
            A fully validated immutable configuration object.

        Raises:
            ConfigurationError: If the file or any required setting is invalid.

        Notes:
            Existing process environment variables take precedence because the
            dotenv loader is called with ``override=False``.
        """

        selected_env_file = env_file or DEFAULT_ENV_FILE

        if not selected_env_file.is_file():
            raise ConfigurationError(
                f"Environment file was not found: {selected_env_file}"
            )

        load_dotenv(selected_env_file, override=False)

        timezone = _required("APP_TIMEZONE")

        try:
            ZoneInfo(timezone)
        except Exception as exc:
            raise ConfigurationError(
                f"Unknown timezone: {timezone}"
            ) from exc

        scopes = tuple(
            scope.strip()
            for scope in _required("GOOGLE_SCOPES").split(",")
            if scope.strip()
        )

        if not scopes:
            raise ConfigurationError(
                "GOOGLE_SCOPES must contain at least one scope"
            )

        database_path = _resolve_path("DATABASE_PATH")

        if not database_path.parent.is_dir():
            raise ConfigurationError(
                "Database directory does not exist: "
                f"{database_path.parent}"
            )

        port = _positive_int("APP_PORT", "5000")

        if port > 65535:
            raise ConfigurationError(
                "APP_PORT must be between 1 and 65535"
            )

        return cls(
            environment=os.getenv("APP_ENV", "development").strip(),
            host=os.getenv("APP_HOST", "127.0.0.1").strip(),
            port=port,
            timezone=timezone,
            debug=_boolean("APP_DEBUG"),
            google_credentials_path=_required_file(
                "GOOGLE_APPLICATION_CREDENTIALS_WEB_APP"
            ),
            google_redirect_uri=_required("GOOGLE_REDIRECT_URI"),
            google_scopes=scopes,
            flask_secret_key_path=_required_file(
                "FLASK_SECRET_KEY_PATH"
            ),
            token_encryption_key_path=_required_file(
                "TOKEN_ENCRYPTION_KEY_PATH"
            ),
            database_path=database_path,
            retention_days=_positive_int("RETENTION_DAYS", "30"),
            log_directory=_optional_path(
                "LOG_DIRECTORY",
                DEFAULT_LOG_DIRECTORY,
            ),
            log_max_bytes=_positive_int(
                "LOG_MAX_BYTES",
                str(DEFAULT_LOG_MAX_BYTES),
            ),
            log_backup_count=_positive_int(
                "LOG_BACKUP_COUNT",
                str(DEFAULT_LOG_BACKUP_COUNT),
            ),
            log_level=_log_level(),
        )


def read_secret_file(path: Path) -> str:
    """Read a non-empty secret from a UTF-8 text file.

    Args:
        path: File containing the secret value.

    Returns:
        The stripped secret.

    Raises:
        OSError: If the file cannot be read.
        ConfigurationError: If the file contains no non-whitespace value.
    """

    value = path.read_text(encoding="utf-8").strip()

    if not value:
        raise ConfigurationError(
            f"Secret file is empty: {path}"
        )

    return value
