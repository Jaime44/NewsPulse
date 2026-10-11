"""Provide the lightweight file logger used by NewsPulse services and routes.

Each :class:`AppLogger` writes timestamped entries to a named file beneath the
project ``logs`` directory and exposes convenience methods for five severity
levels.
"""

import os
from datetime import datetime
from typing import Literal

class AppLogger:
    """Append timestamped application events to a project-local log file.

    Attributes:
        log_file: Absolute path to the selected file under ``logs``.
    """
    # Define allowed logging levels
    LogLevel = Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

    def __init__(self, log_file: str) -> None:
        """Create the logs directory and target file when necessary.

        Args:
            log_file: Name of the log file, such as ``application.log``.

        Raises:
            Exception: Propagates filesystem errors after printing a diagnostic.
        """
        # Ensure the log directory exists
        base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'logs')
        os.makedirs(base_dir, exist_ok=True)

        # Full path to the log file
        self.log_file = os.path.join(base_dir, log_file)

        # Ensure the file exists
        try:
            if not os.path.exists(self.log_file):
                with open(self.log_file, 'w') as f:
                    f.write(f"Log file created on {datetime.now()}\n")
        except Exception as e:
            print(f"Failed to create log file '{self.log_file}': {e}")
            raise

    def log(self, message: str, level: LogLevel) -> None:
        """Append an event with its local timestamp and severity.

        Args:
            message: Event text to record.
            level: One of the supported uppercase severity names.

        Raises:
            Exception: Propagates filesystem errors after printing a diagnostic.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] [{level}] {message}\n"

        # Attempt to write the log entry to the file
        try:
            with open(self.log_file, 'a') as f:
                f.write(log_entry)
        except Exception as e:
            print(f"Failed to write to log file '{self.log_file}': {e}")
            raise

    # Convenience methods for each log level
    def debug(self, message: str) -> None:
        """Log a debug-level event.

        Args:
            message: Event text to record.
        """

        self.log(message, 'DEBUG')

    def info(self, message: str) -> None:
        """Log an informational event.

        Args:
            message: Event text to record.
        """

        self.log(message, 'INFO')

    def warning(self, message: str) -> None:
        """Log a warning event.

        Args:
            message: Event text to record.
        """

        self.log(message, 'WARNING')

    def error(self, message: str) -> None:
        """Log an error event.

        Args:
            message: Event text to record.
        """

        self.log(message, 'ERROR')

    def critical(self, message: str) -> None:
        """Log a critical event.

        Args:
            message: Event text to record.
        """

        self.log(message, 'CRITICAL')
