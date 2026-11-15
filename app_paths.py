"""Where the app lives and where it may write.

Installed on Windows the program folder may be read-only, so the app never
writes beside itself: settings go to %APPDATA%\\DTS2DD (app_settings.py) and
downloads to %LOCALAPPDATA%\\DTS2DD.
"""
import os
import platform
import sys
from pathlib import Path
from typing import Iterable, Optional

APP_FOLDER = "DTS2DD"


def exe_name(name: str) -> str:
    return f"{name}.exe" if platform.system() == "Windows" else name


def app_dir() -> Path:
    """Folder containing the program (the .exe when frozen, else the source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    """Per-user writable folder for downloaded tools."""
    override = os.environ.get("DTS2DD_DATA_DIR")
    if override:
        return Path(override)
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_FOLDER


def find_tool(name: str, folders: Iterable[Path]) -> Optional[Path]:
    """First ``name`` executable found directly in, in bin/ of, or below each folder."""
    exe = exe_name(name)
    for folder in folders:
        if not folder.is_dir():
            continue
        for candidate in (folder / exe, folder / "bin" / exe):
            if candidate.is_file():
                return candidate
        for candidate in folder.rglob(exe):
            if candidate.is_file():
                return candidate
    return None


def ffmpeg_folders() -> list:
    """Bundled folder first, then the per-user download folder, then the
    legacy ./ffmpeg folder used by development checkouts."""
    folders = [app_dir() / "ffmpeg", user_data_dir() / "ffmpeg", Path.cwd() / "ffmpeg"]
    unique = []
    for f in folders:
        if f not in unique:
            unique.append(f)
    return unique
