"""FFmpeg subprocess management and audio stream parsing."""
import re
import subprocess
import platform
from typing import Callable, List, Optional

from audio_stream_info import AudioStreamInfo


def get_audio_streams(file_path: str, ffmpeg_path: str) -> List[AudioStreamInfo]:
    """
    Probe an MKV file and extract audio stream information.

    Args:
        file_path: Path to the input MKV file
        ffmpeg_path: Path to the ffmpeg binary

    Returns:
        List of AudioStreamInfo objects

    Raises:
        RuntimeError: If FFmpeg probe fails
    """
    try:
        result = subprocess.run(
            [ffmpeg_path, "-i", file_path],
            capture_output=True,
            text=True,
            timeout=10
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("FFmpeg probe timed out")
    except Exception as e:
        raise RuntimeError(f"FFmpeg probe failed: {str(e)}")

    # Parse stderr for audio stream information
    # Example line: Stream #0:1(eng): Audio: dts (DTS), 48000 Hz, 5.1(side), fltp, 1536 kb/s
    audio_streams = []
    stream_pattern = re.compile(
        r'Stream #(\d+):(\d+)(?:\((\w+)\))?: Audio: ([^,]+), (\d+) Hz, ([^,]+)(?:, [^,]+)?,(?:\s+(\d+)\s+kb/s)?'
    )

    for line in result.stderr.split('\n'):
        match = stream_pattern.search(line)
        if match:
            file_idx, stream_idx, lang, codec, sample_rate, channels, bitrate = match.groups()

            # Clean up codec name (remove details in parentheses)
            codec_clean = codec.split('(')[0].strip().upper()

            # Format bitrate
            bitrate_str = None
            if bitrate:
                bitrate_str = f"{bitrate} kb/s"

            audio_streams.append(AudioStreamInfo(
                stream_index=int(stream_idx),
                codec_name=codec_clean,
                channels=channels,
                bitrate=bitrate_str,
                language=lang if lang else None
            ))

    if not audio_streams:
        raise RuntimeError("No audio streams found in the file")

    return audio_streams


def convert_to_ac3(
    input_path: str,
    output_path: str,
    bitrate: str,
    ffmpeg_path: str,
    codec: str = "ac3",
    progress_callback: Optional[Callable[[str], None]] = None
) -> bool:
    """
    Convert audio stream in MKV file to AC-3 or E-AC-3 format.

    Args:
        input_path: Path to the input MKV file
        output_path: Path to the output MKV file
        bitrate: Audio bitrate (e.g., "384k", "448k", "640k")
        ffmpeg_path: Path to the ffmpeg binary
        codec: Audio codec ("ac3" or "eac3")
        progress_callback: Optional function(message) to report progress

    Returns:
        True on success, False on failure
    """
    cmd = [
        ffmpeg_path,
        "-i", input_path,
        "-c:v", "copy",
        "-c:a", codec,
        "-b:a", bitrate,
        "-progress", "pipe:1",
        "-y",
        output_path
    ]

    try:
        # Platform-specific subprocess flags
        creationflags = 0
        if platform.system() == "Windows":
            # Hide console window on Windows
            creationflags = subprocess.CREATE_NO_WINDOW

        if progress_callback:
            progress_callback(f"Starting FFmpeg: {ffmpeg_path}")
            progress_callback(f"Input: {input_path}")
            progress_callback(f"Output: {output_path}")
            progress_callback("Encoding in progress...")

        # Use run() for simple, reliable execution
        result = subprocess.run(
            cmd,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        if progress_callback:
            progress_callback("Finalizing output... 100%")

        returncode = result.returncode

        if progress_callback:
            progress_callback(f"FFmpeg process completed with code: {returncode}")

        if returncode != 0:
            if progress_callback:
                progress_callback(f"ERROR: FFmpeg failed with code {returncode}")
            raise RuntimeError(f"FFmpeg conversion failed with code {returncode}")

        return True

    except FileNotFoundError:
        raise RuntimeError("FFmpeg not found at specified path")
    except subprocess.TimeoutExpired:
        process.kill()
        raise RuntimeError("FFmpeg conversion timed out")
    except Exception as e:
        raise RuntimeError(f"Conversion error: {str(e)}")
