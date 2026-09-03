from __future__ import annotations

import os

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class ConfigurationError(RuntimeError):
    """Raised when application configuration is missing or invalid."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise ConfigurationError(
            f"Required environment variable is missing: {name}"
        )

    return value


def _resolve_path(name: str) -> Path:
    path = Path(_required(name)).expanduser()

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.resolve()


def _required_file(name: str) -> Path:
    path = _resolve_path(name)

    if not path.is_file():
        raise ConfigurationError(
            f"{name} does not point to an existing file: {path}"
        )

    return path


def _positive_int(name: str, default: str) -> int:
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
    value = os.getenv(name, default).strip().lower()

    if value not in {"true", "false"}:
        raise ConfigurationError(
            f"{name} must be true or false"
        )

    return value == "true"


@dataclass(frozen=True, slots=True)
class AppConfig:
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

    @classmethod
    def load(
        cls,
        env_file: Path | None = None,
    ) -> "AppConfig":
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
        )


def read_secret_file(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()

    if not value:
        raise ConfigurationError(
            f"Secret file is empty: {path}"
        )

    return value