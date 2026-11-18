"""DTS-HD MA / FLAC -> Dolby TrueHD conversion workflow with verification.

Pipeline: DTS-HD MA (lossless XLL) or FLAC -> integer PCM -> TrueHD -> Matroska,
in a single LibreMPEG process with every other stream copied. The source
file is never written to. The result is first written to a hidden temporary
file in the source folder, verified, and only then renamed to the final
name (or, after explicit user confirmation, swapped in for the source).
"""
import glob
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from audio_stream_info import AudioStreamInfo
from ffmpeg_helper import ConversionCancelled, FFmpegProgress, ProcessHandle, run_ffmpeg
from media_probe import (
    audio_streams_from_probe, classify_audio, describe_video, format_duration,
    format_seconds, is_lossless_dts_hd_ma, keyframe_at_or_before, run_ffprobe,
    stream_packet_hashes, streams_of_type, subprocess_flags, tag, video_signature,
)
from truehd_encoder import (
    LAYOUT_LABELS, LOSSLESS_SAMPLE_FORMATS, SAMPLE_RATES_BY_CHANNELS, SUPPORTED_LAYOUTS,
    EncoderReport, TrueHDError, TrueHDJob, build_clip_cut_command, build_output_decode_command,
    may_exceed_peak_rate,
    build_output_hash_command, build_source_decode_command, build_truehd_command,
    check_access_unit_sizes, check_channel_assignment, read_hash_file, read_major_sync,
)

PARTIAL_PREFIX = ".{stem}.truehd-partial-"
OUTPUT_SUFFIX = "_Sound_TrueHD"
# Copied tracks are compared packet-by-packet over this much of the film.
PACKET_CHECK_SECONDS = 300.0


# --------------------------------------------------------------------------
# Source validation
# --------------------------------------------------------------------------

# FLAC decoder layout -> TrueHD layout. FLAC's default 6-channel order is
# FL FR FC LFE + a surround pair FFmpeg names "back" ("5.1"); in TrueHD that
# pair is the Ls/Rs surround pair, so it is relabelled by position (no
# mixing, samples unchanged). Everything else must already match.
FLAC_LAYOUTS = {
    "stereo": "stereo",
    "5.1(side)": "5.1(side)",
    "5.1": "5.1(side)",
    "7.1": "7.1",
}
# Integer sample formats of the FLAC decoder -> planar format for the encoder.
FLAC_SAMPLE_FORMATS = {"s16": "s16p", "s16p": "s16p", "s32": "s32p", "s32p": "s32p"}


def is_flac(stream: AudioStreamInfo) -> bool:
    return (stream.codec_name or "").lower() == "flac"


def truehd_target_layout(stream: AudioStreamInfo) -> Optional[str]:
    """Layout the TrueHD track will have (None when not convertible)."""
    if is_flac(stream):
        return FLAC_LAYOUTS.get(stream.channel_layout or "")
    return stream.channel_layout if stream.channel_layout in SUPPORTED_LAYOUTS else None


def encoder_sample_format(stream: AudioStreamInfo) -> Optional[str]:
    if is_flac(stream):
        return FLAC_SAMPLE_FORMATS.get(stream.sample_fmt or "")
    return stream.sample_fmt


def flac_source_problems(layout: Optional[str], channels: Optional[int], sample_rate: Optional[int],
                         bit_depth: Optional[int], sample_fmt: Optional[str]) -> List[str]:
    """Source-side reasons a FLAC track cannot be converted losslessly (no encoder check)."""
    problems = []
    target = FLAC_LAYOUTS.get(layout or "")
    if target is None:
        problems.append(
            f"FLAC channel layout '{layout or 'unknown'}' ({channels or '?'} channels) is not supported. "
            "Supported: 2.0, 5.1 and 7.1. No channel conversion or downmix is done automatically.")
        return problems
    if sample_fmt not in FLAC_SAMPLE_FORMATS:
        problems.append(f"The FLAC decoder reports sample format '{sample_fmt}', which is not integer PCM.")
    if bit_depth is not None and bit_depth > 24:
        problems.append(f"{bit_depth}-bit FLAC cannot be stored losslessly in TrueHD (24-bit maximum).")
    rates = SAMPLE_RATES_BY_CHANNELS.get(channels or 0, ())
    if sample_rate not in rates:
        problems.append(f"{sample_rate} Hz with {channels} channels is not a valid Dolby TrueHD "
                        "configuration; resampling would not be lossless.")
    return problems


def validate_flac_source(stream: AudioStreamInfo, report: Optional[EncoderReport]) -> List[str]:
    problems = flac_source_problems(stream.channel_layout, stream.channel_count, stream.sample_rate,
                                    stream.bit_depth, stream.sample_fmt)
    target = truehd_target_layout(stream)
    if target and (report is None or not report.can_encode(target)):
        problems.append(
            f"The configured TrueHD encoder has not passed the {LAYOUT_LABELS[target]} self-test."
            + (f" ({report.status}: {report.message})" if report else ""))
    return problems


