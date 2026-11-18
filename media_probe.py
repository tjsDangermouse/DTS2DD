"""Structured media probing with ffprobe JSON output.

This module complements the original text parser in ffmpeg_helper.py. It is
used for accurate channel-layout detection, format identification
(DTS vs DTS-HD MA vs DTS:X, TrueHD vs TrueHD Atmos, ...) and for the
before/after verification of converted files.
"""
import json
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from app_paths import ffmpeg_folders, find_tool
from audio_stream_info import AudioStreamInfo, SubtitleStreamInfo


def subprocess_flags() -> int:
    """Hide console windows for child processes on Windows."""
    if platform.system() == "Windows":
        return subprocess.CREATE_NO_WINDOW
    return 0


def _exe_name(name: str) -> str:
    return f"{name}.exe" if platform.system() == "Windows" else name


def find_ffprobe(*ffmpeg_paths: Optional[str]) -> Optional[str]:
    """Locate an ffprobe executable.

    Search order: next to each given ffmpeg binary, then PATH, then the
    bundled / downloaded / legacy ffmpeg folders (see app_paths).
    """
    for ffmpeg_path in ffmpeg_paths:
        if not ffmpeg_path:
            continue
        sibling = Path(ffmpeg_path).with_name(_exe_name("ffprobe"))
        if sibling.is_file():
            return str(sibling)

    on_path = shutil.which("ffprobe")
    if on_path:
        return on_path

    found = find_tool("ffprobe", ffmpeg_folders())
    return str(found) if found else None


