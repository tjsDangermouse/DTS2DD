"""FFmpeg subprocess management and audio stream parsing."""
import collections
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from audio_stream_info import AudioStreamInfo, SubtitleStreamInfo
from media_probe import (
    audio_streams_from_probe, find_ffprobe, format_duration, format_seconds, run_ffprobe,
    subprocess_flags, subtitle_streams_from_probe,
)


class ConversionCancelled(RuntimeError):
    """Raised when the user cancels a running FFmpeg process."""


def get_audio_streams(
    file_path: str,
    ffmpeg_path: str,
    ffprobe_path: Optional[str] = None,
) -> List[AudioStreamInfo]:
    """
    Probe an MKV file and extract audio stream information.

    Uses ffprobe JSON output when ffprobe is available (accurate channel
    layout, profile, bit depth, title and dispositions). Falls back to the
    original parsing of ``ffmpeg -i`` text output otherwise.

    Args:
        file_path: Path to the input MKV file
        ffmpeg_path: Path to the ffmpeg binary
        ffprobe_path: Optional ffprobe binary (located automatically if omitted)

    Returns:
        List of AudioStreamInfo objects

    Raises:
        RuntimeError: If FFmpeg probe fails
    """
    ffprobe_path = ffprobe_path or find_ffprobe(ffmpeg_path)
    if ffprobe_path:
        try:
            streams = audio_streams_from_probe(run_ffprobe(ffprobe_path, file_path))
            if streams:
                return streams
        except RuntimeError:
            pass  # fall back to the text parser below
    return _get_audio_streams_text(file_path, ffmpeg_path)


def get_tracks(
    file_path: str,
    ffmpeg_path: str,
    ffprobe_path: Optional[str] = None,
) -> Tuple[List[AudioStreamInfo], List[SubtitleStreamInfo]]:
    """Audio and subtitle tracks of an MKV (one probe).

    Raises RuntimeError when no audio stream is found, like get_audio_streams.
    """
    ffprobe_path = ffprobe_path or find_ffprobe(ffmpeg_path)
    if ffprobe_path:
        try:
            data = run_ffprobe(ffprobe_path, file_path)
            audio = audio_streams_from_probe(data)
            if audio:
                return audio, subtitle_streams_from_probe(data)
        except RuntimeError:
            pass  # fall back to the text parser below
    stderr = _probe_text(file_path, ffmpeg_path)
    return _parse_audio_text(stderr), _parse_subtitles_text(stderr)


def _probe_text(file_path: str, ffmpeg_path: str) -> str:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-i", file_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=subprocess_flags(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("FFmpeg probe timed out")
    except Exception as e:
        raise RuntimeError(f"FFmpeg probe failed: {str(e)}")
    return result.stderr


def _parse_subtitles_text(stderr: str) -> List[SubtitleStreamInfo]:
    # Example: Stream #0:3(eng): Subtitle: hdmv_pgs_subtitle (default) (forced)
    pattern = re.compile(r'Stream #\d+:(\d+)(?:\[\w+\])?(?:\((\w+)\))?: Subtitle: (\w+)(.*)')
    subtitles = []
    for line in stderr.split('\n'):
        match = pattern.search(line)
        if match:
            index, lang, codec, rest = match.groups()
            subtitles.append(SubtitleStreamInfo(
                stream_index=int(index), codec_name=codec, language=lang,
                is_default="(default)" in rest, is_forced="(forced)" in rest,
            ))
    return subtitles


def _get_audio_streams_text(file_path: str, ffmpeg_path: str) -> List[AudioStreamInfo]:
    """Original text-based probe (kept for builds without ffprobe)."""
    return _parse_audio_text(_probe_text(file_path, ffmpeg_path))


def _parse_audio_text(stderr: str) -> List[AudioStreamInfo]:
    # Parse stderr for audio stream information
    # Example line: Stream #0:1(eng): Audio: dts (DTS), 48000 Hz, 5.1(side), fltp, 1536 kb/s
    audio_streams = []
    stream_pattern = re.compile(
        r'Stream #(\d+):(\d+)(?:\[\w+\])?(?:\((\w+)\))?: Audio: ([^,]+), (\d+) Hz, ([^,]+)(?:, [^,]+)?,(?:\s+(\d+)\s+kb/s)?'
    )

    for line in stderr.split('\n'):
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
                language=lang if lang else None,
                audio_index=len(audio_streams),
            ))

    if not audio_streams:
        raise RuntimeError("No audio streams found in the file")

    return audio_streams


