"""Paths and runtime configuration.

All data lives in one folder so it survives app updates:
  Windows : %APPDATA%\\DericBI-CRM
  macOS   : ~/Library/Application Support/DericBI-CRM
  Linux   : ~/.local/share/DericBI-CRM
Override with the DERICBI_DATA environment variable or --data-dir.
"""
import os
import sys
from pathlib import Path

APP_NAME = "DericBI-CRM"
DEFAULT_PORT = 8765


def default_data_dir() -> Path:
    env = os.environ.get("DERICBI_DATA")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


DATA_DIR: Path = default_data_dir()


def set_data_dir(path) -> Path:
    global DATA_DIR
    DATA_DIR = Path(path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["DERICBI_DATA"] = str(DATA_DIR)
    return DATA_DIR


def db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "dericbi_crm.sqlite3"


def static_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "static"
    return Path(__file__).resolve().parent / "static"
