"""FFmpeg subprocess management and audio stream parsing."""
import re
import subprocess
import platform
import os
import tempfile
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


def convert_and_append_audio(
    input_path: str,
    bitrate: str,
    ffmpeg_path: str,
    source_stream_index: int,
    audio_stream_count: int,
    codec: str = "ac3",
    remove_source_stream: bool = False,
    progress_callback: Optional[Callable[[str], None]] = None
) -> bool:
    """
    Append a converted audio stream to an MKV, then safely replace that MKV.

    All existing streams are copied without re-encoding. The selected audio
    stream is mapped a second time and that additional mapping is encoded to
    AC-3 or E-AC-3. If requested, the original selected stream is omitted from
    the replacement file.

    Args:
        input_path: Path to the input MKV file
        bitrate: Audio bitrate (e.g., "384k", "448k", "640k")
        ffmpeg_path: Path to the ffmpeg binary
        source_stream_index: Absolute stream index of the audio stream to convert
        audio_stream_count: Number of audio streams in the input MKV
        codec: Audio codec ("ac3" or "eac3")
        remove_source_stream: Remove the selected original stream from the result
        progress_callback: Optional function(message) to report progress

    Returns:
        True on success, False on failure
    """
    if audio_stream_count < 1:
        raise RuntimeError("The MKV does not contain an audio stream to convert")

    source_path = os.path.abspath(input_path)
    source_dir = os.path.dirname(source_path)
    source_name = os.path.basename(source_path)
    source_stem, _ = os.path.splitext(source_name)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{source_stem}.converting-",
        suffix=".mkv",
        dir=source_dir,
    )
    os.close(file_descriptor)

    # The converted stream is the last audio mapping: all input streams are
    # copied first, optionally minus the selected audio stream, then the
    # selected source stream is mapped again for encoding.
    converted_audio_index = audio_stream_count - (1 if remove_source_stream else 0)
    cmd = [
        ffmpeg_path,
        "-i", source_path,
        "-map", "0",
    ]

    if remove_source_stream:
        cmd.extend(["-map", f"-0:{source_stream_index}"])

    cmd.extend([
        "-map", f"0:{source_stream_index}",
        "-c", "copy",
        f"-c:a:{converted_audio_index}", codec,
        f"-b:a:{converted_audio_index}", bitrate,
        "-progress", "pipe:1",
        "-y",
        temporary_path,
    ])

    try:
        # Platform-specific subprocess flags
        creationflags = 0
        if platform.system() == "Windows":
            # Hide console window on Windows
            creationflags = subprocess.CREATE_NO_WINDOW

        if progress_callback:
            progress_callback(f"Starting FFmpeg: {ffmpeg_path}")
            progress_callback(f"Input: {source_path}")
            progress_callback("Creating a temporary MKV before replacing the source file...")
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

        os.replace(temporary_path, source_path)
        if progress_callback:
            progress_callback("Source MKV updated successfully")
        return True

    except FileNotFoundError:
        raise RuntimeError("FFmpeg not found at specified path")
    except Exception as e:
        raise RuntimeError(f"Conversion error: {str(e)}")
    finally:
        # os.replace removes the temporary path on success. On failure, do not
        # leave a partial conversion beside the user's source MKV.
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