# --------------------------------------------------------------------------
# Command building shared by the AC-3/E-AC-3 and TrueHD workflows
# --------------------------------------------------------------------------

def build_stream_maps(source_stream_index: int, remove_source_stream: bool,
                      remove_streams: Sequence[int] = ()) -> List[str]:
    """Map every input stream, plus the selected audio stream a second time.

    ``remove_streams`` are further absolute stream indices (audio or
    subtitle tracks the user ticked) left out of the output.

    Attachments (fonts etc.) are moved to the end. FFmpeg's Matroska muxer
    rejects the job ("Received a packet for an attachment stream") when an
    encoded stream is mapped after an attachment; Matroska stores
    attachments separately from tracks, so this changes nothing visible.
    """
    maps = ["-map", "0"]
    if remove_source_stream:
        maps += ["-map", f"-0:{source_stream_index}"]
    for index in sorted(set(remove_streams) - {source_stream_index}):
        maps += ["-map", f"-0:{index}"]
    maps += [
        "-map", "-0:t?",
        "-map", f"0:{source_stream_index}",
        "-map", "0:t?",
    ]
    return maps


def converted_audio_output_index(audio_stream_count: int, remove_source_stream: bool,
                                 removed_other_audio: int = 0) -> int:
    """Output audio index (the N in ``a:N``) of the appended converted stream.

    ``removed_other_audio`` counts removed audio tracks other than the source.
    """
    return audio_stream_count - (1 if remove_source_stream else 0) - removed_other_audio


def build_append_audio_command(
    ffmpeg_path: str,
    source_path: str,
    output_path: str,
    source_stream_index: int,
    audio_stream_count: int,
    codec: str,
    bitrate: str,
    remove_source_stream: bool,
    remove_streams: Sequence[int] = (),
    removed_other_audio: int = 0,
    make_default: Optional[bool] = None,
    title: Optional[str] = None,
    keep_args: Sequence[str] = (),
) -> List[str]:
    """FFmpeg command for the AC-3 / E-AC-3 append workflow.

    ``make_default`` None keeps the original behaviour (dispositions copied
    from the source track). True makes the new track the only default audio
    track; False clears its default flag so the original stays the choice.
    """
    converted_audio_index = converted_audio_output_index(
        audio_stream_count, remove_source_stream, removed_other_audio)
    cmd = [ffmpeg_path, "-hide_banner", "-nostdin", "-i", source_path]
    cmd += build_stream_maps(source_stream_index, remove_source_stream, remove_streams)
    cmd += [
        "-c", "copy",
        f"-c:a:{converted_audio_index}", codec,
        f"-b:a:{converted_audio_index}", bitrate,
    ]
    if title:
        cmd += [f"-metadata:s:a:{converted_audio_index}", f"title={title}"]
    if make_default is not None:
        cmd += disposition_args(converted_audio_index, make_default) + ["-default_mode", "passthrough"]
    else:
        cmd += list(keep_args)
    cmd += [
        "-progress", "pipe:1", "-nostats",
        "-y",
        output_path,
    ]
    return cmd


def disposition_args(audio_index: int, make_default: bool) -> List[str]:
    """Make output audio ``audio_index`` the only default track, or clear its flag."""
    if not make_default:
        return [f"-disposition:a:{audio_index}", "0"]
    args = []
    for j in range(audio_index):
        args += [f"-disposition:a:{j}", "-default"]
    return args + [f"-disposition:a:{audio_index}", "default"]


# --------------------------------------------------------------------------
# Process runner with progress, stderr capture and cancellation
# --------------------------------------------------------------------------

@dataclass
class FFmpegProgress:
    out_seconds: float
    total_seconds: Optional[float]
    speed: Optional[float]
    elapsed: float
    out_bytes: Optional[int] = None    # bytes written so far (stream copy: disk-bound)

    @property
    def write_rate(self) -> Optional[float]:
        """Average bytes per second written so far."""
        if not self.out_bytes or self.elapsed <= 0:
            return None
        return self.out_bytes / self.elapsed

    @property
    def fraction(self) -> Optional[float]:
        if not self.total_seconds:
            return None
        return max(0.0, min(1.0, self.out_seconds / self.total_seconds))

    @property
    def eta(self) -> Optional[float]:
        fraction = self.fraction
        if not fraction or fraction <= 0.001:
            return None
        return self.elapsed * (1 - fraction) / fraction


