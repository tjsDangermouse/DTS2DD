"""Read-only media inspection for the Library Scanner.

Combines:
  * ffprobe JSON (always; via media_probe.run_ffprobe),
  * MediaInfo JSON (optional, when the ``mediainfo`` CLI is found),
  * ``mkvmerge -J`` from MKVToolNix (optional, MKV only),
  * a few decoded video frames for HDR10+ / mastering metadata (optional),
  * short stream-copied samples of Dolby tracks for atmos_inspector.

Nothing here writes to the media file. Every external call has a timeout
and honours a cancel event.
"""
import glob
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence

from app_paths import app_dir, exe_name
from app_settings import get_setting
from atmos_inspector import (
    AtmosAnalysis, analyse_ac3, analyse_eac3, analyse_truehd,
    summarize_ac3_bitstream, summarize_truehd_bitstream,
)
from media_probe import bit_depth_of, classify_audio, find_ffprobe, subprocess_flags, tag

# Bump when the stored report changes so cached results are re-inspected.
INSPECTOR_VERSION = 3

SUPPORTED_EXTENSIONS = (".mkv", ".mp4", ".m4v")
SAMPLE_SECONDS = 4.0          # length of each bitstream sample
SAMPLE_MAX_BYTES = 8 * 1024 * 1024


