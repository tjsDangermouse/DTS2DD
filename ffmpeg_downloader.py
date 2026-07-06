"""Automatic FFmpeg detection and download."""
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

try:
    import py7zr
except ImportError:
    py7zr = None


def get_ffmpeg_path() -> Optional[str]:
    """
    Get the path to FFmpeg binary.

    Checks in order:
    1. System PATH
    2. Local ./ffmpeg/ directory (direct)
    3. Local ./ffmpeg/ directory (recursive search)

    Returns:
        Path to ffmpeg binary if found, None otherwise
    """
    # Check system PATH
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path

    # Check local ./ffmpeg/ directory (direct)
    local_ffmpeg_dir = Path("./ffmpeg")
    if platform.system() == "Windows":
        local_ffmpeg = local_ffmpeg_dir / "ffmpeg.exe"
    else:
        local_ffmpeg = local_ffmpeg_dir / "ffmpeg"

    if local_ffmpeg.exists():
        return str(local_ffmpeg)

    # Check for ffmpeg in nested folders (from extraction)
    if local_ffmpeg_dir.exists():
        for exe_file in local_ffmpeg_dir.rglob("ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"):
            if exe_file.is_file():
                return str(exe_file)

    return None


def verify_ffmpeg(ffmpeg_path: str) -> bool:
    """
    Verify that FFmpeg is working correctly.

    Args:
        ffmpeg_path: Path to the ffmpeg binary

    Returns:
        True if ffmpeg runs successfully, False otherwise
    """
    try:
        result = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def download_ffmpeg(progress_callback=None) -> bool:
    """
    Download and extract FFmpeg binary for the current platform.

    Args:
        progress_callback: Optional function(message) to report progress

    Returns:
        True on success, False on failure
    """
    system = platform.system()
    ffmpeg_dir = Path("./ffmpeg")
    ffmpeg_dir.mkdir(exist_ok=True)

    try:
        if system == "Darwin":  # macOS
            return _download_ffmpeg_macos(progress_callback)
        elif system == "Windows":
            return _download_ffmpeg_windows(progress_callback)
        else:
            if progress_callback:
                progress_callback(f"Unsupported platform: {system}")
            return False
    except Exception as e:
        if progress_callback:
            progress_callback(f"Error during download: {str(e)}")
        return False


def _download_ffmpeg_macos(progress_callback=None) -> bool:
    """Download and extract FFmpeg for macOS."""
    url = "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/7z"
    ffmpeg_dir = Path("./ffmpeg")
    archive_path = ffmpeg_dir / "ffmpeg.7z"

    if progress_callback:
        progress_callback("Downloading FFmpeg for macOS...")

    try:
        # Download the file
        urllib.request.urlretrieve(url, archive_path)

        if progress_callback:
            progress_callback("Extracting FFmpeg...")

        # Extract 7z file
        if py7zr is None:
            if progress_callback:
                progress_callback("Error: py7zr library not found. Install with: pip install py7zr")
            return False

        with py7zr.SevenZipFile(archive_path, 'r') as archive:
            archive.extractall(path=ffmpeg_dir)

        # Find and move the ffmpeg binary
        extracted_ffmpeg = None
        for item in ffmpeg_dir.rglob("ffmpeg"):
            if item.is_file() and not item.suffix:  # Binary file with no extension
                extracted_ffmpeg = item
                break

        if extracted_ffmpeg:
            final_path = ffmpeg_dir / "ffmpeg"
            if extracted_ffmpeg != final_path:
                extracted_ffmpeg.rename(final_path)

            # Make executable
            os.chmod(final_path, 0o755)

            # Clean up archive
            archive_path.unlink()

            if progress_callback:
                progress_callback("FFmpeg extracted successfully")
            return True
        else:
            if progress_callback:
                progress_callback("Error: Could not find ffmpeg binary in extracted files")
            return False

    except urllib.error.URLError as e:
        if progress_callback:
            progress_callback(f"Download failed: {str(e)}")
        return False
    except Exception as e:
        if progress_callback:
            progress_callback(f"Extraction failed: {str(e)}")
        return False