@dataclass
class FFmpegResult:
    returncode: int
    stderr_tail: List[str]

    def error_summary(self, max_lines: int = 8) -> str:
        """The most useful stderr lines (FFmpeg prints the cause last)."""
        lines = [l for l in self.stderr_tail if l.strip()]
        return "\n".join(lines[-max_lines:]) or f"exit code {self.returncode}"


class ProcessHandle:
    """Lets another thread cancel the currently running process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self.cancelled = threading.Event()

    def attach(self, proc):
        with self._lock:
            self._proc = proc
        if self.cancelled.is_set():
            self._terminate(proc)

    def detach(self):
        with self._lock:
            self._proc = None

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._terminate(proc)

    @staticmethod
    def _terminate(proc):
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass


def run_ffmpeg(
    cmd: Sequence[str],
    total_seconds: Optional[float] = None,
    on_progress: Optional[Callable[[FFmpegProgress], None]] = None,
    handle: Optional[ProcessHandle] = None,
    stderr_lines: int = 200,
) -> FFmpegResult:
    """Run FFmpeg with ``-progress pipe:1`` and report structured progress.

    stderr is drained on a separate thread so the pipe can never fill and
    stall the encoder; the last ``stderr_lines`` lines are kept for errors.
    """
    tail = collections.deque(maxlen=stderr_lines)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess_flags(),
        )
    except FileNotFoundError:
        raise RuntimeError(f"Executable not found: {cmd[0]}")

    if handle:
        handle.attach(proc)

    def drain_stderr():
        for line in proc.stderr:
            tail.append(line.rstrip())

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    out_seconds = 0.0
    out_bytes = None
    speed = None
    try:
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms"):
                # Both keys are in microseconds in current FFmpeg builds.
                try:
                    out_seconds = int(value) / 1_000_000
                except ValueError:
                    pass
            elif key == "total_size":
                try:
                    out_bytes = int(value)
                except ValueError:
                    pass
            elif key == "speed":
                try:
                    speed = float(value.rstrip("x"))
                except ValueError:
                    speed = None
            elif key == "progress" and on_progress:
                on_progress(FFmpegProgress(out_seconds, total_seconds, speed,
                                           time.monotonic() - started, out_bytes))
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        stderr_thread.join(timeout=5)
        if handle:
            handle.detach()

    if handle and handle.cancelled.is_set():
        raise ConversionCancelled("Conversion cancelled by user")
    return FFmpegResult(proc.returncode, list(tail))


# --------------------------------------------------------------------------
# AC-3 / E-AC-3 workflow (in-place update, unchanged behaviour)
# --------------------------------------------------------------------------

def convert_and_append_audio(
    input_path: str,
    bitrate: str,
    ffmpeg_path: str,
    source_stream_index: int,
    audio_stream_count: int,
    codec: str = "ac3",
    remove_source_stream: bool = False,
    progress_callback: Optional[Callable[[str], None]] = None,
    handle: Optional[ProcessHandle] = None,
    remove_streams: Sequence[int] = (),
    removed_other_audio: int = 0,
    on_progress: Optional[Callable[[FFmpegProgress], None]] = None,
    make_default: Optional[bool] = None,
    validate: Optional[Callable[[str], None]] = None,
    title: Optional[str] = None,
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
        handle: Optional ProcessHandle used to cancel the conversion
        remove_streams: Other absolute stream indices (audio/subtitle) to drop
        removed_other_audio: How many of ``remove_streams`` are audio streams
        make_default: see build_append_audio_command
        validate: optional check of the finished temporary file; raising
            stops the replace (the source is left unchanged)

    Returns:
        True on success, False on failure
    """
    if audio_stream_count < 1:
        raise RuntimeError("The MKV does not contain an audio stream to convert")

    removed = set(remove_streams) | ({source_stream_index} if remove_source_stream else set())
    keep = keep_dispositions_args(_probe_quietly(input_path, ffmpeg_path), removed)
    cmd_args = (source_stream_index, audio_stream_count, codec, bitrate,
                remove_source_stream, remove_streams, removed_other_audio, make_default, title, keep)
    return _rewrite_in_place(
        input_path, ffmpeg_path,
        lambda src, tmp: build_append_audio_command(ffmpeg_path, src, tmp, *cmd_args),
        progress_callback, handle, encoding=True, on_progress=on_progress, validate=validate,
    )