class InspectionCancelled(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@dataclass
class ToolSet:
    ffmpeg: Optional[str]
    ffprobe: Optional[str]
    mediainfo: Optional[str] = None
    mkvmerge: Optional[str] = None

    def describe(self) -> str:
        parts = []
        for name, path in (("ffprobe", self.ffprobe), ("MediaInfo", self.mediainfo),
                           ("MKVToolNix", self.mkvmerge)):
            parts.append(f"{name} {'✓' if path else '✗'}")
        return "  ".join(parts)


def _first_existing(candidates: Sequence[str]) -> Optional[str]:
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def find_mediainfo(configured: Optional[str] = None) -> Optional[str]:
    """MediaInfo command-line tool (the GUI app cannot print JSON)."""
    candidates = [configured, shutil.which("mediainfo"),
                  str(app_dir() / "mediainfo" / exe_name("mediainfo"))]
    if platform.system() == "Windows":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            candidates.append(os.path.join(base, "MediaInfo_CLI", "MediaInfo.exe"))
            candidates.append(os.path.join(base, "MediaInfo CLI", "MediaInfo.exe"))
    else:
        candidates += ["/opt/homebrew/bin/mediainfo", "/usr/local/bin/mediainfo", "/usr/bin/mediainfo"]
    return _first_existing(candidates)


def find_mkvmerge(configured: Optional[str] = None) -> Optional[str]:
    candidates = [configured, shutil.which("mkvmerge"),
                  str(app_dir() / "mkvtoolnix" / exe_name("mkvmerge"))]
    if platform.system() == "Windows":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            candidates.append(os.path.join(base, "MKVToolNix", "mkvmerge.exe"))
    elif platform.system() == "Darwin":
        candidates += sorted(glob.glob("/Applications/MKVToolNix*.app/Contents/MacOS/mkvmerge"), reverse=True)
        candidates += ["/opt/homebrew/bin/mkvmerge", "/usr/local/bin/mkvmerge"]
    else:
        candidates += ["/usr/bin/mkvmerge", "/usr/local/bin/mkvmerge"]
    return _first_existing(candidates)


def discover_tools(ffmpeg_path: Optional[str], ffprobe_path: Optional[str] = None) -> ToolSet:
    return ToolSet(
        ffmpeg=ffmpeg_path,
        ffprobe=ffprobe_path or find_ffprobe(ffmpeg_path),
        mediainfo=find_mediainfo(get_setting("mediainfo_path")),
        mkvmerge=find_mkvmerge(get_setting("mkvmerge_path")),
    )


# --------------------------------------------------------------------------
# Subprocess helper with cancellation and output cap
# --------------------------------------------------------------------------

def run_capture(cmd: Sequence[str], timeout: float = 120, cancel: Optional[threading.Event] = None,
                max_bytes: Optional[int] = None, text: bool = False):
    """Run ``cmd`` and return (returncode, stdout, stderr).

    stdout is truncated at ``max_bytes`` (the process is then stopped).
    Raises InspectionCancelled or subprocess.TimeoutExpired.
    """
    proc = subprocess.Popen(list(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=subprocess_flags())
    chunks, size = [], 0
    stderr_parts = []

    def drain_stderr():
        for line in proc.stderr:
            if sum(len(p) for p in stderr_parts) < 64 * 1024:
                stderr_parts.append(line)

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    def reader():
        nonlocal size
        while True:
            block = proc.stdout.read(65536)
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if max_bytes and size >= max_bytes:
                break

    r = threading.Thread(target=reader, daemon=True)
    r.start()
    started = time.monotonic()
    try:
        while r.is_alive():
            r.join(0.2)
            if cancel is not None and cancel.is_set():
                raise InspectionCancelled("Scan cancelled")
            if time.monotonic() - started > timeout:
                raise subprocess.TimeoutExpired(cmd, timeout)
        if proc.poll() is None:
            if max_bytes and size >= max_bytes:
                proc.kill()
            try:
                proc.wait(timeout=max(1.0, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                proc.kill()
                raise
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t.join(timeout=2)
    out = b"".join(chunks)
    if max_bytes:
        out = out[:max_bytes]
    err = b"".join(stderr_parts)
    if text:
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    return proc.returncode, out, err.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Report model
# --------------------------------------------------------------------------

@dataclass
class VideoTrack:
    index: int
    codec: str
    profile: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    pix_fmt: Optional[str] = None
    bit_depth: Optional[int] = None
    color_transfer: Optional[str] = None
    color_primaries: Optional[str] = None
    hdr_formats: List[str] = field(default_factory=list)   # "Dolby Vision", "HDR10", "HDR10+", "HLG"
    dv_profile: Optional[int] = None
    dv_level: Optional[int] = None
    dv_compat_id: Optional[int] = None
    dv_rpu: Optional[bool] = None
    dv_el: Optional[bool] = None
    dv_bl: Optional[bool] = None
    mastering_metadata: Optional[bool] = None     # from decoded frames (None = not checked)
    hdr10plus_checked: bool = False
    attached_pic: bool = False
    title: Optional[str] = None
    mediainfo_hdr: Optional[str] = None

    @property
    def label(self) -> str:
        hdr = "/".join(self.hdr_formats) or "SDR"
        if self.dv_profile is not None:
            hdr = hdr.replace("Dolby Vision", f"DV P{self.dv_profile}"
                              + (f".{self.dv_compat_id}" if self.dv_compat_id is not None else ""))
        size = f" {self.width}x{self.height}" if self.width else ""
        return f"{self.codec.upper()}{size} {hdr}"


@dataclass
class AudioTrack:
    index: int
    audio_index: int
    codec: str
    format_name: str
    profile: Optional[str] = None
    channels: Optional[int] = None
    channel_layout: Optional[str] = None
    sample_rate: Optional[int] = None
    sample_fmt: Optional[str] = None
    bit_depth: Optional[int] = None
    bit_rate: Optional[int] = None
    language: Optional[str] = None
    title: Optional[str] = None
    default: bool = False
    forced: bool = False
    commentary: bool = False
    visual_impaired: bool = False
    codec_id: Optional[str] = None          # Matroska CodecID (ffprobe tag or mkvmerge)
    atmos: Optional[AtmosAnalysis] = None
    bitstream: Optional[Dict] = None        # Eac3Summary / TrueHDSummary as dict
    mediainfo: Optional[Dict] = None        # selected MediaInfo fields

    @property
    def label(self) -> str:
        name = self.format_name
        if self.atmos and self.atmos.atmos:
            name = "TrueHD Atmos" if self.codec == "truehd" else "DD+ Atmos"
        ch = self.channel_layout or (f"{self.channels}ch" if self.channels else "?")
        return f"{name} {ch}"


@dataclass
class SubtitleTrack:
    index: int
    sub_index: int
    codec: str
    language: Optional[str] = None
    title: Optional[str] = None
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False
    commentary: bool = False
    frames: Optional[int] = None            # from Matroska statistics tags when present
    codec_id: Optional[str] = None

    @property
    def is_pgs(self) -> bool:
        return self.codec == "hdmv_pgs_subtitle"


@dataclass
class MediaReport:
    path: str
    size: int
    mtime_ns: int
    container: str = ""
    duration: Optional[float] = None
    video: List[VideoTrack] = field(default_factory=list)
    audio: List[AudioTrack] = field(default_factory=list)
    subtitles: List[SubtitleTrack] = field(default_factory=list)
    chapters: int = 0
    attachments: int = 0
    tools: Dict[str, bool] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    raw_streams: List[Dict] = field(default_factory=list)   # trimmed ffprobe stream entries
    inspector_version: int = INSPECTOR_VERSION

    @property
    def is_mkv(self) -> bool:
        return self.path.lower().endswith(".mkv")

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def audio_by_index(self, index: int) -> Optional[AudioTrack]:
        return next((a for a in self.audio if a.index == index), None)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "MediaReport":
        data = dict(data)
        data["video"] = [VideoTrack(**_known(VideoTrack, v)) for v in data.get("video", [])]
        audio = []
        for a in data.get("audio", []):
            a = _known(AudioTrack, a)
            a["atmos"] = AtmosAnalysis.from_dict(a.get("atmos"))
            audio.append(AudioTrack(**a))
        data["audio"] = audio
        data["subtitles"] = [SubtitleTrack(**_known(SubtitleTrack, s)) for s in data.get("subtitles", [])]
        return cls(**_known(cls, data))


def _known(cls, data: Dict) -> Dict:
    return {k: v for k, v in data.items() if k in cls.__dataclass_fields__}


# --------------------------------------------------------------------------
# ffprobe -> report
# --------------------------------------------------------------------------

def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flag(stream: Dict, name: str) -> bool:
    return bool((stream.get("disposition") or {}).get(name))


def _stat_frames(stream: Dict) -> Optional[int]:
    """NUMBER_OF_FRAMES statistics tag written by mkvmerge (any language suffix)."""
    for key, value in (stream.get("tags") or {}).items():
        if key.upper().startswith("NUMBER_OF_FRAMES"):
            return _int(value)
    return None


_RAW_KEYS = ("index", "codec_type", "codec_name", "codec_long_name", "profile", "codec_tag_string",
             "width", "height", "pix_fmt", "color_range", "color_space", "color_transfer",
             "color_primaries", "sample_fmt", "sample_rate", "channels", "channel_layout",
             "bits_per_raw_sample", "bit_rate", "start_time", "disposition", "tags", "side_data_list")


def _trim_stream(stream: Dict) -> Dict:
    trimmed = {k: stream[k] for k in _RAW_KEYS if k in stream}
    if "disposition" in trimmed:
        trimmed["disposition"] = {k: v for k, v in trimmed["disposition"].items() if v}
    return trimmed


def report_from_probe(path: str, probe: Dict, size: int = 0, mtime_ns: int = 0) -> MediaReport:
    fmt = probe.get("format") or {}
    report = MediaReport(path=path, size=size, mtime_ns=mtime_ns,
                         container=fmt.get("format_name") or "",
                         chapters=len(probe.get("chapters") or []))
    try:
        report.duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        report.duration = None
    audio_index = sub_index = 0
    for s in probe.get("streams", []):
        kind = s.get("codec_type")
        report.raw_streams.append(_trim_stream(s))
        if kind == "video":
            report.video.append(_video_from_probe(s))
        elif kind == "audio":
            codec = (s.get("codec_name") or "unknown").lower()
            report.audio.append(AudioTrack(
                index=int(s["index"]), audio_index=audio_index, codec=codec,
                format_name=classify_audio(s), profile=s.get("profile"),
                channels=_int(s.get("channels")), channel_layout=s.get("channel_layout") or None,
                sample_rate=_int(s.get("sample_rate")), sample_fmt=s.get("sample_fmt"),
                bit_depth=bit_depth_of(s), bit_rate=_int(s.get("bit_rate")),
                language=tag(s, "language"), title=tag(s, "title"),
                default=_flag(s, "default"), forced=_flag(s, "forced"),
                commentary=_flag(s, "comment"), visual_impaired=_flag(s, "visual_impaired"),
            ))
            audio_index += 1
        elif kind == "subtitle":
            report.subtitles.append(SubtitleTrack(
                index=int(s["index"]), sub_index=sub_index, codec=s.get("codec_name") or "unknown",
                language=tag(s, "language"), title=tag(s, "title"),
                default=_flag(s, "default"), forced=_flag(s, "forced"),
                hearing_impaired=_flag(s, "hearing_impaired"), commentary=_flag(s, "comment"),
                frames=_stat_frames(s),
            ))
            sub_index += 1
        elif kind == "attachment":
            report.attachments += 1
    return report


def _video_from_probe(s: Dict) -> VideoTrack:
    v = VideoTrack(
        index=int(s["index"]), codec=s.get("codec_name") or "unknown", profile=s.get("profile"),
        width=_int(s.get("width")), height=_int(s.get("height")), pix_fmt=s.get("pix_fmt"),
        bit_depth=_int(s.get("bits_per_raw_sample")), color_transfer=s.get("color_transfer"),
        color_primaries=s.get("color_primaries"), attached_pic=_flag(s, "attached_pic"),
        title=tag(s, "title"),
    )
    for item in s.get("side_data_list") or []:
        kind = item.get("side_data_type") or ""
        if kind == "DOVI configuration record":
            v.dv_profile = _int(item.get("dv_profile"))
            v.dv_level = _int(item.get("dv_level"))
            v.dv_compat_id = _int(item.get("dv_bl_signal_compatibility_id"))
            v.dv_rpu = bool(_int(item.get("rpu_present_flag")))
            v.dv_el = bool(_int(item.get("el_present_flag")))
            v.dv_bl = bool(_int(item.get("bl_present_flag")))
        elif kind == "Mastering display metadata":
            v.mastering_metadata = True
    formats = []
    if v.dv_profile is not None:
        formats.append("Dolby Vision")
    if v.color_transfer == "smpte2084":
        # PQ base layer. Strictly HDR10 also needs BT.2020 + static metadata;
        # the metadata check is done on decoded frames when enabled.
        if v.dv_profile is None or v.dv_compat_id in (1, 6):
            formats.append("HDR10")
    elif v.color_transfer == "arib-std-b67":
        formats.append("HLG")
    v.hdr_formats = formats
    return v


# --------------------------------------------------------------------------
# Optional sources
# --------------------------------------------------------------------------

MEDIAINFO_AUDIO_FIELDS = ("Format", "Format_Commercial_IfAny", "Format_AdditionalFeatures",
                          "Format_Profile", "Format_Settings", "ComplexityIndex",
                          "NumberOfDynamicObjects", "BedChannelCount", "Channels",
                          "ChannelLayout", "BitDepth", "CodecID")
MEDIAINFO_VIDEO_FIELDS = ("HDR_Format", "HDR_Format_Profile", "HDR_Format_Level",
                          "HDR_Format_Settings", "HDR_Format_Compatibility", "transfer_characteristics",
                          "MasteringDisplay_ColorPrimaries", "MaxCLL", "CodecID")


def run_mediainfo(mediainfo: str, path: str, cancel=None) -> Optional[Dict]:
    code, out, _ = run_capture([mediainfo, "--Output=JSON", path], timeout=180, cancel=cancel, text=True)
    if code != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def mediainfo_tracks(data: Optional[Dict], kind: str) -> List[Dict]:
    if not data:
        return []
    tracks = (data.get("media") or {}).get("track") or []
    return [t for t in tracks if t.get("@type") == kind]


def _match_mediainfo(tracks: List[Dict], stream_index: int, type_index: int) -> Optional[Dict]:
    for t in tracks:
        if str(t.get("StreamOrder", "")).split("-")[-1] == str(stream_index):
            return t
    return tracks[type_index] if type_index < len(tracks) else None


def apply_mediainfo(report: MediaReport, data: Optional[Dict]) -> None:
    audio = mediainfo_tracks(data, "Audio")
    for i, a in enumerate(report.audio):
        t = _match_mediainfo(audio, a.index, i)
        if t is not None:
            a.mediainfo = {k: t[k] for k in MEDIAINFO_AUDIO_FIELDS if t.get(k)}
    video = mediainfo_tracks(data, "Video")
    for i, v in enumerate(report.video):
        t = _match_mediainfo(video, v.index, i)
        if t is None:
            continue
        hdr = " / ".join(str(t[k]) for k in ("HDR_Format", "HDR_Format_Profile", "HDR_Format_Compatibility")
                         if t.get(k))
        v.mediainfo_hdr = hdr or None
        hdr_text = str(t.get("HDR_Format") or "") + " " + str(t.get("HDR_Format_Compatibility") or "")
        if "HDR10+" in hdr_text or "SMPTE ST 2094 App 4" in hdr_text:
            if "HDR10+" not in v.hdr_formats:
                v.hdr_formats.append("HDR10+")
        if "Dolby Vision" in hdr_text and "Dolby Vision" not in v.hdr_formats:
            v.hdr_formats.insert(0, "Dolby Vision")


def run_mkvmerge(mkvmerge: str, path: str, cancel=None) -> Optional[Dict]:
    code, out, _ = run_capture([mkvmerge, "-J", path], timeout=180, cancel=cancel, text=True)
    if code not in (0, 1) or not out.strip():      # 1 = warnings
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def apply_mkvmerge(report: MediaReport, data: Optional[Dict]) -> None:
    if not data:
        return
    tracks = {t.get("id"): t for t in data.get("tracks") or []}
    for track in list(report.audio) + list(report.subtitles):
        info = tracks.get(track.index)
        if not info:
            continue
        props = info.get("properties") or {}
        track.codec_id = props.get("codec_id") or track.codec_id
        if isinstance(track, SubtitleTrack):
            track.hearing_impaired = track.hearing_impaired or bool(props.get("flag_hearing_impaired"))
            track.forced = track.forced or bool(props.get("forced_track"))
            if track.frames is None:
                track.frames = _int(props.get("num_index_entries"))
        if not track.language and props.get("language") not in (None, "und"):
            track.language = props.get("language")


def check_video_frames(ffprobe: str, path: str, video: VideoTrack, cancel=None) -> None:
    """Decode a couple of frames to read HDR10+ / mastering-display side data."""
    cmd = [ffprobe, "-v", "error", "-select_streams", f"{video.index}", "-read_intervals", "%+#2",
           "-show_frames", "-show_entries", "frame=side_data_list", "-of", "json", path]
    try:
        code, out, _ = run_capture(cmd, timeout=90, cancel=cancel, text=True)
    except subprocess.TimeoutExpired:
        return
    if code != 0:
        return
    try:
        frames = json.loads(out or "{}").get("frames") or []
    except json.JSONDecodeError:
        return
    video.hdr10plus_checked = True
    types = {item.get("side_data_type", "") for f in frames for item in f.get("side_data_list") or []}
    if any("SMPTE2094-40" in t or "HDR10+" in t for t in types) and "HDR10+" not in video.hdr_formats:
        video.hdr_formats.append("HDR10+")
    if any("Mastering display" in t for t in types):
        video.mastering_metadata = True
    elif video.mastering_metadata is None:
        video.mastering_metadata = False


def read_stream_sample(ffmpeg: str, path: str, stream_index: int, muxer: str,
                       start: Optional[float] = None, seconds: float = SAMPLE_SECONDS,
                       cancel=None) -> bytes:
    """Stream-copy a few seconds of one track as a raw elementary stream."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path, "-map", f"0:{stream_index}", "-c", "copy", "-t", f"{seconds:.3f}",
            "-f", muxer, "-"]
    try:
        code, out, _ = run_capture(cmd, timeout=120, cancel=cancel, max_bytes=SAMPLE_MAX_BYTES)
    except subprocess.TimeoutExpired:
        return b""
    return out


def _sample_points(duration: Optional[float]) -> List[Optional[float]]:
    """Start of the film plus the middle, so a JOC flag is checked twice."""
    if duration and duration > 120:
        return [None, duration / 2]
    return [None]


def analyse_dolby_track(ffmpeg: Optional[str], path: str, track: AudioTrack, duration: Optional[float],
                        cancel=None, bitstream: bool = True) -> None:
    if track.codec in ("eac3", "ac3"):
        summary = None
        if bitstream and ffmpeg:
            data = b"".join(read_stream_sample(ffmpeg, path, track.index, track.codec, start, cancel=cancel)
                            for start in _sample_points(duration))
            summary = summarize_ac3_bitstream(data)
            track.bitstream = summary.to_dict()
        if track.codec == "eac3":
            track.atmos = analyse_eac3(track.profile, track.title, summary, track.mediainfo)
        else:
            track.atmos = analyse_ac3(track.title, summary)
    elif track.codec == "truehd":
        summary = None
        if bitstream and ffmpeg:
            data = b"".join(read_stream_sample(ffmpeg, path, track.index, "truehd", start, cancel=cancel)
                            for start in _sample_points(duration))
            summary = summarize_truehd_bitstream(data)
            track.bitstream = summary.to_dict()
        track.atmos = analyse_truehd(track.profile, track.title, summary, track.mediainfo)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

@dataclass
class InspectOptions:
    bitstream: bool = True          # parse E-AC-3 / TrueHD samples for Atmos
    video_frames: bool = True       # decode 2 frames for HDR10+ / mastering metadata
    use_mediainfo: bool = True
    use_mkvmerge: bool = True


def inspect_file(path: str, tools: ToolSet, options: Optional[InspectOptions] = None,
                 cancel: Optional[threading.Event] = None) -> MediaReport:
    """Inspect one file. Never raises for media problems; they go to report.errors."""
    from media_probe import run_ffprobe

    options = options or InspectOptions()
    path = os.path.abspath(path)
    st = os.stat(path)
    if not tools.ffprobe:
        report = MediaReport(path=path, size=st.st_size, mtime_ns=st.st_mtime_ns)
        report.errors.append("ffprobe is not available; the file could not be analysed")
        return report
    try:
        probe = run_ffprobe(tools.ffprobe, path, timeout=180)
    except RuntimeError as e:
        report = MediaReport(path=path, size=st.st_size, mtime_ns=st.st_mtime_ns)
        report.errors.append(str(e))
        return report

    report = report_from_probe(path, probe, st.st_size, st.st_mtime_ns)
    report.tools = {"ffprobe": True, "mediainfo": False, "mkvmerge": False}
    _check_cancel(cancel)

    if options.use_mediainfo and tools.mediainfo:
        try:
            data = run_mediainfo(tools.mediainfo, path, cancel)
            apply_mediainfo(report, data)
            report.tools["mediainfo"] = data is not None
        except subprocess.TimeoutExpired:
            report.notes.append("MediaInfo timed out")
    _check_cancel(cancel)

    if options.use_mkvmerge and tools.mkvmerge and report.is_mkv:
        try:
            data = run_mkvmerge(tools.mkvmerge, path, cancel)
            apply_mkvmerge(report, data)
            report.tools["mkvmerge"] = data is not None
        except subprocess.TimeoutExpired:
            report.notes.append("mkvmerge timed out")
    _check_cancel(cancel)

    if options.video_frames:
        for v in report.video:
            if not v.attached_pic and v.color_transfer == "smpte2084":
                check_video_frames(tools.ffprobe, path, v, cancel)
                _check_cancel(cancel)

    for a in report.audio:
        if a.codec in ("eac3", "ac3", "truehd"):
            analyse_dolby_track(tools.ffmpeg, path, a, report.duration, cancel, options.bitstream)
            _check_cancel(cancel)
    return report


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise InspectionCancelled("Scan cancelled")