def run_ffprobe(ffprobe_path: str, file_path: str, timeout: int = 120) -> Dict:
    """Return ffprobe's JSON description of streams, format and chapters."""
    cmd = [
        ffprobe_path, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=subprocess_flags(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffprobe timed out while reading the file")
    except OSError as e:
        raise RuntimeError(f"Could not run ffprobe: {e}")

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["unknown error"]
        raise RuntimeError(f"ffprobe could not read the file: {detail[0]}")
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise RuntimeError("ffprobe returned unreadable output")


# --------------------------------------------------------------------------
# Format identification
# --------------------------------------------------------------------------

def classify_audio(stream: Dict) -> str:
    """Return a human-readable format name, or "Unknown" when unsure.

    Only profile strings that FFmpeg actually reports are mapped; anything
    else is reported as unknown rather than guessed.
    """
    codec = (stream.get("codec_name") or "").lower()
    profile = (stream.get("profile") or "").strip()

    if codec == "dts":
        if "DTS:X" in profile:
            return "DTS:X (DTS-HD Master Audio core)"
        mapping = {
            "DTS-HD MA": "DTS-HD Master Audio",
            "DTS-HD HRA": "DTS-HD High Resolution",
            "DTS": "DTS",
            "DTS-ES": "DTS-ES",
            "DTS 96/24": "DTS 96/24",
            "DTS Express": "DTS Express",
        }
        return mapping.get(profile, "DTS (Unknown profile)")
    if codec == "truehd":
        if "Atmos" in profile:
            return "Dolby TrueHD Atmos"
        return "Dolby TrueHD"
    if codec == "eac3":
        if "Atmos" in profile:
            return "Dolby Digital Plus Atmos"
        return "Dolby Digital Plus"
    if codec == "ac3":
        return "Dolby Digital"
    if codec:
        return codec.upper()
    return "Unknown"


def is_lossless_dts_hd_ma(stream: Dict) -> bool:
    """True for DTS-HD MA (including DTS:X, which carries a DTS-HD MA bed)."""
    profile = stream.get("profile") or ""
    return (stream.get("codec_name") == "dts"
            and (profile == "DTS-HD MA" or profile.startswith("DTS-HD MA + DTS:X")))


def bit_depth_of(stream: Dict) -> Optional[int]:
    """Return the source bit depth when it is known."""
    raw = stream.get("bits_per_raw_sample")
    try:
        value = int(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    sample_fmt = stream.get("sample_fmt") or ""
    if sample_fmt.startswith("s16"):
        return 16
    return None


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def audio_streams_from_probe(data: Dict) -> List[AudioStreamInfo]:
    """Convert ffprobe JSON into AudioStreamInfo objects."""
    streams = []
    audio_index = 0
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
        disposition = stream.get("disposition") or {}
        layout = stream.get("channel_layout") or None
        channels = _int_or_none(stream.get("channels"))
        bit_rate = _int_or_none(stream.get("bit_rate"))
        streams.append(AudioStreamInfo(
            stream_index=int(stream["index"]),
            codec_name=(stream.get("codec_name") or "unknown").upper(),
            channels=layout or (f"{channels} channels" if channels else "Unknown layout"),
            bitrate=f"{bit_rate // 1000} kb/s" if bit_rate else None,
            language=tags.get("language"),
            audio_index=audio_index,
            format_name=classify_audio(stream),
            profile=stream.get("profile"),
            channel_count=channels,
            channel_layout=layout,
            sample_rate=_int_or_none(stream.get("sample_rate")),
            sample_fmt=stream.get("sample_fmt"),
            bit_depth=bit_depth_of(stream),
            title=tags.get("title"),
            is_default=bool(disposition.get("default")),
            is_forced=bool(disposition.get("forced")),
        ))
        audio_index += 1
    return streams


def subtitle_streams_from_probe(data: Dict) -> List[SubtitleStreamInfo]:
    """Convert ffprobe JSON into SubtitleStreamInfo objects."""
    subtitles = []
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
        disposition = stream.get("disposition") or {}
        subtitles.append(SubtitleStreamInfo(
            stream_index=int(stream["index"]),
            codec_name=stream.get("codec_name") or "unknown",
            language=tags.get("language"),
            title=tags.get("title"),
            is_default=bool(disposition.get("default")),
            is_forced=bool(disposition.get("forced")),
        ))
    return subtitles


def streams_of_type(data: Dict, codec_type: str) -> List[Dict]:
    return [s for s in data.get("streams", []) if s.get("codec_type") == codec_type]


def format_duration(data: Dict) -> Optional[float]:
    try:
        return float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Video preservation checks
# --------------------------------------------------------------------------

VIDEO_SIGNATURE_FIELDS = (
    "codec_name", "profile", "level", "width", "height", "pix_fmt",
    "color_range", "color_space", "color_transfer", "color_primaries",
    "chroma_location", "field_order", "r_frame_rate", "bits_per_raw_sample",
)

# Side data that describes HDR / Dolby Vision and must survive remuxing.
_IGNORED_SIDE_DATA_KEYS = {"displaymatrix"}


def video_signature(stream: Dict) -> Dict:
    """Return the stable, format-relevant properties of a video stream."""
    signature = {field: stream.get(field) for field in VIDEO_SIGNATURE_FIELDS}
    side_data = []
    for item in stream.get("side_data_list", []) or []:
        side_data.append({k: v for k, v in item.items() if k not in _IGNORED_SIDE_DATA_KEYS})
    signature["side_data"] = sorted(side_data, key=lambda d: json.dumps(d, sort_keys=True))
    return signature


def describe_video(stream: Dict) -> str:
    """Short description such as 'HEVC 3840x2160 smpte2084, Dolby Vision P8.1'."""
    parts = [
        (stream.get("codec_name") or "?").upper(),
        f"{stream.get('width')}x{stream.get('height')}",
    ]
    if stream.get("color_transfer"):
        parts.append(stream["color_transfer"])
    for item in stream.get("side_data_list", []) or []:
        if item.get("side_data_type") == "DOVI configuration record":
            parts.append(f"Dolby Vision profile {item.get('dv_profile')}"
                         f" (compat id {item.get('dv_bl_signal_compatibility_id')})")
    return " ".join(parts)


def video_packet_hashes(
    ffmpeg_path: str,
    file_path: str,
    video_index: int = 0,
    max_packets: int = 300,
    seek: Optional[float] = None,
    timeout: int = 600,
) -> List[Tuple[str, str]]:
    """Return (size, md5) for the first packets of a video stream.

    Uses stream copy, so it hashes the packet payload exactly as stored,
    including in-band HDR10+/Dolby Vision RPU NAL units.
    """
    cmd = [ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error"]
    if seek is not None:
        cmd += ["-ss", f"{seek:.6f}"]
    cmd += [
        "-i", file_path, "-map", f"0:v:{video_index}", "-c", "copy",
        "-frames:v", str(max_packets), "-f", "framemd5", "-",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=subprocess_flags(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not hash video packets: {result.stderr.strip()[-300:]}")
    hashes = []
    for line in result.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) >= 6:
            hashes.append((fields[4], fields[5]))
    return hashes


# Packets are interleaved within a few seconds in a Matroska file (FFmpeg's
# default max_interleave_delta is 10 s), so once the read is this far past the
# window every packet inside the window has been delivered.
PACKET_WINDOW_MARGIN = 20.0


def parse_packet_hashes(lines, stream_indices: Sequence[int], window: Optional[float] = None,
                        margin: float = PACKET_WINDOW_MARGIN) -> Dict[int, List[Tuple[str, str, float]]]:
    """Parse framemd5 lines. With ``window``, keep packets up to ``window``
    seconds after the first one and stop consuming ``lines`` once a packet is
    ``margin`` seconds beyond that."""
    hashes: Dict[int, List[Tuple[str, str, float]]] = {index: [] for index in stream_indices}
    time_bases: Dict[int, float] = {}
    first = None
    for line in lines:
        line = line.rstrip("\r\n")
        if line.startswith("#tb "):                     # "#tb 0: 1/1000"
            key, _, value = line[4:].partition(":")
            num, _, den = value.strip().partition("/")
            try:
                time_bases[int(key)] = int(num) / int(den)
            except (ValueError, ZeroDivisionError):
                pass
            continue
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) >= 6 and fields[0].isdigit() and int(fields[0]) < len(stream_indices):
            k = int(fields[0])
            try:
                dts = int(fields[1]) * time_bases.get(k, 0.0)
            except ValueError:
                dts = 0.0
            first = dts if first is None else min(first, dts)
            if window is not None and dts - first > window:
                if dts - first > window + margin:
                    break
                continue
            hashes[stream_indices[k]].append((fields[4], fields[5], dts))
    return hashes


def stream_packet_hashes(
    ffmpeg_path: str,
    file_path: str,
    stream_indices: Sequence[int],
    seconds: float,
    seek: Optional[float] = None,
    timeout: int = 1800,
) -> Dict[int, List[Tuple[str, str, float]]]:
    """(size, md5, dts seconds) of every packet of the given streams within a time window.

    One pass with stream copy, so payloads are hashed exactly as the demuxer
    delivers them (after Matroska content decoding such as zlib). Returns a
    dict keyed by the requested stream index.

    FFmpeg's input ``-t`` ends each stream separately: a subtitle track with
    no event after the window never ends, and FFmpeg then reads the whole
    film (an hour on a USB hard disk). So no ``-t`` is given: the output is
    read as it arrives, packets after the window are ignored, and FFmpeg is
    stopped once the read is PACKET_WINDOW_MARGIN past the window - only the
    start of the file is ever read.
    """
    cmd = [ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error"]
    if seek is not None:
        cmd += ["-ss", f"{seek:.6f}"]
    cmd += ["-i", file_path]
    for index in stream_indices:
        cmd += ["-map", f"0:{index}"]
    cmd += ["-c", "copy", "-f", "framemd5", "-"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=subprocess_flags())
    stderr: List[str] = []
    drain = threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True)
    drain.start()
    timed_out = threading.Event()

    def on_timeout():
        timed_out.set()
        proc.kill()
    timer = threading.Timer(timeout, on_timeout)
    timer.start()
    stopped_early = False
    try:
        hashes = parse_packet_hashes(proc.stdout, stream_indices, seconds)
        if proc.poll() is None:
            stopped_early = True       # window passed: no need to read the rest of the film
            proc.kill()
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        drain.join(timeout=5)
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    if not stopped_early and proc.returncode != 0:
        raise RuntimeError(f"Could not read packets: {''.join(stderr).strip()[-300:]}")
    return hashes


def keyframe_at_or_before(ffprobe_path: str, file_path: str, target: float,
                          video_index: int = 0) -> Optional[float]:
    """Find the timestamp of the last video keyframe at or before ``target``."""
    if target <= 0:
        return 0.0
    window_start = max(0.0, target - 30.0)
    cmd = [
        ffprobe_path, "-v", "error", "-select_streams", f"v:{video_index}",
        "-read_intervals", f"{window_start:.3f}%{target + 0.5:.3f}",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", file_path,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, creationflags=subprocess_flags(),
    )
    best = None
    for line in result.stdout.splitlines():
        parts = line.split(",")
        if len(parts) < 2 or "K" not in parts[1]:
            continue
        try:
            pts = float(parts[0])
        except ValueError:
            continue
        if pts <= target + 1e-6 and (best is None or pts > best):
            best = pts
    return best


def parse_timecode(text: str) -> float:
    """Parse 'SS', 'MM:SS' or 'HH:MM:SS(.fff)' into seconds."""
    text = (text or "").strip()
    if not text:
        return 0.0
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Invalid time '{text}'")
    seconds = 0.0
    for part in parts:
        if part == "" or float(part) < 0:
            raise ValueError(f"Invalid time '{text}'")
        seconds = seconds * 60 + float(part)
    return seconds


def format_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def tag(stream: Dict, name: str) -> Optional[str]:
    tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
    return tags.get(name.lower())


def summarize_streams(data: Dict) -> Sequence[str]:
    """Lines describing every stream (for the activity log)."""
    lines = []
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        desc = f"#{s.get('index')} {kind}: {s.get('codec_name')}"
        if kind == "audio":
            desc += f" {classify_audio(s)} {s.get('channel_layout')}"
        lang = tag(s, "language")
        if lang:
            desc += f" [{lang}]"
        lines.append(desc)
    return lines