def validate_source(stream: AudioStreamInfo, report: Optional[EncoderReport]) -> List[str]:
    """Return blocking problems for converting ``stream`` (empty when OK)."""
    problems = []
    if stream.format_name is None:
        return ["Detailed stream information is unavailable (ffprobe not found), "
                "so the channel layout cannot be verified."]
    if is_flac(stream):
        return validate_flac_source(stream, report)

    probe_like = {"codec_name": stream.codec_name.lower(), "profile": stream.profile}
    if not is_lossless_dts_hd_ma(probe_like):
        problems.append(
            f"The selected track is {stream.format_name}. TrueHD conversion is only "
            "offered for DTS-HD Master Audio and FLAC, because other formats are lossy and "
            "a TrueHD copy would not be lossless relative to the original master."
        )
        return problems

    if stream.sample_fmt not in LOSSLESS_SAMPLE_FORMATS:
        problems.append(
            f"The DTS decoder reports {stream.sample_fmt} output, which means only "
            "the lossy DTS core would be decoded (the lossless XLL extension is not "
            "being used). Conversion is disabled."
        )

    layout = stream.channel_layout
    if layout not in SUPPORTED_LAYOUTS:
        hint = {
            "7.1(wide)": " This 7.1 variant uses front-centre speakers instead of side "
                         "surrounds; the TrueHD encoder cannot represent it.",
            "6.1": " LibreMPEG accepts 6.1 but its output was not bit-exact in testing.",
            "5.1": " This 5.1 variant uses rear (back) surrounds; the encoder signals it "
                   "without surround channels for 5.1 decoders.",
        }.get(layout or "", "")
        problems.append(
            f"Channel layout '{layout or 'unknown'}' is not supported. Supported: "
            "5.1 (side surrounds) and 7.1 (side + rear surrounds).{}".format(hint)
            + " No channel conversion will be performed automatically."
        )
    else:
        rates = SAMPLE_RATES_BY_CHANNELS.get(stream.channel_count or 0, ())
        if stream.sample_rate not in rates:
            problems.append(
                f"{stream.sample_rate} Hz with {stream.channel_count} channels is not a "
                "valid Dolby TrueHD configuration; resampling would not be lossless."
            )
        if report is None or not report.can_encode(layout):
            problems.append(
                f"The configured TrueHD encoder has not passed the {LAYOUT_LABELS[layout]} "
                "self-test." + (f" ({report.status}: {report.message})" if report else "")
            )
    return problems


def source_warnings(stream: AudioStreamInfo) -> List[str]:
    warnings = []
    if is_flac(stream):
        warnings.append("Converting FLAC to TrueHD does not create Dolby Atmos; the result is "
                        "channel-based TrueHD with exactly the same samples.")
        if stream.channel_layout == "5.1":
            warnings.append("FLAC's 6-channel order names the surround pair 'back'. It is carried as "
                            "the 5.1 surround (Ls/Rs) pair in TrueHD; channel order and samples are "
                            "unchanged.")
    if stream.profile and "DTS:X" in stream.profile:
        warnings.append("DTS:X object metadata cannot be carried into TrueHD; the lossless "
                        "7.1/5.1 channel bed is converted. The result is NOT Dolby Atmos.")
    if (stream.channel_count and stream.sample_rate
            and may_exceed_peak_rate(stream.channel_count, stream.sample_rate, stream.bit_depth or 24)):
        warnings.append(
            f"{stream.channel_count}-channel {stream.sample_rate // 1000} kHz audio is close to "
            "TrueHD's 18 Mb/s maximum. Loud passages may not fit, in which case the conversion "
            "stops at the end with an error and no file is saved. Try a test clip of a loud "
            "scene first.")
    return warnings


# --------------------------------------------------------------------------
# Output naming / safety helpers
# --------------------------------------------------------------------------