def build_remove_streams_command(ffmpeg_path: str, source_path: str, output_path: str,
                                 remove_streams: Sequence[int], keep_args: Sequence[str] = ()) -> List[str]:
    """Stream-copy every track except ``remove_streams`` (nothing is re-encoded).

    ``keep_args`` (see keep_dispositions_args) keeps default flags unchanged.
    """
    cmd = [ffmpeg_path, "-hide_banner", "-nostdin", "-i", source_path, "-map", "0"]
    for index in sorted(set(remove_streams)):
        cmd += ["-map", f"-0:{index}"]
    cmd += ["-c", "copy"] + list(keep_args) + ["-progress", "pipe:1", "-nostats", "-y", output_path]
    return cmd


DISPOSITION_NAMES = (
    "default", "dub", "original", "comment", "lyrics", "karaoke", "forced", "hearing_impaired",
    "visual_impaired", "clean_effects", "attached_pic", "captions", "descriptions", "metadata",
    "dependent", "still_image",
)


def keep_dispositions_args(probe: Optional[dict], removed: Sequence[int] = ()) -> List[str]:
    """Stop the ffmpeg CLI from inventing default flags.

    When no ``-disposition`` option is given, ffmpeg marks the first track of
    a type "default" if two or more tracks of that type remain and none is
    default - e.g. the first subtitle after removing others, which makes
    players show subtitles automatically. Any explicit disposition disables
    that, so output stream 0 is given exactly the flags it already has.
    """
    removed = set(removed)
    for st in (probe or {}).get("streams", []):
        if st.get("index") in removed or st.get("codec_type") in ("attachment", None):
            continue
        flags = [k for k, v in (st.get("disposition") or {}).items() if v and k in DISPOSITION_NAMES]
        return ["-disposition:0", "+".join(flags) or "0"]
    return []


def _probe_quietly(source_path: str, ffmpeg_path: str) -> Optional[dict]:
    try:
        ffprobe = find_ffprobe(ffmpeg_path)
        return run_ffprobe(ffprobe, source_path) if ffprobe else None
    except Exception:
        return None


def build_remux_command(ffmpeg_path: str, source_path: str, output_path: str,
                        remove_streams: Sequence[int] = (),
                        dispositions: Optional[dict] = None,
                        keep_args: Sequence[str] = ()) -> List[str]:
    """Stream-copy remux that can drop tracks and set dispositions.

    ``dispositions`` maps an OUTPUT stream specifier (e.g. "a:0") to an
    FFmpeg disposition value ("default", "0", "-default", ...). Default flags
    are written exactly as set (``-default_mode passthrough``).
    """
    cmd = [ffmpeg_path, "-hide_banner", "-nostdin", "-i", source_path, "-map", "0"]
    for index in sorted(set(remove_streams)):
        cmd += ["-map", f"-0:{index}"]
    cmd += ["-c", "copy"] + list(keep_args)
    for spec, value in (dispositions or {}).items():
        cmd += [f"-disposition:{spec}", value]
    cmd += ["-default_mode", "passthrough", "-progress", "pipe:1", "-nostats", "-y", output_path]
    return cmd


def remux_in_place(
    input_path: str,
    ffmpeg_path: str,
    remove_streams: Sequence[int] = (),
    dispositions: Optional[dict] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    handle: Optional[ProcessHandle] = None,
    on_progress: Optional[Callable[[FFmpegProgress], None]] = None,
    validate: Optional[Callable[[str], None]] = None,
) -> bool:
    """Library-scanner repair: drop tracks and/or fix flags without re-encoding."""
    if not remove_streams and not dispositions:
        raise RuntimeError("Nothing to change")
    keep = keep_dispositions_args(_probe_quietly(input_path, ffmpeg_path), remove_streams)
    return _rewrite_in_place(
        input_path, ffmpeg_path,
        lambda src, tmp: build_remux_command(ffmpeg_path, src, tmp, remove_streams, dispositions, keep),
        progress_callback, handle, encoding=False, on_progress=on_progress, validate=validate,
    )


