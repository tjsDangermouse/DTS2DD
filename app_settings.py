"""Small persistent settings store (JSON in the user's config folder)."""
import json
import os
import platform
import tempfile
from pathlib import Path
from typing import Any, Dict

APP_FOLDER = "DTS2DD"


def settings_dir() -> Path:
    override = os.environ.get("DTS2DD_SETTINGS_DIR")
    if override:
        return Path(override)
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_FOLDER


def settings_path() -> Path:
    return settings_dir() / "settings.json"


def load_settings() -> Dict[str, Any]:
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: Dict[str, Any]) -> None:
    """Write settings atomically so a crash never leaves a corrupt file."""
    directory = settings_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp, settings_path())
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def get_setting(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)


def set_setting(key: str, value: Any) -> None:
    settings = load_settings()
    settings[key] = value
    save_settings(settings)