def unique_path(path: Path) -> Path:
    """Return ``path`` or a numbered variant that does not exist yet."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise TrueHDError(f"Could not find a free file name near {path}")


def output_name(source: Path, clip_seconds: Optional[int]) -> Path:
    if clip_seconds:
        return source.with_name(f"{source.stem}_TrueHD_Test_{clip_seconds}s.mkv")
    return source.with_name(f"{source.stem}{OUTPUT_SUFFIX}.mkv")


def find_stale_partials(source_path: str) -> List[str]:
    """Temporary files left behind by an interrupted conversion."""
    source = Path(source_path)
    patterns = [
        str(source.with_name(PARTIAL_PREFIX.format(stem=glob.escape(source.stem)) + "*.mkv")),
        str(source.with_name(f".{glob.escape(source.stem)}.converting-*.mkv")),
    ]
    found = []
    for pattern in patterns:
        found.extend(glob.glob(pattern))
    return sorted(found)


def estimate_required_bytes(source_size: int, duration: Optional[float],
                            stream: AudioStreamInfo, clip_seconds: Optional[float]) -> int:
    """Conservative estimate of the space the temporary output needs."""
    channels = stream.channel_count or 8
    rate = stream.sample_rate or 48000
    bits = stream.bit_depth or 24
    pcm_rate = channels * rate * bits / 8
    if clip_seconds and duration:
        fraction = 2 * min(1.0, clip_seconds / duration)    # stream-copied cut + output
        seconds = clip_seconds
    else:
        fraction = 1.0
        seconds = duration or 0
    truehd_bytes = pcm_rate * 0.8 * seconds          # TrueHD is typically 50-70% of PCM
    return int(source_size * fraction + truehd_bytes + 200 * 1024 * 1024)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass
class TrueHDOptions:
    remove_source: bool = False
    make_default: bool = False
    strict_verify: bool = True
    test_clip: bool = False
    clip_seconds: int = 60
    clip_start: float = 0.0
    replace_original: bool = False   # only honoured after verification + confirmation
    remove_streams: Tuple[int, ...] = ()   # other audio/subtitle tracks to leave out


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class TrueHDResult:
    source_path: str
    temp_path: Optional[str]
    final_path: Optional[str]
    checks: List[Check] = field(default_factory=list)
    lossless: Optional[bool] = None
    lossless_detail: str = ""
    awaiting_replace_confirmation: bool = False
    clip: bool = False

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


ProgressFn = Callable[[str, Optional[float], str], None]   # (phase, fraction, detail)


# --------------------------------------------------------------------------
# Converter
# --------------------------------------------------------------------------

class TrueHDConverter:
    def __init__(self, encoder: str, ffprobe: str, reference_ffmpeg: Optional[str],
                 report: EncoderReport,
                 log: Callable[[str], None] = print,
                 progress: Optional[ProgressFn] = None,
                 handle: Optional[ProcessHandle] = None,
                 runner: Callable = run_ffmpeg):
        self.encoder = encoder
        self.ffprobe = ffprobe
        self.reference = reference_ffmpeg or encoder
        self.report = report
        self.log = log
        self.progress = progress or (lambda phase, fraction, detail: None)
        self.handle = handle or ProcessHandle()
        self.runner = runner

    # ---- main entry -----------------------------------------------------

    def convert(self, source_path: str, stream_index: int, options: TrueHDOptions) -> TrueHDResult:
        source_path = os.path.abspath(source_path)
        source = Path(source_path)
        self.progress("Analysing source", None, source.name)
        source_probe = run_ffprobe(self.ffprobe, source_path)
        streams = audio_streams_from_probe(source_probe)
        stream = next((s for s in streams if s.stream_index == stream_index), None)
        if stream is None:
            raise TrueHDError(f"Audio stream #{stream_index} not found in {source.name}")

        problems = validate_source(stream, self.report)
        if problems:
            raise TrueHDError("\n\n".join(problems))
        for warning in source_warnings(stream):
            self.log(f"WARNING: {warning}")

        duration = format_duration(source_probe)
        clip_seconds = options.clip_seconds if options.test_clip else None
        clip_start = None
        if clip_seconds:
            if duration and options.clip_start >= duration - 1:
                raise TrueHDError(f"Test clip start {format_seconds(options.clip_start)} is past "
                                  f"the end of the film ({format_seconds(duration)}).")
            clip_start = options.clip_start
            if streams_of_type(source_probe, "video") and clip_start > 0:
                keyframe = keyframe_at_or_before(self.ffprobe, source_path, clip_start)
                if keyframe is not None:
                    if abs(keyframe - clip_start) > 0.001:
                        self.log(f"Test clip start moved to video keyframe at "
                                 f"{keyframe:.3f}s so audio and video start together")
                    clip_start = keyframe

        self._check_disk_space(source, source_probe, stream, clip_seconds)

        removable = {st["index"]: st.get("codec_type") for st in source_probe.get("streams", [])
                     if st.get("codec_type") in ("audio", "subtitle")}
        unknown = [i for i in options.remove_streams if i not in removable]
        if unknown:
            raise TrueHDError(f"Tracks to remove not found in {source.name}: "
                              + ", ".join(f"#{i}" for i in unknown))
        remove_streams = tuple(sorted(set(options.remove_streams) - {stream.stream_index}))
        remove_source = options.remove_source or stream.stream_index in options.remove_streams

        final_path = unique_path(output_name(source, clip_seconds))
        temp_path = self._make_temp(source)
        cut_path = self._make_temp(source) if clip_seconds else None

        job = TrueHDJob(
            source_path=cut_path or source_path,
            output_path=temp_path,
            source_stream_index=stream.stream_index,
            audio_stream_count=len(streams),
            layout=truehd_target_layout(stream),
            sample_fmt=encoder_sample_format(stream),
            sample_rate=stream.sample_rate,
            language=stream.language,
            remove_source=remove_source,
            make_default=options.make_default,
            remove_streams=remove_streams,
            removed_other_audio=sum(1 for i in remove_streams if removable[i] == "audio"),
            source_layout=stream.channel_layout,
        )

        # Strict verification fingerprints (small files in the system temp folder)
        # Small working files (fingerprints, packet sizes) in the system temp folder
        work_dir = tempfile.mkdtemp(prefix="dts2dd-verify-")
        source_hash = os.path.join(work_dir, "source.sha256") if options.strict_verify else None
        output_hash = os.path.join(work_dir, "output.sha256")
        packet_sizes = os.path.join(work_dir, "truehd-packet-sizes.txt")

        result = TrueHDResult(source_path, temp_path, None, clip=bool(clip_seconds))
        keep_temp = False
        try:
            self._log_plan(source, stream, job, source_probe, options, clip_start, clip_seconds)
            reference_probe = source_probe
            if clip_seconds:
                # Step 1: cut the clip by stream copy (keeps all tracks in sync).
                self._run("Cutting test clip",
                          build_clip_cut_command(self.encoder, source_path, cut_path,
                                                 clip_start, float(clip_seconds)),
                          float(clip_seconds), "Could not cut the test clip")
                reference_probe = run_ffprobe(self.ffprobe, cut_path)
                duration = format_duration(reference_probe) or float(clip_seconds)
            # Step 2: append the TrueHD track (whole film, or the cut clip).
            self._encode(job, duration, source_hash, packet_sizes)
            self._verify_output(job, stream, reference_probe, result,
                                original_path=source_path, clip_start=clip_start,
                                packet_sizes_path=packet_sizes)
            if not result.passed:
                failed = [c for c in result.checks if not c.passed]
                raise TrueHDError("Output verification failed - the source was not changed:\n"
                                  + "\n".join(f"  {c.name}: {c.detail}" for c in failed))
            if options.strict_verify:
                self._strict_compare(job, stream, duration, result, source_hash, output_hash)
                if not result.lossless:
                    raise TrueHDError("Lossless verification failed - the source was not "
                                      f"changed:\n{result.lossless_detail}")

            if options.replace_original and not clip_seconds:
                # Leave the verified temp file for the GUI to confirm.
                result.awaiting_replace_confirmation = True
                result.final_path = str(final_path)
                keep_temp = True
            else:
                result.final_path = self.finalize_as_new(result, final_path)
            return result
        finally:
            if not keep_temp and os.path.exists(temp_path):
                os.unlink(temp_path)
            if cut_path and os.path.exists(cut_path):
                os.unlink(cut_path)
            shutil.rmtree(work_dir, ignore_errors=True)

    def _make_temp(self, source: Path) -> str:
        """Hidden temporary MKV beside the source (never the source itself)."""
        try:
            fd, path = tempfile.mkstemp(
                prefix=PARTIAL_PREFIX.format(stem=source.stem), suffix=".mkv", dir=str(source.parent))
            os.close(fd)
            # mkstemp creates owner-only files; match the source so Plex (which
            # may run as another user) can read the result.
            shutil.copymode(str(source), path)
            return path
        except OSError as e:
            raise TrueHDError(f"Cannot write to {source.parent}: {e}. Check folder permissions.")

    # ---- finalising -----------------------------------------------------

    @staticmethod
    def finalize_as_new(result: TrueHDResult, final_path: Optional[Path] = None) -> str:
        """Rename the verified temp file to a new MKV (never overwrites)."""
        target = unique_path(Path(final_path or result.final_path))
        if target.exists():
            raise TrueHDError(f"{target} already exists")
        os.rename(result.temp_path, target)
        result.final_path = str(target)
        result.awaiting_replace_confirmation = False
        return str(target)

    @staticmethod
    def finalize_replace(result: TrueHDResult) -> str:
        """Replace the source with the verified output (after user consent)."""
        if not result.passed or result.clip:
            raise TrueHDError("Refusing to replace the original: output not fully verified")
        if not result.temp_path or not os.path.exists(result.temp_path):
            raise TrueHDError("The verified temporary file is missing; the source was not changed")
        os.replace(result.temp_path, result.source_path)
        result.final_path = result.source_path
        result.awaiting_replace_confirmation = False
        return result.source_path

    @staticmethod
    def discard(result: TrueHDResult) -> None:
        if result.temp_path and os.path.exists(result.temp_path):
            os.unlink(result.temp_path)

    # ---- steps ----------------------------------------------------------

    def _check_disk_space(self, source: Path, probe: Dict, stream: AudioStreamInfo,
                          clip_seconds: Optional[int]) -> None:
        required = estimate_required_bytes(source.stat().st_size, format_duration(probe),
                                           stream, clip_seconds)
        free = shutil.disk_usage(str(source.parent)).free
        if free < required:
            raise TrueHDError(
                f"Not enough free disk space in {source.parent}.\n"
                f"Needed (estimate): {required / 1e9:.1f} GB, available: {free / 1e9:.1f} GB.")

    def _log_plan(self, source: Path, stream: AudioStreamInfo, job: TrueHDJob,
                  probe: Dict, options: TrueHDOptions,
                  clip_start: Optional[float], clip_seconds: Optional[int]) -> None:
        self.log("========================================")
        self.log("Starting Dolby TrueHD conversion" + (" (TEST CLIP)" if clip_seconds else ""))
        self.log(f"  Input: {source.name}")
        decode = "lossless FLAC decode" if is_flac(stream) else "lossless XLL decode"
        self.log(f"  Source: {stream.format_name} {stream.channel_layout}, "
                 f"{stream.channel_count} ch, {stream.sample_rate} Hz, "
                 f"{stream.bit_depth or '?'}-bit ({stream.sample_fmt} - {decode})")
        if job.source_layout and job.source_layout != job.layout:
            self.log(f"  Channel labels: {job.source_layout} -> {job.layout} (relabelled by position, "
                     "samples unchanged)")
        for v in streams_of_type(probe, "video"):
            self.log(f"  Video (copied, not re-encoded): {describe_video(v)}")
        self.log(f"  Output: {job.title}, {stream.channel_count} ch, {stream.sample_rate} Hz")
        self.log(f"  Encoder: {self.encoder}")
        if clip_seconds:
            self.log(f"  Test clip: {clip_seconds}s from {clip_start:.3f}s")
        self.log(f"  Keep original track: {'No' if job.remove_source else 'Yes'}")
        if job.remove_streams:
            self.log("  Also removing tracks: " + ", ".join(f"#{i}" for i in job.remove_streams))
        self.log(f"  Make TrueHD default: {'Yes' if job.make_default else 'No'}")
        self.log(f"  Strict lossless verification: {'Yes' if options.strict_verify else 'No'}")
        self.log("========================================")

    def _run(self, phase: str, cmd: List[str], total_seconds: Optional[float], failure: str,
             output: Optional[str] = None) -> None:
        self.log("Command: " + " ".join(_quote(c) for c in cmd))
        self.progress(phase, 0.0, "")

        def on_progress(p: FFmpegProgress):
            detail = f"{format_seconds(p.elapsed)} elapsed"
            if p.speed:
                detail += f" • {p.speed:.1f}x"
            if p.eta is not None:
                detail += f" • ~{format_seconds(p.eta)} remaining"
            self.progress(phase, p.fraction, detail)

        result = self.runner(cmd, total_seconds=total_seconds, on_progress=on_progress,
                             handle=self.handle)
        output = output or cmd[-1]
        if result.returncode != 0:
            raise TrueHDError(f"{failure} - the source was not changed.\n\n" + result.error_summary())
        if not os.path.exists(output) or os.path.getsize(output) == 0:
            raise TrueHDError(f"{failure}: no output was produced - the source was not changed.")

    def _encode(self, job: TrueHDJob, total_seconds: Optional[float],
                source_hash_path: Optional[str] = None,
                packet_sizes_path: Optional[str] = None) -> None:
        self._run("Encoding TrueHD",
                  build_truehd_command(self.encoder, job, source_hash_path, packet_sizes_path),
                  total_seconds, "TrueHD encoding failed", output=job.output_path)
        self.log("Encoding finished; verifying output...")

    def _verify_output(self, job: TrueHDJob, stream: AudioStreamInfo,
                       source_probe: Dict, result: TrueHDResult,
                       original_path: str, clip_start: Optional[float] = None,
                       packet_sizes_path: Optional[str] = None) -> None:
        """Check the output against ``source_probe`` (the film, or the cut clip)."""
        clip = clip_start is not None
        self.progress("Verifying output", None, "streams and metadata")
        out = run_ffprobe(self.ffprobe, job.output_path)
        checks = result.checks
        k = job.new_audio_index

        src_audio = streams_of_type(source_probe, "audio")
        out_audio = streams_of_type(out, "audio")
        removed = job.removed_indices
        kept_audio = [s for s in src_audio if s["index"] not in removed]
        checks.append(Check("Audio track count", len(out_audio) == len(kept_audio) + 1,
                            f"{len(out_audio)} (expected {len(kept_audio) + 1})"))

        new = out_audio[k] if len(out_audio) > k else None
        if new is None:
            checks.append(Check("TrueHD track present", False, "no audio stream at expected position"))
            return
        checks.append(Check("TrueHD track present", new.get("codec_name") == "truehd",
                            f"codec {new.get('codec_name')}"))
        checks.append(Check("Channel count", int(new.get("channels") or 0) == stream.channel_count,
                            f"{new.get('channels')} (expected {stream.channel_count})"))
        checks.append(Check("Channel layout", new.get("channel_layout") == job.layout,
                            f"{new.get('channel_layout')} (expected {job.layout})"))
        checks.append(Check("Sample rate", int(new.get("sample_rate") or 0) == stream.sample_rate,
                            f"{new.get('sample_rate')} Hz"))
        checks.append(Check("Not labelled Atmos", "atmos" not in (new.get("profile") or "").lower()
                            and "atmos" not in (tag(new, "title") or "").lower(), "channel-based TrueHD"))
        checks.append(Check("Track title", tag(new, "title") == job.title, tag(new, "title") or ""))
        if stream.language:
            checks.append(Check("Track language", tag(new, "language") == stream.language,
                                tag(new, "language") or "none"))

        sync = read_major_sync(self.encoder, job.output_path, k)
        sync_error = check_channel_assignment(job.layout, sync)
        checks.append(Check("TrueHD speaker assignment (bitstream)", sync_error is None,
                            sync_error or "matches source layout"))

        # Every access unit must fit the peak data rate declared in the header,
        # or hardware decoders (AV receivers) can glitch at loud passages.
        if sync and packet_sizes_path:
            rate_error, rate_summary = check_access_unit_sizes(
                packet_sizes_path, sync.peak_bitrate, stream.sample_rate)
            checks.append(Check("TrueHD data rate within declared peak", rate_error is None,
                                rate_error or rate_summary))

        # A/V sync: the new track must start at the same time as its source
        # track (the strict comparison then proves sample 0 lines up).
        src_track = next(s for s in src_audio if s["index"] == job.source_stream_index)
        try:
            offset = float(new.get("start_time", 0)) - float(src_track.get("start_time", 0))
        except (TypeError, ValueError):
            offset = None
        checks.append(Check("Audio sync with original track",
                            offset is not None and abs(offset) <= 0.002,
                            "start times match" if offset is not None and abs(offset) <= 0.002
                            else f"TrueHD starts {offset if offset is None else round(offset * 1000)} ms "
                                 "away from the original track"))

        # Other audio tracks keep codec / language / title / forced flag.
        for i, (before, after) in enumerate(zip(kept_audio, out_audio[:k])):
            same = (before.get("codec_name") == after.get("codec_name")
                    and before.get("channels") == after.get("channels")
                    and tag(before, "language") == tag(after, "language")
                    and tag(before, "title") == tag(after, "title")
                    and before.get("disposition", {}).get("forced") == after.get("disposition", {}).get("forced"))
            checks.append(Check(f"Audio track {i + 1} preserved", same,
                                f"{classify_audio(after)} [{tag(after, 'language') or '-'}]"))

        defaults = [i for i, s in enumerate(out_audio) if s.get("disposition", {}).get("default")]
        if job.make_default:
            checks.append(Check("Default audio track", defaults == [k],
                                f"default flags on audio {[d + 1 for d in defaults]}"))
        else:
            expected = [i for i, s in enumerate(kept_audio) if s.get("disposition", {}).get("default")]
            checks.append(Check("Default audio track", defaults == expected,
                                f"default flags on audio {[d + 1 for d in defaults]}"))

        for kind, label in (("subtitle", "Subtitle"), ("attachment", "Attachment")):
            before = [s for s in streams_of_type(source_probe, kind) if s["index"] not in removed]
            after = streams_of_type(out, kind)
            same = len(before) == len(after) and all(
                b.get("codec_name") == a.get("codec_name")
                and tag(b, "language") == tag(a, "language")
                and tag(b, "title") == tag(a, "title")
                and tag(b, "filename") == tag(a, "filename")
                and b.get("disposition", {}).get("default") == a.get("disposition", {}).get("default")
                and b.get("disposition", {}).get("forced") == a.get("disposition", {}).get("forced")
                for b, a in zip(before, after))
            checks.append(Check(f"{label} tracks preserved", same, f"{len(after)} of {len(before)}"))

        if not clip:
            before, after = len(source_probe.get("chapters", [])), len(out.get("chapters", []))
            checks.append(Check("Chapters preserved", before == after, f"{after} of {before}"))

        src_video, out_video = streams_of_type(source_probe, "video"), streams_of_type(out, "video")
        checks.append(Check("Video track count", len(src_video) == len(out_video),
                            f"{len(out_video)} of {len(src_video)}"))
        for i, (before, after) in enumerate(zip(src_video, out_video)):
            if before.get("disposition", {}).get("attached_pic"):
                continue
            sig_before, sig_after = video_signature(before), video_signature(after)
            diff = [key for key in sig_before if sig_before[key] != sig_after.get(key)]
            checks.append(Check(f"Video {i + 1} format/HDR/Dolby Vision metadata", not diff,
                                describe_video(after) if not diff else f"changed: {', '.join(diff)}"))

        self._check_copied_packets(job, source_probe, out, original_path, clip_start, checks)

        out_duration, expected = format_duration(out), format_duration(source_probe)
        if out_duration and expected:
            checks.append(Check("Duration", abs(out_duration - expected) <= max(2.0, expected * 0.002),
                                f"{format_seconds(out_duration)} (expected ~{format_seconds(expected)})"))

        for c in checks:
            self.log(f"  {'✓' if c.passed else '✗'} {c.name}: {c.detail}")

    def _check_copied_packets(self, job: TrueHDJob, source_probe: Dict, out: Dict,
                              original_path: str, clip_start: Optional[float],
                              checks: List["Check"]) -> None:
        """Every copied track (video, other audio, subtitles) must contain the
        same packet payloads as the original.

        Both files are read by the independent standard FFmpeg, which has zlib,
        so a track the encoder build mis-read (e.g. zlib-compressed PGS
        subtitles) shows up as a difference. Covers the first
        PACKET_CHECK_SECONDS of the film, or the whole test clip.
        """
        self.progress("Verifying output", None, "comparing copied tracks with the original")
        new_index = streams_of_type(out, "audio")[job.new_audio_index]["index"]
        src_streams = [st for st in source_probe.get("streams", [])
                       if st.get("codec_type") not in ("attachment", None)
                       and st["index"] not in job.removed_indices]
        out_streams = [st for st in out.get("streams", [])
                       if st.get("codec_type") not in ("attachment", None) and st["index"] != new_index]
        if len(src_streams) != len(out_streams):
            checks.append(Check("Copied tracks identical to original", False,
                                f"{len(out_streams)} copied tracks, expected {len(src_streams)}"))
            return
        clip = clip_start is not None
        try:
            src = stream_packet_hashes(self.reference, original_path, [st["index"] for st in src_streams],
                                       PACKET_CHECK_SECONDS, clip_start if clip else None)
            dst = stream_packet_hashes(self.reference, job.output_path, [st["index"] for st in out_streams],
                                       PACKET_CHECK_SECONDS)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            checks.append(Check("Copied tracks identical to original", False, str(e)))
            return

        problems, compared = [], 0
        for before, after in zip(src_streams, out_streams):
            # Packets near the end of a time-limited read can be included or
            # dropped differently (reordered video frames), so ignore the last
            # 2 seconds of the new file's window; compare payloads only.
            O_full = dst[after["index"]]
            if O_full:
                end = max(p[2] for p in O_full) - 2.0
                O_full = [p for p in O_full if p[2] <= end] or O_full[:1]
            S = [(p[0], p[1]) for p in src[before["index"]]]
            O = [(p[0], p[1]) for p in O_full]
            label = f"{before.get('codec_type')} #{before['index']} ({before.get('codec_name')}" + (
                f", {tag(before, 'language')})" if tag(before, "language") else ")")
            if not O:
                if S and not clip:
                    problems.append(f"{label}: no packets in the new file")
                continue
            try:
                start = S.index(O[0])
            except ValueError:
                problems.append(f"{label}: packets differ from the original")
                continue
            n = min(len(O), len(S) - start)
            if (not clip and start != 0) or S[start:start + n] != O[:n]:
                problems.append(f"{label}: packets differ from the original")
                continue
            compared += n
        window = "the whole clip" if clip else f"the first {int(PACKET_CHECK_SECONDS // 60)} minutes"
        checks.append(Check("Copied tracks identical to original", not problems,
                            "; ".join(problems[:4]) + (" ..." if len(problems) > 4 else "") if problems
                            else f"{len(out_streams)} tracks, {compared:,} packets byte-identical over {window} "
                                 "(read by independent FFmpeg)"))

    def _strict_compare(self, job: TrueHDJob, stream: AudioStreamInfo,
                        total_seconds: Optional[float], result: TrueHDResult,
                        source_hash_path: Optional[str], output_hash_path: Optional[str]) -> None:
        """Prove every decoded sample of the new TrueHD track equals the source.

        The SHA-256 of the decoded source PCM (exactly what the encoder was
        given) was written during the encode pass. Here the TrueHD track is
        decoded from the finished MKV by the independent FFmpeg build and
        hashed the same way. Only the output file is read; the source film is
        not read again. On a mismatch the slow sample-by-sample comparison is
        run to report where the first difference is.
        """
        decoder_name = "independent FFmpeg" if self.reference != self.encoder else "encoder build"
        self.log("Strict lossless verification: decoding the TrueHD track from the new MKV "
                 "and comparing its SHA-256 with the source audio's...")
        self.log(f"  TrueHD decoded by: {decoder_name} ({self.reference})")

        source_digest = read_hash_file(source_hash_path) if source_hash_path else None
        if not source_digest:
            result.lossless = False
            result.lossless_detail = "the source audio fingerprint was not produced during encoding"
            self.log(f"  ✗ Lossless comparison: {result.lossless_detail}")
            return

        self._run("Verifying lossless audio",
                  build_output_hash_command(self.reference, job.output_path,
                                            job.new_audio_index, output_hash_path),
                  total_seconds, "Lossless verification could not decode the TrueHD track")
        output_digest = read_hash_file(output_hash_path)
        bits = stream.bit_depth or 24

        if output_digest == source_digest:
            result.lossless = True
            result.lossless_detail = (
                f"SHA-256 of all decoded samples identical "
                f"({format_seconds(total_seconds)} of {stream.channel_count}-channel audio, "
                f"{bits}-bit integer PCM; TrueHD decoded by {decoder_name})")
            self.log(f"  ✓ Lossless comparison: {result.lossless_detail}")
            self.log(f"    SHA-256 {source_digest}")
            return

        self.log(f"  ✗ Fingerprints differ (source {source_digest}, TrueHD {output_digest}); "
                 "locating the first difference...")
        self._locate_difference(job, stream, total_seconds, result)
        if result.lossless:
            # Should be impossible: hashes differ but samples match.
            result.lossless = False
            result.lossless_detail = ("the audio fingerprints differ, although a sample comparison "
                                      "found no difference")

    def _locate_difference(self, job: TrueHDJob, stream: AudioStreamInfo,
                           total_seconds: Optional[float], result: TrueHDResult) -> None:
        """Slow path: decode source and TrueHD output side by side and find the
        first differing sample (only used after a fingerprint mismatch)."""
        channels = stream.channel_count
        src_cmd = build_source_decode_command(self.encoder, job)
        out_cmd = build_output_decode_command(self.reference, job.output_path, job.new_audio_index)

        frame_bytes = 4 * channels
        expected_bytes = int((total_seconds or 0) * stream.sample_rate * frame_bytes)
        chunk = frame_bytes * 48000      # one second at 48 kHz
        started = time.monotonic()
        procs = []
        try:
            for cmd in (src_cmd, out_cmd):
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, creationflags=subprocess_flags())
                procs.append(proc)
            compared = 0
            mismatch = None
            while True:
                if self.handle.cancelled.is_set():
                    raise ConversionCancelled("Verification cancelled by user")
                a = _read_exact(procs[0].stdout, chunk)
                b = _read_exact(procs[1].stdout, chunk)
                if a != b:
                    mismatch = _first_difference(a, b, compared, frame_bytes)
                    break
                if not a:
                    break
                compared += len(a)
                if expected_bytes:
                    elapsed = time.monotonic() - started
                    fraction = min(1.0, compared / expected_bytes)
                    eta = elapsed * (1 - fraction) / fraction if fraction > 0.001 else None
                    self.progress("Locating the difference", fraction,
                                  f"{format_seconds(elapsed)} elapsed"
                                  + (f" • ~{format_seconds(eta)} remaining" if eta is not None else ""))
            codes = []
            for proc in procs:
                proc.stdout.close()
                codes.append(proc.wait())
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

        frames = compared // frame_bytes
        if mismatch:
            result.lossless = False
            result.lossless_detail = mismatch
        elif any(codes):
            result.lossless = False
            result.lossless_detail = f"a decoder failed during comparison (exit codes {codes})"
        elif frames == 0:
            result.lossless = False
            result.lossless_detail = "no audio samples were decoded"
        else:
            result.lossless = True
            result.lossless_detail = (
                f"{frames:,} samples x {channels} channels identical "
                f"({format_seconds(frames / stream.sample_rate)} of audio, "
                f"{stream.bit_depth or 24}-bit integer PCM)")
        self.log(f"  {'✓' if result.lossless else '✗'} Lossless comparison: {result.lossless_detail}")


def _read_exact(pipe, size: int) -> bytes:
    parts, remaining = [], size
    while remaining:
        data = pipe.read(remaining)
        if not data:
            break
        parts.append(data)
        remaining -= len(data)
    return b"".join(parts)


def _first_difference(a: bytes, b: bytes, offset: int, frame_bytes: int) -> str:
    if len(a) != len(b) and a[:min(len(a), len(b))] == b[:min(len(a), len(b))]:
        shorter = "source" if len(a) < len(b) else "TrueHD output"
        return (f"sample counts differ: the {shorter} ends after "
                f"{(offset + min(len(a), len(b))) // frame_bytes:,} samples")
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            sample = (offset + i) // frame_bytes
            channel = (i % frame_bytes) // 4
            return f"first difference at sample {sample:,}, channel {channel + 1}"
    return "decoded audio differs"


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg
