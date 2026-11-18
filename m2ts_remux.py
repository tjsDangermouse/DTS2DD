"""MKV -> M2TS remux with tsMuxeR (experimental Panasonic compatibility test).

This is a container change only:
  * video, audio and PGS subtitle bitstreams are copied by tsMuxeR (SRT is
    rendered to PGS by tsMuxeR when the user ticks it);
  * nothing is decoded and re-encoded, no AC-3 core is created, and Dolby
    TrueHD is never replaced by another format;
  * the source MKV is only read. The M2TS is written to a hidden temporary
    file beside the chosen output, verified, and then renamed. An existing
    file is only replaced when the caller passes ``overwrite=True`` (the UI
    asks first).

Verification (after tsMuxeR has finished) reads both files with the
standard FFmpeg/ffprobe:
  * every selected track is present with the same codec and channel count;
  * video: format, size and HDR signalling compared; decoded frames at the
    start and middle compared (identical frames = video copied unchanged);
  * audio: the raw elementary streams of TrueHD / AC-3 / E-AC-3 / DTS are
    compared byte for byte (windows at the start and middle, plus the whole
    TrueHD / DTS-HD stream when "full comparison" is on);
  * Atmos: the TrueHD major sync / E-AC-3 JOC signalling is parsed again
    from the M2TS (atmos_inspector);
  * HDR: the Dolby Vision configuration record and the per-frame side data
    (Dolby Vision RPU, HDR10+, mastering display, content light level) are
    compared on sample frames.

None of this proves that a particular player will play the file; the
report always says "Panasonic playback not yet tested".
"""
import glob
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from atmos_inspector import (
    STATUS_DDP_ATMOS, STATUS_TRUEHD_ATMOS, analyse_eac3, analyse_truehd, summarize_ac3_bitstream,
    summarize_truehd_bitstream,
)
from audio_stream_info import SUBTITLE_CODEC_NAMES
from compatibility_rules import ScannerConfig, language_label, normalize_language, subtitle_cleanup_plan
from ffmpeg_helper import ConversionCancelled, ProcessHandle, format_size
from media_inspector import (
    InspectOptions, MediaReport, ToolSet, inspect_file, run_capture,
)
from media_probe import format_duration, format_seconds, run_ffprobe, streams_of_type, subprocess_flags, tag
from tsmuxer_tool import (
    MetaTrack, MuxProgress, TsDetection, TsMuxerError, TsMuxerInfo, TsTrack, build_meta, detect_tracks,
    fps_value, run_tsmuxer, write_meta,
)

PARTIAL_INFIX = ".remuxing-"
DISK_MARGIN_BYTES = 512 * 1024 * 1024
TS_OVERHEAD = 1.04           # 192-byte M2TS packets + PES headers (estimate)

OUTCOME_REMUXED = "Remux completed"
OUTCOME_AUDIO_VERIFIED = "Audio streams verified"
OUTCOME_AUDIO_PRESENT = "Audio streams present (not every track bit-compared)"
OUTCOME_ATMOS = "Atmos metadata detected"
OUTCOME_ATMOS_MISSING = "Atmos metadata NOT detected in the M2TS"
OUTCOME_HDR_SAME = "HDR metadata unchanged on the frames checked"
OUTCOME_HDR_CHANGED = "HDR metadata changed or unverified"
OUTCOME_SDR = "No HDR metadata (SDR video)"
OUTCOME_PANASONIC = "Panasonic playback not yet tested"

# ffprobe codec -> tsMuxeR meta codec names that may carry it.
TS_CODECS = {
    "hevc": ("V_MPEGH/ISO/HEVC",), "h264": ("V_MPEG4/ISO/AVC",), "vc1": ("V_MS/VFW/WVC1",),
    "mpeg2video": ("V_MPEG-2",), "vvc": ("V_MPEGI/ISO/VVC",),
    "truehd": ("A_MLP", "A_AC3"), "ac3": ("A_AC3",), "eac3": ("A_AC3",), "dts": ("A_DTS",),
    "aac": ("A_AAC",), "mp3": ("A_MP3",), "mp2": ("A_MP3",),
    "hdmv_pgs_subtitle": ("S_HDMV/PGS",), "subrip": ("S_TEXT/UTF8",),
}
UNSUPPORTED = {
    "flac": "FLAC cannot be carried in M2TS by tsMuxeR",
    "opus": "Opus cannot be carried in M2TS by tsMuxeR",
    "vorbis": "Vorbis cannot be carried in M2TS by tsMuxeR",
    "alac": "ALAC cannot be carried in M2TS by tsMuxeR",
    "ass": "ASS/SSA styled subtitles are not supported by tsMuxeR (only PGS and SRT)",
    "ssa": "ASS/SSA styled subtitles are not supported by tsMuxeR (only PGS and SRT)",
    "dvd_subtitle": "DVD (VobSub) subtitles are not supported by tsMuxeR",
    "webvtt": "WebVTT subtitles are not supported by tsMuxeR",
    "mov_text": "MP4 text subtitles are not supported by tsMuxeR",
    "av1": "AV1 video cannot be put in M2TS without re-encoding (never done)",
    "vp9": "VP9 video cannot be put in M2TS without re-encoding (never done)",
}
# Output codec expected in the M2TS for each source codec.
OUTPUT_CODEC = {"subrip": "hdmv_pgs_subtitle", "mp2": ("mp2", "mp3"), "mp3": ("mp2", "mp3")}
# Raw elementary-stream muxers used for byte comparison.
RAW_MUXER = {"truehd": "truehd", "ac3": "ac3", "eac3": "eac3", "dts": "dts"}
FULL_COMPARE_CODECS = ("truehd", "dts")
SAMPLE_SECONDS = 8.0


class RemuxError(RuntimeError):
    pass


class OutputExists(RemuxError):
    pass


class VerificationFailed(RemuxError):
    def __init__(self, message: str, report: str = ""):
        super().__init__(message)
        self.report = report


def _is_pcm(codec: str) -> bool:
    return codec.startswith("pcm_")


def ts_codecs_for(codec: str) -> Tuple[str, ...]:
    return ("A_LPCM",) if _is_pcm(codec) else TS_CODECS.get(codec, ())


def default_output_path(source: str) -> str:
    p = Path(source)
    return str(p.with_suffix(".m2ts"))


def find_stale_partials(output_path: str) -> List[str]:
    """Temporary M2TS files left by an interrupted remux to ``output_path``."""
    out = Path(output_path)
    return sorted(glob.glob(str(out.with_name(f".{glob.escape(out.stem)}{PARTIAL_INFIX}*.m2ts"))))


# --------------------------------------------------------------------------
# HDR snapshot (stream configuration + per-frame side data)
# --------------------------------------------------------------------------

FRAME_KEYS = {
    "rpu": ("Dolby Vision RPU Data",),
    "hdr10plus": ("SMPTE2094-40", "HDR10+"),
    "mastering": ("Mastering display metadata",),
    "cll": ("Content light level metadata",),
}
FRAME_LABELS = {"rpu": "Dolby Vision RPU", "hdr10plus": "HDR10+ dynamic metadata",
                "mastering": "HDR10 mastering display metadata", "cll": "HDR10 content light level (MaxCLL)"}


@dataclass
class HdrSnapshot:
    dovi: Optional[Dict] = None                 # profile/level/compat/rpu/el/bl from the stream config
    container: List[str] = field(default_factory=list)   # stream-level side data keys (mastering/cll)
    frames: Dict[str, List[str]] = field(default_factory=dict)   # position label -> keys found
    transfer: Optional[str] = None
    primaries: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    @property
    def frame_keys(self) -> set:
        return {k for keys in self.frames.values() for k in keys}

    @property
    def checked(self) -> bool:
        return bool(self.frames)

    @property
    def is_hdr(self) -> bool:
        return (self.transfer in ("smpte2084", "arib-std-b67") or self.dovi is not None
                or bool(self.frame_keys & {"rpu", "hdr10plus", "mastering"}))

    def dovi_label(self) -> str:
        d = self.dovi
        if not d:
            return "none"
        return (f"profile {d.get('profile')}.{d.get('compat')} level {d.get('level')} "
                f"(RPU {'yes' if d.get('rpu') else 'no'}, EL {'yes' if d.get('el') else 'no'}, "
                f"BL {'yes' if d.get('bl') else 'no'})")


def _dovi_from_stream(stream: Dict) -> Optional[Dict]:
    for item in stream.get("side_data_list") or []:
        if item.get("side_data_type") == "DOVI configuration record":
            def num(k):
                try:
                    return int(item.get(k))
                except (TypeError, ValueError):
                    return None
            return {"profile": num("dv_profile"), "level": num("dv_level"),
                    "compat": num("dv_bl_signal_compatibility_id"), "rpu": bool(num("rpu_present_flag")),
                    "el": bool(num("el_present_flag")), "bl": bool(num("bl_present_flag"))}
    return None


def _container_hdr(stream: Dict) -> List[str]:
    keys = []
    for item in stream.get("side_data_list") or []:
        kind = item.get("side_data_type") or ""
        if kind == "Mastering display metadata":
            keys.append("mastering")
        elif kind == "Content light level metadata":
            keys.append("cll")
    return keys


def frame_side_data(ffprobe: str, path: str, stream_index: int, start: Optional[float] = None,
                    frames: int = 3, cancel=None) -> List[str]:
    """Side-data keys (see FRAME_KEYS) on the first ``frames`` decoded frames from ``start``."""
    interval = (f"{start:.3f}" if start else "") + f"%+#{frames}"
    cmd = [ffprobe, "-v", "error", "-select_streams", str(stream_index), "-read_intervals", interval,
           "-show_frames", "-show_entries", "frame_side_data=side_data_type", "-of", "compact=p=0", path]
    code, out, err = run_capture(cmd, timeout=180, cancel=cancel, text=True)
    if code != 0:
        raise RemuxError(f"ffprobe could not decode video frames: {err.strip().splitlines()[-1:] or code}")
    found = set()
    for key, needles in FRAME_KEYS.items():
        if any(n in out for n in needles):
            found.add(key)
    return sorted(found)


def hdr_snapshot(ffprobe: str, path: str, stream: Dict, duration: Optional[float], cancel=None) -> HdrSnapshot:
    snap = HdrSnapshot(dovi=_dovi_from_stream(stream), container=_container_hdr(stream),
                       transfer=stream.get("color_transfer"), primaries=stream.get("color_primaries"))
    positions = [("start", None)]
    if duration and duration > 60:
        positions.append(("middle", duration / 2))
    for label, start in positions:
        try:
            snap.frames[label] = frame_side_data(ffprobe, path, int(stream["index"]), start, cancel=cancel)
        except (RemuxError, subprocess.TimeoutExpired, OSError) as e:
            snap.errors.append(f"{label}: {e}")
    return snap


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

@dataclass
class PlannedTrack:
    kind: str                       # video / audio / subtitle
    index: int                      # absolute ffprobe stream index in the MKV
    codec: str
    description: str
    language: Optional[str] = None
    ts: Optional[TsTrack] = None
    compatible: bool = False
    handling: str = ""              # how tsMuxeR handles it, or why it cannot
    selected: bool = False
    channels: Optional[int] = None
    atmos: Optional[str] = None     # Atmos status of the source track (atmos_inspector)
    atmos_flag: Optional[bool] = None
    fps: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    forced: bool = False
    bytes: Optional[int] = None     # size of the track in the MKV when known (statistics tags)

    @property
    def label(self) -> str:
        return f"#{self.index} {self.description}"


@dataclass
class M2TSPlan:
    source: str
    output: str
    report: MediaReport
    probe: Dict
    detection: TsDetection
    video: List[PlannedTrack] = field(default_factory=list)
    audio: List[PlannedTrack] = field(default_factory=list)
    subtitles: List[PlannedTrack] = field(default_factory=list)
    source_hdr: Optional[HdrSnapshot] = None
    blockers: List[str] = field(default_factory=list)
    metadata_warnings: List[str] = field(default_factory=list)   # need explicit confirmation
    warnings: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)
    first_audio: Optional[int] = None
    source_size: int = 0
    source_mtime_ns: int = 0

    @property
    def tracks(self) -> List[PlannedTrack]:
        return self.video + self.audio + self.subtitles

    def selected(self, kind: Optional[str] = None) -> List[PlannedTrack]:
        return [t for t in self.tracks if t.selected and (kind is None or t.kind == kind)]

    def ordered_audio(self) -> List[PlannedTrack]:
        """Selected audio, the chosen first track first (M2TS has no default flag)."""
        audio = self.selected("audio")
        first = next((a for a in audio if a.index == self.first_audio), None)
        return ([first] + [a for a in audio if a is not first]) if first else audio

    def excluded(self) -> List[Tuple[PlannedTrack, str]]:
        out = []
        for t in self.tracks:
            if not t.selected:
                out.append((t, t.handling if not t.compatible else "not selected"))
        return out

    def selection_problems(self) -> List[str]:
        problems = list(self.blockers)
        if not self.selected("video"):
            problems.append("No video track is selected.")
        if not self.selected("audio"):
            problems.append("No audio track is selected.")
        for t in self.selected():
            if not t.compatible:
                problems.append(f"{t.kind.title()} {t.label} cannot be remuxed: {t.handling}")
        return problems

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.metadata_warnings or [t for t in self.tracks if not t.compatible])

    def estimated_bytes(self) -> int:
        selected = self.selected()
        if selected and all(t.bytes for t in selected):
            return int(sum(t.bytes for t in selected) * TS_OVERHEAD)
        dropped = sum(t.bytes or 0 for t in self.tracks if not t.selected)
        return int(max(0, self.source_size - dropped) * TS_OVERHEAD)

    def meta_tracks(self) -> List[MetaTrack]:
        result = []
        video = self.selected("video")
        fps = video[0].fps if video else None
        width = video[0].width if video else None
        height = video[0].height if video else None
        for t in video + self.ordered_audio() + self.selected("subtitle"):
            options: List[Tuple[str, str]] = []
            if t.kind == "video" and t.fps:
                options.append(("fps", t.fps))
            if t.kind == "audio" and t.ts and t.ts.delay_ms:
                options.append(("timeshift", f"{t.ts.delay_ms}ms"))
            if t.kind == "subtitle":
                if fps:
                    options.append(("fps", fps))
                if t.codec == "subrip":
                    options += [("font-name", "Arial"), ("font-size", "65"), ("font-color", "0xffffffff"),
                                ("bottom-offset", "24"), ("font-border", "5")]
                    if width and height:
                        options += [("video-width", str(width)), ("video-height", str(height))]
            result.append(MetaTrack(t.ts.codec_id, t.ts.track_id, t.language, options))
        return result

    def meta_text(self) -> str:
        return build_meta(self.source, self.meta_tracks())


def _stat_bytes(stream: Dict, duration: Optional[float]) -> Optional[int]:
    tags = stream.get("tags") or {}
    for key, value in tags.items():
        if key.upper().startswith("NUMBER_OF_BYTES"):
            try:
                return int(value)
            except ValueError:
                pass
    for key, value in tags.items():
        if key.upper() == "BPS" or key.upper().startswith("BPS-"):
            try:
                return int(int(value) / 8 * (duration or 0)) or None
            except ValueError:
                pass
    try:
        return int(int(stream.get("bit_rate")) / 8 * (duration or 0)) or None
    except (TypeError, ValueError):
        return None


def _matches(codec: str, ts: Optional[TsTrack]) -> bool:
    return bool(ts and ts.detected and ts.codec_id in ts_codecs_for(codec))


def _audio_description(a) -> str:
    text = a.label
    if a.language:
        text += f" [{a.language}]"
    if a.title:
        text += f' "{a.title}"'
    if a.default:
        text += " {default}"
    return text


def _subtitle_description(s) -> str:
    text = f"{SUBTITLE_CODEC_NAMES.get(s.codec, s.codec.upper())} {language_label(s.language)}"
    if s.title:
        text += f' "{s.title}"'
    flags = [n for n, on in (("forced", s.forced), ("default", s.default), ("SDH", s.hearing_impaired)) if on]
    if flags:
        text += " {" + ", ".join(flags) + "}"
    return text


def build_plan(source: str, report: MediaReport, probe: Dict, detection: TsDetection,
               source_hdr: Optional[HdrSnapshot] = None, output: Optional[str] = None,
               config: Optional[ScannerConfig] = None, subtitle_mode: str = "preferred") -> M2TSPlan:
    """Work out what tsMuxeR can keep. ``subtitle_mode``: preferred / none / all."""
    config = config or ScannerConfig()
    plan = M2TSPlan(source=os.path.abspath(source), output=output or default_output_path(source),
                    report=report, probe=probe, detection=detection, source_hdr=source_hdr,
                    source_size=report.size, source_mtime_ns=report.mtime_ns)
    duration = report.duration or format_duration(probe)
    streams = {int(s["index"]): s for s in probe.get("streams", [])}

    if not report.is_mkv:
        plan.blockers.append("Only MKV files can be remuxed to M2TS here.")
    if report.errors:
        plan.blockers += report.errors

    mismatches = []

    def ts_for(index: int, codec: str) -> Optional[TsTrack]:
        ts = detection.track(index + 1)          # tsMuxeR: MKV track position, 1-based
        if ts is not None and ts.detected and ts_codecs_for(codec) and ts.codec_id not in ts_codecs_for(codec):
            mismatches.append(f"stream #{index} is {codec} but tsMuxeR track {index + 1} is "
                              f"{ts.codec_id} ({ts.stream_type})")
        return ts

    # ---- video
    for v in report.video:
        if v.attached_pic:
            continue                      # cover art is an attachment, not a track
        s = streams.get(v.index, {})
        ts = ts_for(v.index, v.codec)
        t = PlannedTrack("video", v.index, v.codec, v.label, s.get("tags", {}).get("language"), ts,
                         width=v.width, height=v.height, fps=fps_value(s.get("r_frame_rate")),
                         bytes=_stat_bytes(s, duration))
        if v.codec in UNSUPPORTED:
            t.handling = UNSUPPORTED[v.codec]
        elif not ts_codecs_for(v.codec):
            t.handling = f"{v.codec} video is not supported in M2TS"
        elif not _matches(v.codec, ts):
            t.handling = "tsMuxeR could not identify this track"
        else:
            t.compatible = True
            t.handling = "copied unchanged (no re-encoding)"
        plan.video.append(t)
    compatible_video = [t for t in plan.video if t.compatible]
    if compatible_video:
        compatible_video[0].selected = True
        if not compatible_video[0].fps:
            plan.warnings.append("The frame rate could not be read reliably; tsMuxeR will use the rate "
                                 "in the video stream.")
        if len(compatible_video) > 1:
            plan.warnings.append(f"{len(compatible_video)} video tracks: only the first is remuxed "
                                 "(tick another one in the list to include it).")
    elif plan.video:
        plan.blockers.append(f"The video cannot be remuxed: {plan.video[0].handling}.")
    else:
        plan.blockers.append("The file has no video track.")

    # ---- audio
    for a in report.audio:
        s = streams.get(a.index, {})
        ts = ts_for(a.index, a.codec)
        t = PlannedTrack("audio", a.index, a.codec, _audio_description(a), a.language, ts,
                         channels=a.channels, bytes=_stat_bytes(s, duration))
        if a.atmos:
            t.atmos, t.atmos_flag = a.atmos.status, a.atmos.atmos
        if a.codec in UNSUPPORTED:
            t.handling = UNSUPPORTED[a.codec]
        elif not ts_codecs_for(a.codec):
            t.handling = f"{a.format_name} is not supported by tsMuxeR in M2TS"
        elif not _matches(a.codec, ts):
            t.handling = "tsMuxeR could not identify this track"
        else:
            t.compatible = True
            t.selected = True
            if a.codec == "truehd":
                core = ts.codec_id == "A_AC3"
                t.handling = ("TrueHD copied unchanged with its embedded AC-3 core" if core else
                              "TrueHD copied unchanged (no AC-3 core in this MKV track)")
                if ts.atmos:
                    t.handling += "; tsMuxeR reports Atmos"
                    if a.atmos and a.atmos.atmos is False:
                        plan.warnings.append(f"Audio #{a.index}: tsMuxeR labels it Atmos but the bitstream "
                                             "check found no Atmos presentation; the bitstream result is used.")
            elif _is_pcm(a.codec):
                t.handling = "PCM repacked as Blu-ray LPCM (lossless; samples unchanged)"
            elif a.codec == "dts":
                t.handling = f"{a.format_name} copied unchanged (core and extensions)"
            else:
                t.handling = "copied unchanged"
        plan.audio.append(t)
    if not [t for t in plan.audio if t.compatible]:
        plan.blockers.append("No audio track can be carried in M2TS by tsMuxeR.")
    defaults = [a for a in report.audio if a.default]
    selected_audio = [t for t in plan.audio if t.selected]
    first = next((t for t in selected_audio if defaults and t.index == defaults[0].index), None)
    plan.first_audio = (first or (selected_audio[0] if selected_audio else None)).index \
        if selected_audio else None

    # ---- subtitles
    pgs_like = []
    for sub in report.subtitles:
        ts = ts_for(sub.index, sub.codec)
        t = PlannedTrack("subtitle", sub.index, sub.codec, _subtitle_description(sub), sub.language, ts,
                         forced=sub.forced, bytes=_stat_bytes(streams.get(sub.index, {}), duration))
        if sub.codec in UNSUPPORTED:
            t.handling = UNSUPPORTED[sub.codec]
        elif not ts_codecs_for(sub.codec):
            t.handling = f"{sub.codec} subtitles are not supported by tsMuxeR"
        elif not _matches(sub.codec, ts):
            t.handling = "tsMuxeR could not identify this track"
        else:
            t.compatible = True
            t.handling = ("PGS copied unchanged" if sub.codec == "hdmv_pgs_subtitle" else
                          "SRT text rendered to PGS bitmaps by tsMuxeR (font and layout chosen by "
                          "tsMuxeR; not ticked by default)")
            if sub.codec == "hdmv_pgs_subtitle":
                pgs_like.append(sub)
        plan.subtitles.append(t)
    if subtitle_mode == "all":
        for t in plan.subtitles:
            t.selected = t.compatible and t.codec == "hdmv_pgs_subtitle"
    elif subtitle_mode == "preferred":
        keep = {s.index for s, _ in subtitle_cleanup_plan(pgs_like, config).keep}
        for t in plan.subtitles:
            t.selected = t.index in keep

    if mismatches:
        plan.blockers.append("tsMuxeR numbers the tracks differently than expected, so the right tracks "
                             "cannot be chosen safely: " + "; ".join(mismatches))

    _explain(plan)
    return plan


def _explain(plan: M2TSPlan) -> None:
    report = plan.report
    for t in plan.audio:
        if t.codec == "truehd" and t.ts and t.ts.codec_id == "A_MLP":
            plan.info.append(
                f"Audio #{t.index}: the TrueHD stream is copied as-is (M2TS stream type 0x83). MKV TrueHD "
                "tracks normally have no AC-3 core - on Blu-ray discs one is interleaved in the same stream, "
                "and some players need it. tsMuxeR does not create one and this app will not fabricate one. "
                "The separate AC-3 track (if kept) is its own stream. Whether the Panasonic accepts TrueHD "
                "without a core is part of what this test shows.")
    hdr = plan.source_hdr
    video = next((v for v in report.video if not v.attached_pic), None)
    if video is not None:
        if video.dv_profile is not None:
            dv = f"Dolby Vision profile {video.dv_profile}" + (
                f".{video.dv_compat_id}" if video.dv_compat_id is not None else "")
            msg = (f"{dv}: tsMuxeR copies the RPU in the video stream and writes its own Dolby Vision "
                   "descriptor, working out the profile from the stream (PQ single layer = 8.1). The "
                   "descriptor and RPUs are compared after remuxing. Whether the Panasonic uses Dolby Vision "
                   "from an M2TS over DLNA is not known.")
            if video.dv_profile == 5:
                msg += " Profile 5 has no HDR10 fallback: a player that ignores Dolby Vision shows wrong colours."
            if video.dv_profile == 7:
                msg += (" Profile 7 (dual layer) in one track is not standard outside Blu-ray discs; "
                        "players may use only the HDR10 base layer.")
            plan.metadata_warnings.append(msg)
        if "HDR10+" in video.hdr_formats or (hdr and "hdr10plus" in hdr.frame_keys):
            plan.metadata_warnings.append(
                "HDR10+: the dynamic metadata is part of the video stream (SEI) and is copied unchanged; "
                "tsMuxeR 2.6.16+ handles it. It is checked on sample frames afterwards.")
        if hdr and hdr.checked and "mastering" in hdr.container and "mastering" not in hdr.frame_keys:
            plan.metadata_warnings.append(
                "HDR10 mastering display metadata is stored only in the MKV header, not in the video stream. "
                "M2TS has no such header, so the M2TS will not carry it (tsMuxeR copies the video stream).")
    if report.chapters:
        plan.info.append(f"{report.chapters} chapter(s) not carried: a plain M2TS file has no chapters.")
    if report.attachments:
        plan.info.append(f"{report.attachments} attachment(s) (fonts / cover art) are not carried.")
    plan.info.append("M2TS has no track names or default flags: players usually start with the first audio "
                     "track, so choose which one comes first.")
    excluded = [t for t in plan.tracks if not t.compatible]
    for t in excluded:
        plan.warnings.append(f"{t.kind.title()} {t.label} will be left out: {t.handling}.")


# --------------------------------------------------------------------------
# Raw stream / frame comparison helpers
# --------------------------------------------------------------------------

def raw_sample(ffmpeg: str, path: str, index: int, muxer: str, start: Optional[float], seconds: float,
               cancel=None, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
    if start:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", path, "-map", f"0:{index}", "-c", "copy", "-t", f"{seconds:.3f}", "-f", muxer, "-"]
    code, out, _ = run_capture(cmd, timeout=300, cancel=cancel, max_bytes=max_bytes)
    return out


def window_matches(src: bytes, out: bytes, probe_len: int = 4096) -> Optional[bool]:
    """True if ``out`` appears unchanged inside ``src``; None when it cannot be aligned."""
    if len(out) < probe_len or len(src) < probe_len:
        return None
    offset = src.find(out[:probe_len])
    if offset < 0:
        return False
    usable = min(len(out), len(src) - offset)
    if usable < len(out) * 0.5:
        return None
    return src[offset:offset + usable] == out[:usable]


def frame_hashes(ffmpeg: str, path: str, index: int, start: Optional[float], frames: int, cancel=None) -> List[str]:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-noautorotate"]
    if start:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", path, "-map", f"0:{index}", "-frames:v", str(frames), "-f", "framemd5", "-"]
    code, out, err = run_capture(cmd, timeout=600, cancel=cancel, text=True)
    if code != 0:
        raise RemuxError(err.strip().splitlines()[-1] if err.strip() else f"ffmpeg exit {code}")
    return [line.rsplit(",", 1)[-1].strip() for line in out.splitlines() if line and not line.startswith("#")]


def frames_match(src: List[str], out: List[str]) -> Optional[bool]:
    if not out or not src:
        return None
    try:
        offset = src.index(out[0])
    except ValueError:
        return None
    usable = min(len(out), len(src) - offset)
    if usable < min(len(out), 3):
        return None
    return src[offset:offset + usable] == out[:usable]


class _StreamReader:
    """Reads one ffmpeg raw-stream pipe on a thread (so both pipes drain together)."""

    def __init__(self, cmd: Sequence[str]):
        self.proc = subprocess.Popen(list(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, creationflags=subprocess_flags())

    def read(self, n: int) -> bytes:
        return self.proc.stdout.read(n)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


def full_stream_compare(ffmpeg: str, src_path: str, src_index: int, out_path: str, out_index: int,
                        muxer: str, cancel=None, on_bytes: Callable[[int], None] = lambda n: None,
                        chunk: int = 1 << 20) -> Tuple[Optional[bool], str]:
    """Compare a whole elementary stream of both files byte for byte.

    The M2TS stream may start a little later (tsMuxeR aligns audio to the
    first video frame), so its first 64 KiB are located in the source first.
    Returns (identical?, explanation).
    """
    def cmd(path, index):
        return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", path, "-map", f"0:{index}",
                "-c", "copy", "-f", muxer, "-"]
    src = _StreamReader(cmd(src_path, src_index))
    out = _StreamReader(cmd(out_path, out_index))
    try:
        head = out.read(64 * 1024)
        if len(head) < 1024:
            return None, "the M2TS stream is too short to compare"
        buf = b""
        offset = -1
        while len(buf) < 64 * 1024 * 1024:
            block = src.read(chunk)
            if not block:
                break
            buf += block
            offset = buf.find(head)
            if offset >= 0:
                break
        if offset < 0:
            return False, "the start of the M2TS stream was not found in the source stream"
        skipped = offset
        pending_src = buf[offset + len(head):]
        compared = len(head)
        pending_out = b""
        while True:
            if cancel is not None and cancel.is_set():
                raise ConversionCancelled("Verification cancelled")
            o = out.read(chunk)
            pending_out += o
            while len(pending_src) < len(pending_out):
                s = src.read(max(chunk, len(pending_out) - len(pending_src)))
                if not s:
                    break
                pending_src += s
            n = min(len(pending_out), len(pending_src))
            if pending_out[:n] != pending_src[:n]:
                return False, f"streams differ after {compared + n:,} bytes"
            compared += n
            on_bytes(compared)
            pending_out, pending_src = pending_out[n:], pending_src[n:]
            if not o:
                if pending_out:
                    return False, "the M2TS stream is longer than the source stream"
                break
        rest = len(pending_src)
        while True:
            s = src.read(chunk)
            if not s:
                break
            rest += len(s)
        total = skipped + compared + rest
        detail = f"{compared:,} bytes identical"
        if skipped or rest:
            missing = (skipped + rest) / max(1, total)
            detail += f" ({skipped:,} bytes before and {rest:,} bytes after not in the M2TS)"
            if missing > 0.005:
                return False, detail + " - more than 0.5% of the stream is missing"
        return True, detail
    finally:
        src.close()
        out.close()


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

OK, WARN, FAIL, UNVERIFIED = "ok", "warning", "FAILED", "unverified"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


@dataclass
class Verification:
    checks: List[Check] = field(default_factory=list)
    audio_identical: Dict[int, Optional[bool]] = field(default_factory=dict)   # source index -> result
    atmos: Dict[int, str] = field(default_factory=dict)                         # source index -> status
    atmos_expected: Dict[int, bool] = field(default_factory=dict)
    output_hdr: Optional[HdrSnapshot] = None
    hdr_status: str = OUTCOME_HDR_CHANGED
    hdr_lines: List[str] = field(default_factory=list)
    output_tracks: List[str] = field(default_factory=list)

    def add(self, name, status, detail=""):
        self.checks.append(Check(name, status, detail))

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def audio_verified(self) -> bool:
        audio = [c for c in self.checks if c.name.startswith("Audio")]
        return bool(audio) and all(c.status == OK for c in audio) and \
            all(v is True for v in self.audio_identical.values())

    def outcomes(self) -> List[str]:
        result = [OUTCOME_REMUXED]
        result.append(OUTCOME_AUDIO_VERIFIED if self.audio_verified else OUTCOME_AUDIO_PRESENT)
        if self.atmos_expected:
            if all(self.atmos.get(i) in (STATUS_TRUEHD_ATMOS, STATUS_DDP_ATMOS) for i in self.atmos_expected):
                result.append(OUTCOME_ATMOS)
            else:
                result.append(OUTCOME_ATMOS_MISSING)
        result.append(self.hdr_status)
        result.append(OUTCOME_PANASONIC)
        return result


def _output_groups(out_probe: Dict):
    """Output streams by type, with TrueHD's AC-3 core sub-stream (same PID) set apart."""
    streams = out_probe.get("streams", [])
    truehd_ids = {s.get("id") for s in streams if s.get("codec_name") == "truehd"}
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio, cores = [], {}
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        if s.get("codec_name") == "ac3" and s.get("id") in truehd_ids:
            cores[s.get("id")] = s
        else:
            audio.append(s)
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    return video, audio, subs, cores


def _expected_codec(codec: str):
    if _is_pcm(codec):
        return ("pcm_bluray",)
    value = OUTPUT_CODEC.get(codec, codec)
    return value if isinstance(value, tuple) else (value,)


def verify_output(plan: M2TSPlan, out_path: str, ffmpeg: str, ffprobe: str, full_audio: bool = True,
                  log: Callable[[str], None] = print,
                  progress: Callable[[str, Optional[float], str], None] = lambda p, f, d: None,
                  cancel: Optional[threading.Event] = None) -> Verification:
    v = Verification()
    try:
        out = run_ffprobe(ffprobe, out_path, timeout=300)
    except RuntimeError as e:
        v.add("Output readable", FAIL, str(e))
        return v
    fmt = (out.get("format") or {}).get("format_name") or ""
    v.add("Output readable", OK if "mpegts" in fmt else FAIL, f"container: {fmt or 'unknown'}")
    out_video, out_audio, out_subs, cores = _output_groups(out)
    src_streams = {int(s["index"]): s for s in plan.probe.get("streams", [])}
    duration = format_duration(plan.probe)

    # ---- video
    sel_video = plan.selected("video")
    if len(out_video) != len(sel_video):
        v.add("Video tracks", FAIL, f"{len(out_video)} in the M2TS, {len(sel_video)} expected")
    for t, o in zip(sel_video, out_video):
        s = src_streams[t.index]
        diffs = []
        for key in ("codec_name", "profile", "width", "height", "pix_fmt", "color_transfer", "color_primaries",
                    "color_space"):
            if s.get(key) != o.get(key) and s.get(key) is not None:
                diffs.append(f"{key} {s.get(key)} -> {o.get(key)}")
        v.output_tracks.append(f"Video {o.get('codec_name')} {o.get('width')}x{o.get('height')} "
                               f"{o.get('pix_fmt') or ''} {o.get('color_transfer') or ''}".strip())
        if diffs:
            v.add("Video format", FAIL, "; ".join(diffs))
        else:
            v.add("Video format", OK, f"{o.get('codec_name')} {o.get('profile') or ''} {o.get('width')}x"
                                      f"{o.get('height')} {o.get('pix_fmt')} - unchanged")
        progress("Verifying video frames", None, "")
        _check_frames(v, ffmpeg, plan.source, t.index, out_path, int(o["index"]), duration, cancel)
        snap = hdr_snapshot(ffprobe, out_path, o, format_duration(out), cancel)
        v.output_hdr = snap
        _compare_hdr(v, plan.source_hdr, snap)
        break                       # HDR/frames checked on the main video track

    # ---- audio
    sel_audio = plan.ordered_audio()
    if len(out_audio) != len(sel_audio):
        v.add("Audio tracks", FAIL, f"{len(out_audio)} in the M2TS, {len(sel_audio)} expected")
    for t, o in zip(sel_audio, out_audio):
        name = f"Audio #{t.index}"
        problems = []
        if o.get("codec_name") not in _expected_codec(t.codec):
            problems.append(f"codec {o.get('codec_name')}, expected {t.codec}")
        if t.channels and o.get("channels") != t.channels:
            problems.append(f"{o.get('channels')} channels, expected {t.channels}")
        out_lang, src_lang = normalize_language(tag(o, "language")), normalize_language(t.language)
        if src_lang and out_lang and out_lang != src_lang:
            problems.append(f"language {out_lang}, expected {src_lang}")
        core = cores.get(o.get("id"))
        desc = f"{o.get('codec_name')} {o.get('channels')} ch {o.get('sample_rate')} Hz"
        if t.codec == "truehd":
            desc += ("; embedded AC-3 core present" if core and core.get("channels") else
                     "; no embedded AC-3 core")
        v.output_tracks.append(f"Audio {desc} [{tag(o, 'language') or '-'}] (from #{t.index})")
        v.add(name, FAIL if problems else OK, "; ".join(problems) or desc)
        if problems:
            continue
        _compare_audio(v, plan, t, o, ffmpeg, out_path, duration, full_audio, log, progress, cancel)
        _check_atmos(v, plan, t, o, ffmpeg, out_path, format_duration(out), cancel)

    # ---- subtitles
    sel_subs = plan.selected("subtitle")
    if len(out_subs) != len(sel_subs):
        v.add("Subtitle tracks", FAIL, f"{len(out_subs)} in the M2TS, {len(sel_subs)} expected")
    else:
        bad = [f"#{t.index}" for t, o in zip(sel_subs, out_subs)
               if o.get("codec_name") != "hdmv_pgs_subtitle"
               or (normalize_language(t.language) and normalize_language(tag(o, "language"))
                   and normalize_language(t.language) != normalize_language(tag(o, "language")))]
        v.add("Subtitle tracks", FAIL if bad else OK,
              f"different codec or language: {', '.join(bad)}" if bad else
              f"{len(out_subs)} PGS track(s) present" if out_subs else "none selected")
        for t, o in zip(sel_subs, out_subs):
            v.output_tracks.append(f"Subtitle PGS [{tag(o, 'language') or '-'}] (from #{t.index}"
                                   + (", rendered from SRT" if t.codec == "subrip" else "") + ")")

    # ---- duration
    d_out = format_duration(out)
    if duration and d_out:
        diff = abs(duration - d_out)
        v.add("Duration", OK if diff <= max(2.0, duration * 0.002) else WARN,
              f"{format_seconds(d_out)} (source {format_seconds(duration)})")
    return v


def _check_frames(v: Verification, ffmpeg, src_path, src_index, out_path, out_index, duration, cancel):
    results = []
    positions = [("start", None, None)]
    if duration and duration > 60:
        mid = duration / 2
        positions.append(("middle", mid - 1.5, mid))
    try:
        for label, src_start, out_start in positions:
            src = frame_hashes(ffmpeg, src_path, src_index, src_start, 120 if src_start else 48, cancel)
            out = frame_hashes(ffmpeg, out_path, out_index, out_start, 12, cancel)
            results.append((label, frames_match(src, out), len(out)))
    except (RemuxError, subprocess.TimeoutExpired, OSError) as e:
        v.add("Video frames", UNVERIFIED, f"could not decode frames: {e}")
        return
    if any(r is False for _, r, _ in results):
        v.add("Video frames", FAIL, "decoded frames differ from the source: " +
              ", ".join(label for label, r, _ in results if r is False))
    elif all(r is True for _, r, _ in results):
        v.add("Video frames", OK, "decoded frames identical to the source at the "
              + " and ".join(label for label, _, _ in results) + " (video copied, not re-encoded)")
    else:
        v.add("Video frames", UNVERIFIED, "frames identical where they could be lined up; "
              + ", ".join(f"{label} could not be aligned" for label, r, _ in results if r is None))


def _compare_hdr(v: Verification, src: Optional[HdrSnapshot], out: HdrSnapshot) -> None:
    lines = []
    if src is None or not src.checked:
        v.hdr_status = OUTCOME_HDR_CHANGED if out.is_hdr else OUTCOME_SDR
        v.hdr_lines = ["Source HDR metadata could not be checked; output: "
                       + (", ".join(FRAME_LABELS[k] for k in sorted(out.frame_keys)) or "none")]
        v.add("HDR metadata", UNVERIFIED, v.hdr_lines[0])
        return
    if not src.is_hdr and not out.is_hdr:
        v.hdr_status = OUTCOME_SDR
        v.hdr_lines = ["SDR video (no HDR or Dolby Vision metadata in the source)"]
        v.add("HDR metadata", OK, v.hdr_lines[0])
        return
    changed = False
    known = lambda t: t if t and t != "unknown" else None     # noqa: E731
    lines.append(f"Transfer: {known(src.transfer) or 'not declared'} -> {known(out.transfer) or 'not declared'}")
    if known(src.transfer) and known(src.transfer) != known(out.transfer):
        changed = True
    lines.append(f"Dolby Vision configuration: {src.dovi_label()} -> {out.dovi_label()}")
    if (src.dovi is None) != (out.dovi is None) or (src.dovi and out.dovi and any(
            src.dovi.get(k) != out.dovi.get(k) for k in ("profile", "compat", "rpu", "el", "bl"))):
        changed = True
    if src.dovi and out.dovi and src.dovi.get("level") != out.dovi.get("level"):
        lines.append("  (Dolby Vision level differs - tsMuxeR computes it from resolution and frame rate)")
    for key in ("rpu", "hdr10plus", "mastering", "cll"):
        before = key in src.frame_keys or (key in src.container and key not in ("rpu", "hdr10plus"))
        after = key in out.frame_keys
        if before or after:
            where = "video stream" if key in src.frame_keys else "MKV header only"
            lines.append(f"{FRAME_LABELS[key]}: {'present (' + where + ')' if before else 'absent'} -> "
                         f"{'present' if after else 'ABSENT'}")
            if before != after:
                changed = True
    unchecked = [label for label in src.frames if label not in out.frames]
    if src.errors or out.errors or unchecked:
        lines.append("Some frame positions could not be checked: " + "; ".join(src.errors + out.errors + unchecked))
        changed = True
    v.hdr_lines = lines
    v.hdr_status = OUTCOME_HDR_CHANGED if changed else OUTCOME_HDR_SAME
    v.add("HDR metadata", WARN if changed else OK, "; ".join(lines))


def _compare_audio(v, plan, t, o, ffmpeg, out_path, duration, full_audio, log, progress, cancel):
    muxer = RAW_MUXER.get(t.codec)
    name = f"Audio #{t.index} bitstream"
    if muxer is None:
        v.audio_identical[t.index] = None
        v.add(name, UNVERIFIED, "not compared byte for byte (repackaged for M2TS); presence and channels checked")
        return
    out_index = int(o["index"])
    try:
        if full_audio and t.codec in FULL_COMPARE_CODECS:
            progress(f"Comparing audio #{t.index} (whole stream)", None, "reads both files completely")
            last = [0.0]

            def on_bytes(n):
                now = time.monotonic()
                if now - last[0] > 1.0:
                    last[0] = now
                    progress(f"Comparing audio #{t.index} (whole stream)", None, f"{n / 1e6:,.0f} MB identical")
            same, detail = full_stream_compare(ffmpeg, plan.source, t.index, out_path, out_index, muxer,
                                               cancel=cancel, on_bytes=on_bytes)
            scope = "whole stream"
        else:
            results = []
            positions = [(None, None)]
            if duration and duration > 60:
                positions.append((duration / 2 - 3, duration / 2))
            for src_start, out_start in positions:
                src = raw_sample(ffmpeg, plan.source, t.index, muxer, src_start, SAMPLE_SECONDS + 6, cancel)
                out = raw_sample(ffmpeg, out_path, out_index, muxer, out_start, SAMPLE_SECONDS, cancel)
                results.append(window_matches(src, out))
            same = False if False in results else (True if all(r is True for r in results) else None)
            scope = f"{len(results)} sample window(s) of {SAMPLE_SECONDS:g} s"
            detail = {True: "bytes identical", False: "bytes differ", None: "could not be aligned"}[same]
    except ConversionCancelled:
        raise
    except Exception as e:     # noqa: BLE001 - reported, never fatal by itself
        same, detail, scope = None, f"comparison failed: {e}", "-"
    v.audio_identical[t.index] = same
    status = {True: OK, False: FAIL, None: UNVERIFIED}[same]
    v.add(name, status, f"{scope}: {detail}")


def _check_atmos(v, plan, t, o, ffmpeg, out_path, out_duration, cancel):
    if t.codec not in ("truehd", "eac3"):
        return
    # Atmos is expected when the MKV's own bitstream analysis found it; tsMuxeR's "+ ATMOS" label
    # only counts when that analysis could not decide (it never overrides a checked bitstream).
    expected = t.atmos_flag is True or bool(t.atmos_flag is None and t.codec == "truehd" and t.ts and t.ts.atmos)
    starts = [None] + ([out_duration / 2] if out_duration and out_duration > 120 else [])
    try:
        data = b"".join(raw_sample(ffmpeg, out_path, int(o["index"]), t.codec, s, 4.0, cancel,
                                   max_bytes=8 * 1024 * 1024) for s in starts)
    except Exception as e:     # noqa: BLE001
        data = b""
        v.add(f"Audio #{t.index} Atmos", UNVERIFIED, f"could not read the M2TS track: {e}")
    if t.codec == "truehd":
        an = analyse_truehd(o.get("profile"), None, summarize_truehd_bitstream(data) if data else None)
    else:
        an = analyse_eac3(o.get("profile"), None, summarize_ac3_bitstream(data) if data else None)
    v.atmos[t.index] = an.status
    if expected:
        v.atmos_expected[t.index] = True
        ok = an.status in (STATUS_TRUEHD_ATMOS, STATUS_DDP_ATMOS)
        v.add(f"Audio #{t.index} Atmos", OK if ok else FAIL,
              f"{an.status} in the M2TS" + (f" ({an.evidence[0]})" if an.evidence else ""))
    elif data:
        v.add(f"Audio #{t.index} Atmos", OK, f"{an.status} in the M2TS (source: {t.atmos or 'not Atmos'})")


# --------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------

@dataclass
class RemuxOptions:
    overwrite: bool = False               # replace an existing output (the UI asks first)
    accept_metadata_risk: bool = False    # the user confirmed the Dolby Vision / HDR10+ warning
    full_audio_compare: bool = True       # compare whole TrueHD / DTS-HD streams


@dataclass
class RemuxResult:
    plan: M2TSPlan
    output: str
    verification: Verification
    elapsed: float
    mux_lines: List[str]
    source_unchanged: bool
    report: str = ""

    @property
    def outcomes(self) -> List[str]:
        return self.verification.outcomes()


ProgressFn = Callable[[str, Optional[float], str], None]


class M2TSRemuxer:
    def __init__(self, tsmuxer: TsMuxerInfo, ffmpeg: str, ffprobe: str, tools: Optional[ToolSet] = None,
                 log: Callable[[str], None] = print, progress: ProgressFn = lambda p, f, d: None,
                 handle: Optional[ProcessHandle] = None):
        self.tsmuxer = tsmuxer
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.tools = tools or ToolSet(ffmpeg=ffmpeg, ffprobe=ffprobe)
        self.log = log
        self.progress = progress
        self.handle = handle or ProcessHandle()

    # ---- analysis -------------------------------------------------------

    def analyse(self, source: str, output: Optional[str] = None, config: Optional[ScannerConfig] = None,
                subtitle_mode: str = "preferred") -> M2TSPlan:
        if not self.tsmuxer or not self.tsmuxer.ok:
            raise TsMuxerError(self.tsmuxer.message if self.tsmuxer else "tsMuxeR is not available")
        if not self.ffprobe:
            raise RemuxError("ffprobe is required to analyse and verify the remux")
        source = os.path.abspath(source)
        self.progress("Analysing the MKV", None, Path(source).name)
        report = inspect_file(source, self.tools, InspectOptions(video_frames=True), cancel=self.handle.cancelled)
        probe = run_ffprobe(self.ffprobe, source, timeout=300)
        self._cancelled()
        self.progress("Reading tracks with tsMuxeR", None, "")
        detection = detect_tracks(self.tsmuxer.path, source)
        self._cancelled()
        hdr = None
        video = next((s for s in streams_of_type(probe, "video")
                      if not (s.get("disposition") or {}).get("attached_pic")), None)
        if video is not None:
            self.progress("Reading HDR metadata from sample frames", None, "")
            hdr = hdr_snapshot(self.ffprobe, source, video, format_duration(probe), cancel=self.handle.cancelled)
        self._cancelled()
        return build_plan(source, report, probe, detection, hdr, output, config, subtitle_mode)

    def _cancelled(self):
        if self.handle.cancelled.is_set():
            raise ConversionCancelled("Cancelled")

    # ---- remux ----------------------------------------------------------

    def remux(self, plan: M2TSPlan, options: RemuxOptions) -> RemuxResult:
        problems = plan.selection_problems()
        if problems:
            raise RemuxError("Cannot remux:\n  " + "\n  ".join(problems))
        if plan.metadata_warnings and not options.accept_metadata_risk:
            raise RemuxError("This film has Dolby Vision / HDR10+ metadata that M2TS remuxing may change; "
                             "the remux needs your explicit confirmation as a compatibility test.")
        source, output = plan.source, os.path.abspath(plan.output)
        if os.path.normcase(source) == os.path.normcase(output):
            raise RemuxError("The output must be a different file from the source MKV.")
        if not output.lower().endswith(".m2ts"):
            raise RemuxError("The output file name must end in .m2ts")
        try:
            st = os.stat(source)
        except OSError as e:
            raise RemuxError(f"The source MKV is not accessible: {e}")
        if (st.st_size, st.st_mtime_ns) != (plan.source_size, plan.source_mtime_ns):
            raise RemuxError("The MKV changed after it was analysed; analyse it again.")
        if os.path.exists(output) and not options.overwrite:
            raise OutputExists(f"{Path(output).name} already exists.")
        out_dir = os.path.dirname(output)
        if not os.path.isdir(out_dir):
            raise RemuxError(f"The output folder does not exist: {out_dir}")
        needed = plan.estimated_bytes() + DISK_MARGIN_BYTES
        free = shutil.disk_usage(out_dir).free
        if free < needed:
            raise RemuxError(f"Not enough free disk space in {out_dir}: about {format_size(needed)} needed, "
                             f"{format_size(free)} available.")

        meta_text = plan.meta_text()
        fd, temp = tempfile.mkstemp(prefix=f".{Path(output).stem}{PARTIAL_INFIX}", suffix=".m2ts", dir=out_dir)
        os.close(fd)
        meta_path = write_meta(meta_text)
        started = time.monotonic()
        try:
            self.log("tsMuxeR meta file:")
            for line in meta_text.splitlines():
                self.log(f"  {line}")
            self.log(f"Writing a temporary M2TS beside the output: {Path(temp).name}")

            def on_progress(p: MuxProgress):
                detail = f"{format_seconds(p.elapsed)} elapsed"
                if p.eta is not None:
                    detail += f" • ~{format_seconds(p.eta)} remaining"
                self.progress("Remuxing with tsMuxeR", p.fraction, detail)

            result = run_tsmuxer(self.tsmuxer.path, meta_path, temp, on_progress, self.handle)
            if result.returncode != 0:
                raise RemuxError(f"tsMuxeR failed (exit code {result.returncode}):\n{result.error_summary()}")
            if not result.success_message:
                self.log("Note: tsMuxeR exited normally but did not print its success message; "
                         "the output is verified anyway.")
            if not os.path.isfile(temp) or os.path.getsize(temp) == 0:
                raise RemuxError("tsMuxeR produced no output (empty file).")
            elapsed = time.monotonic() - started
            self.log(f"tsMuxeR finished: {format_size(os.path.getsize(temp))} in {format_seconds(elapsed)}")

            self.progress("Verifying the M2TS", None, "")
            verification = verify_output(plan, temp, self.ffmpeg, self.ffprobe, options.full_audio_compare,
                                         self.log, self.progress, self.handle.cancelled)
            self._cancelled()
            for c in verification.checks:
                self.log(f"  {'✓' if c.status == OK else '⚠' if c.status in (WARN, UNVERIFIED) else '✗'} "
                         f"{c.name}: {c.detail}")
            st2 = os.stat(source)
            unchanged = (st2.st_size, st2.st_mtime_ns) == (plan.source_size, plan.source_mtime_ns)
            res = RemuxResult(plan, output, verification, elapsed, result.lines, unchanged)
            if verification.failures:
                res.output = ""
                res.report = format_report(res)
                raise VerificationFailed(
                    "The M2TS failed verification and was deleted:\n  "
                    + "\n  ".join(f"{c.name}: {c.detail}" for c in verification.failures), res.report)
            try:
                shutil.copymode(source, temp)       # readable by e.g. a Plex service account
            except OSError:
                pass
            if os.path.exists(output) and not options.overwrite:
                raise OutputExists(f"{Path(output).name} appeared while remuxing; nothing was replaced.")
            os.replace(temp, output)
            res.report = format_report(res)
            return res
        finally:
            if os.path.exists(temp):
                try:
                    os.unlink(temp)
                except OSError:
                    pass
            shutil.rmtree(os.path.dirname(meta_path), ignore_errors=True)


def unique_output(path: str) -> str:
    """``path`` or 'name (2).m2ts', 'name (3).m2ts', ... whichever does not exist."""
    if not os.path.exists(path):
        return path
    p = Path(path)
    n = 2
    while True:
        candidate = p.with_name(f"{p.stem} ({n}){p.suffix}")
        if not os.path.exists(candidate):
            return str(candidate)
        n += 1


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def describe_plan(plan: M2TSPlan) -> List[str]:
    lines = [f"Source: {plan.source}", f"Output: {plan.output}",
             f"Estimated output size: about {format_size(plan.estimated_bytes())}", ""]
    hdr = plan.source_hdr
    for t in plan.video:
        lines.append(f"Video {t.label} - {t.handling}" + ("" if t.selected else " [not included]"))
    if hdr:
        lines.append(f"  Dolby Vision configuration: {hdr.dovi_label()}")
        for label, keys in hdr.frames.items():
            lines.append(f"  Frame metadata ({label}): " + (", ".join(FRAME_LABELS[k] for k in keys) or "none"))
        if hdr.container:
            lines.append("  MKV header: " + ", ".join(FRAME_LABELS[k] for k in hdr.container))
    lines.append("Audio (in M2TS order):")
    for t in plan.ordered_audio():
        lines.append(f"  keep  {t.label} - {t.handling}" + (f" - {t.atmos}" if t.atmos else ""))
    for t in plan.audio:
        if not t.selected:
            lines.append(f"  OMIT  {t.label} - {t.handling if not t.compatible else 'not selected'}")
    lines.append("Subtitles:")
    for t in plan.subtitles:
        lines.append(f"  {'keep' if t.selected else 'OMIT'}  {t.label} - "
                     f"{t.handling if (t.selected or not t.compatible) else 'not selected'}")
    if not plan.subtitles:
        lines.append("  none in the source")
    for title, items in (("Blocking problems", plan.blockers), ("Video metadata warnings", plan.metadata_warnings),
                         ("Warnings", plan.warnings), ("Notes", plan.info)):
        if items:
            lines += ["", f"{title}:"] + [f"  • {i}" for i in items]
    return lines


def format_report(result: RemuxResult) -> str:
    plan, v = result.plan, result.verification
    lines = ["DTS2DD - MKV to M2TS remux report (experimental Panasonic compatibility test)", ""]
    lines.append("Outcome:")
    lines += [f"  • {o}" for o in result.outcomes]
    if v.failures:
        lines.append("  • VERIFICATION FAILED - the M2TS was deleted")
    lines += ["", f"Source: {plan.source}", f"Source container: Matroska (MKV)",
              f"Output: {result.output or '(deleted)'}", "Output container: MPEG-2 transport stream (M2TS)",
              f"Source MKV unchanged: {'yes (same size and modification time)' if result.source_unchanged else 'NO - it changed during the remux'}",
              f"Remux time: {format_seconds(result.elapsed)}", ""]
    lines.append("Tracks in the M2TS:")
    lines += [f"  {t}" for t in v.output_tracks] or ["  (none read)"]
    omitted = plan.excluded()
    lines.append("Tracks omitted:")
    lines += [f"  {t.kind} {t.label} - {reason}" for t, reason in omitted] or ["  none"]
    lines += ["", "HDR / Dolby Vision (before -> after):"]
    lines += [f"  {l}" for l in v.hdr_lines] or ["  not checked"]
    if v.atmos:
        lines += ["", "Atmos (parsed from the M2TS bitstream):"]
        for index, status in v.atmos.items():
            src = next((t for t in plan.audio if t.index == index), None)
            lines.append(f"  audio #{index}: source '{src.atmos if src else '?'}' -> M2TS '{status}'")
    lines += ["", "Checks:"]
    lines += [f"  [{c.status}] {c.name}: {c.detail}" for c in v.checks]
    notes = plan.metadata_warnings + plan.warnings + plan.info
    if notes:
        lines += ["", "Warnings and notes:"] + [f"  • {n}" for n in notes]
    lines += ["", "Metadata checks do not prove that the Panasonic will play this file. Test it over Plex DLNA "
                  "and check the Denon shows Dolby TrueHD / Dolby Atmos."]
    return "\n".join(lines)
