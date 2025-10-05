import os
import subprocess

from pathlib import Path
from typing import Optional
from typing import NoReturn




def clear_files_in_directory() -> None:
    """
    Clear the contents of all files in the 'logs' directory located
    relative to the current working directory.

    Each file in the 'logs' directory will be truncated to size 0.
    Only regular files are affected.

    Raises:
        FileNotFoundError: If the 'logs' directory does not exist.
        RuntimeError: If the truncate command fails for any file.
    """
    try:
        current_dir = os.getcwd()
        logs_dir = os.path.join(current_dir, "logs")
        if not os.path.isdir(logs_dir):
            raise FileNotFoundError(f"Logs directory does not exist: {logs_dir}")

        for filename in os.listdir(logs_dir):
            file_path = os.path.join(logs_dir, filename)
            if os.path.isfile(file_path):
                try:
                    subprocess.run(["truncate", "-s", "0", file_path], check=True)
                except subprocess.CalledProcessError as exc:
                    raise RuntimeError(f"Failed to clear {file_path}: {exc}")
    except Exception as e:
        print(f"Error while clearing log files: {e}")
        raise



def load_secret_key_from_file(path: str) -> str:
    """
    Load the Flask secret key from a file.

    Args:
        path (str): Path to the file containing the secret key.

    Returns:
        str: The secret key read from the file.

    Raises:
        RuntimeError: If the file does not exist, cannot be read,
                      or contains an empty value.
    """
    file_path = Path(path)

    if not file_path.exists() or not file_path.is_file():
        raise RuntimeError(f"Secret key file not found: {file_path}")

    try:
        secret = file_path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise RuntimeError(f"Failed to read secret key from {file_path}: {exc}")

    if not secret:
        raise RuntimeError(f"Secret key file is empty: {file_path}")

    return secret