def remove_streams_in_place(
    input_path: str,
    ffmpeg_path: str,
    remove_streams: Sequence[int],
    progress_callback: Optional[Callable[[str], None]] = None,
    handle: Optional[ProcessHandle] = None,
    on_progress: Optional[Callable[[FFmpegProgress], None]] = None,
) -> bool:
    """Remove audio/subtitle tracks from an MKV without converting anything.

    Works like the conversion: a temporary MKV is written beside the source
    and replaces it only after FFmpeg succeeded.
    """
    if not remove_streams:
        raise RuntimeError("No tracks were chosen for removal")
    keep = keep_dispositions_args(_probe_quietly(input_path, ffmpeg_path), remove_streams)
    return _rewrite_in_place(
        input_path, ffmpeg_path,
        lambda src, tmp: build_remove_streams_command(ffmpeg_path, src, tmp, remove_streams, keep),
        progress_callback, handle, encoding=False, on_progress=on_progress,
    )


def _duration_of(source_path: str, ffmpeg_path: str) -> Optional[float]:
    """Film length for the progress bar (None = bar stays indeterminate)."""
    try:
        ffprobe = find_ffprobe(ffmpeg_path)
        return format_duration(run_ffprobe(ffprobe, source_path)) if ffprobe else None
    except Exception:
        return None


def format_size(size: float) -> str:
    return f"{size / 1e9:.1f} GB" if size >= 1e9 else f"{size / 1e6:.0f} MB"


def copy_summary(written: int, seconds: float) -> str:
    """E.g. 'Wrote 40.1 GB in 14:05 (47 MB/s)'."""
    rate = f" ({written / seconds / 1e6:.0f} MB/s)" if seconds > 0 else ""
    return f"Wrote {format_size(written)} in {format_seconds(seconds)}{rate}"


def _rewrite_in_place(
    input_path: str,
    ffmpeg_path: str,
    build_cmd: Callable[[str, str], List[str]],
    progress_callback: Optional[Callable[[str], None]],
    handle: Optional[ProcessHandle],
    encoding: bool,
    on_progress: Optional[Callable[[FFmpegProgress], None]] = None,
    validate: Optional[Callable[[str], None]] = None,
) -> bool:
    """Run ``build_cmd(source, temp)`` then atomically replace the source.

    ``validate(temp_path)`` (optional) runs before the replace; if it raises,
    the temporary file is deleted and the source is left unchanged.

    Even without re-encoding, MKV interleaves every track, so the whole file
    is read and written again; the time is set by the disk. ``on_progress``
    receives percentage / speed / bytes written so the user can see that.
    """
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
    # mkstemp creates owner-only files; keep the source's permissions so the
    # replaced MKV stays readable by other users (e.g. a Plex service account).
    shutil.copymode(source_path, temporary_path)

    cmd = build_cmd(source_path, temporary_path)

    try:
        if progress_callback:
            progress_callback(f"Starting FFmpeg: {ffmpeg_path}")
            progress_callback(f"Input: {source_path}")
            progress_callback("Creating a temporary MKV before replacing the source file...")
            progress_callback("Encoding in progress..." if encoding else "Copying tracks...")

        total_seconds = _duration_of(source_path, ffmpeg_path)
        started = time.monotonic()
        result = run_ffmpeg(cmd, total_seconds=total_seconds, on_progress=on_progress, handle=handle)
        returncode = result.returncode

        if progress_callback:
            progress_callback(f"FFmpeg process completed with code: {returncode}")

        if returncode != 0:
            if progress_callback:
                progress_callback(f"ERROR: FFmpeg failed with code {returncode}")
            raise RuntimeError(
                f"FFmpeg {'conversion' if encoding else 'track removal'} failed with code {returncode}:\n{result.error_summary()}"
            )

        written = os.path.getsize(temporary_path)
        if written == 0:
            raise RuntimeError("FFmpeg produced an empty file; the source was not changed")
        if progress_callback:
            progress_callback(copy_summary(written, time.monotonic() - started))

        if validate is not None:
            if progress_callback:
                progress_callback("Validating the new file before replacing the original...")
            validate(temporary_path)

        os.replace(temporary_path, source_path)
        if progress_callback:
            progress_callback("Source MKV updated successfully")
        return True

    except ConversionCancelled:
        raise
    except FileNotFoundError:
        raise RuntimeError("FFmpeg not found at specified path")
    except Exception as e:
        raise RuntimeError(f"Conversion error: {str(e)}")
    finally:
        # os.replace removes the temporary path on success. On failure, do not
        # leave a partial conversion beside the user's source MKV.
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
