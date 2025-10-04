import os
from datetime import datetime
from typing import Literal

class AppLogger:
    """
    A class to log application events to a .log file.
    Supports different logging levels: DEBUG, INFO, WARNING, ERROR, CRITICAL.
    Logs are stored in 'newsletter_ai/data_log/' relative to the project root.
    """
    # Define allowed logging levels
    LogLevel = Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

    def __init__(self, log_file: str) -> None:
        """
        Initialize the logger with the given file name.
        If the file or folder does not exist, it will be created automatically.

        :param log_file: Name of the log file (e.g., 'application.log')
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
        """
        Log an event to the file with a timestamp and the specified level.

        :param message: The message to log
        :param level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
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
        self.log(message, 'DEBUG')

    def info(self, message: str) -> None:
        self.log(message, 'INFO')

    def warning(self, message: str) -> None:
        self.log(message, 'WARNING')

    def error(self, message: str) -> None:
        self.log(message, 'ERROR')

    def critical(self, message: str) -> None:
        self.log(message, 'CRITICAL')