def _download_ffmpeg_windows(progress_callback=None) -> bool:
    """Download and extract FFmpeg for Windows."""
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    ffmpeg_dir = Path("./ffmpeg")
    archive_path = ffmpeg_dir / "ffmpeg.zip"

    if progress_callback:
        progress_callback("Downloading FFmpeg for Windows...")

    try:
        # Download the file with progress callback
        import urllib.request
        import urllib.error

        def download_with_progress(url, filepath, progress_callback=None):
            """Download file with progress reporting."""
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0')

            with urllib.request.urlopen(req, timeout=60) as response:
                file_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                chunk_size = 1024 * 1024  # 1MB chunks

                with open(filepath, 'wb') as out_file:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        out_file.write(chunk)
                        downloaded += len(chunk)

                        if file_size > 0 and progress_callback:
                            percent = int((downloaded / file_size) * 100)
                            mb_done = downloaded / (1024 * 1024)
                            mb_total = file_size / (1024 * 1024)
                            progress_callback(
                                f"Downloading FFmpeg: {percent}% ({mb_done:.1f}MB / {mb_total:.1f}MB)"
                            )

        download_with_progress(url, archive_path, progress_callback)

        # Verify file was downloaded
        if archive_path.stat().st_size == 0:
            if progress_callback:
                progress_callback("Download failed: Empty file received")
            archive_path.unlink()
            return False

        if progress_callback:
            progress_callback("Extracting FFmpeg...")

        # Extract zip file
        with zipfile.ZipFile(archive_path, 'r') as archive:
            archive.extractall(path=ffmpeg_dir)

        # Find the ffmpeg.exe binary (may be in nested folders)
        extracted_ffmpeg = None
        for item in ffmpeg_dir.rglob("ffmpeg.exe"):
            if item.is_file():
                extracted_ffmpeg = item
                break

        if extracted_ffmpeg:
            # Try to move to root of ffmpeg_dir
            final_path = ffmpeg_dir / "ffmpeg.exe"
            try:
                if extracted_ffmpeg != final_path:
                    import shutil as sh
                    sh.copy2(extracted_ffmpeg, final_path)
                    if progress_callback:
                        progress_callback("FFmpeg extracted and copied to ffmpeg folder")
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Note: Could not move ffmpeg to root, using from: {extracted_ffmpeg.parent}")

            # Clean up archive
            archive_path.unlink()

            if progress_callback:
                progress_callback("FFmpeg extraction completed")
            return True
        else:
            if progress_callback:
                progress_callback("Error: Could not find ffmpeg.exe in extracted files")
            return False

    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        if progress_callback:
            progress_callback(f"Download failed: {str(e)}")
        if archive_path.exists():
            archive_path.unlink()
        return False
    except Exception as e:
        if progress_callback:
            progress_callback(f"Extraction failed: {str(e)}")
        if archive_path.exists():
            archive_path.unlink()
        return False


def ensure_ffmpeg_available(progress_callback=None) -> str:
    """
    Ensure FFmpeg is available on the system.

    Checks for FFmpeg in PATH and local ./ffmpeg/ directory.
    Downloads if not found.

    Args:
        progress_callback: Optional function(message) for progress updates

    Returns:
        Path to ffmpeg binary

    Raises:
        RuntimeError: If FFmpeg cannot be obtained
    """
    ffmpeg_path = get_ffmpeg_path()

    if ffmpeg_path:
        if verify_ffmpeg(ffmpeg_path):
            if progress_callback:
                progress_callback("Found existing FFmpeg installation")
            return ffmpeg_path

    # Try to download FFmpeg
    if not download_ffmpeg(progress_callback):
        raise RuntimeError(
            "Could not obtain FFmpeg. Please install manually:\n"
            "macOS: brew install ffmpeg\n"
            "Windows: Download from https://www.gyan.dev/ffmpeg/builds/"
        )

    # Get the newly downloaded FFmpeg
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path or not verify_ffmpeg(ffmpeg_path):
        raise RuntimeError(
            "FFmpeg download completed but verification failed. "
            "Please check your installation."
        )

    if progress_callback:
        progress_callback("FFmpeg ready!")
    return ffmpeg_path
