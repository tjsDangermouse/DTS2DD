"""Safe repairs for files found by the Library Scanner.

Repairs only run when the user selects them. Each file gets ONE pass that
combines its selected repairs where possible:

  * FLAC -> TrueHD or DTS-HD MA -> TrueHD: the existing TrueHDConverter
    (verified, lossless, strict PCM hash comparison), with subtitle removal
    folded into the same pass.
  * DTS -> AC-3 / E-AC-3: the existing convert_and_append_audio, with
    subtitle removal and default flags in the same pass.
  * Subtitle cleanup and/or default-audio fix only: one stream-copy remux.

Video is never re-encoded. Output goes to a temporary file beside the
source, is validated (tracks, metadata, Dolby Vision/HDR signature, chapters,
attachments, duration, byte-identical copied packets) and only then replaces
the original. A failure leaves the original untouched.
"""
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from audio_stream_info import AudioStreamInfo
from compatibility_rules import (
    ACTION_CLEAN_SUBS, ACTION_DTS_CONVERT, ACTION_FIX_DEFAULT, ACTION_FLAC_TRUEHD, ACTION_LABELS,
    ScannerConfig, SubtitlePlan, flac_target, sub_description, subtitle_cleanup_plan,
)
from ffmpeg_helper import (
    ConversionCancelled, ProcessHandle, convert_and_append_audio, remux_in_place,
)
from library_scanner import ScanItem
from media_inspector import AudioTrack
from media_probe import (
    format_duration, run_ffprobe, stream_packet_hashes, streams_of_type, tag, video_signature,
)
from truehd_encoder import LAYOUT_LABELS, EncoderReport, TrueHDError
from truehd_workflow import (
    TrueHDConverter, TrueHDOptions, source_warnings, truehd_target_layout, validate_source,
)

# Copied packets are compared over this much of the start of the film.
REMUX_PACKET_CHECK_SECONDS = 30.0
DISK_MARGIN_BYTES = 512 * 1024 * 1024

STATUS_REPAIRED = "Repaired"
STATUS_PARTIAL = "Repaired (some steps skipped)"
STATUS_SKIPPED = "Skipped"
STATUS_FAILED = "Failed"
STATUS_CANCELLED = "Cancelled"


@dataclass
class RepairOptions:
    remove_unlabelled_subs: bool = False     # the UI confirms this explicitly
    remove_duplicate_subs: bool = False
    keep_original_audio: bool = True         # keep FLAC / DTS beside the new track
    make_new_default: bool = True            # new track becomes the default audio track
    strict_verify: bool = True               # TrueHD: SHA-256 of every decoded sample
    replace_original: bool = True            # TrueHD: replace after verification, else save a new MKV
    dts_target: str = "auto"                 # auto / truehd / eac3 / ac3
    lossy_bitrate: str = "640k"


@dataclass
class AudioJob:
    kind: str                     # "truehd", "eac3", "ac3"
    action: str                   # ACTION_FLAC_TRUEHD / ACTION_DTS_CONVERT
    track: AudioTrack
    target_label: str
    warnings: List[str] = field(default_factory=list)
    bitrate: Optional[str] = None     # AC-3 / E-AC-3 only


@dataclass
class RepairPlan:
    item: ScanItem
    requested: List[str]
    subtitle_plan: Optional[SubtitlePlan] = None
    audio_job: Optional[AudioJob] = None
    dispositions: Dict[str, str] = field(default_factory=dict)   # output spec -> +default / -default
    default_description: str = ""
    default_by_new_track: bool = False    # the new audio track becomes the only default
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (action, reason)
    notes: List[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.item.path

    @property
    def remove_streams(self) -> List[int]:
        return self.subtitle_plan.remove_indices if self.subtitle_plan else []

    @property
    def method(self) -> Optional[str]:
        if self.audio_job:
            return "truehd" if self.audio_job.kind == "truehd" else "lossy"
        if self.remove_streams or self.dispositions:
            return "remux"
        return None

    @property
    def actions(self) -> List[str]:
        done = []
        if self.audio_job:
            done.append(self.audio_job.action)
        if self.remove_streams:
            done.append(ACTION_CLEAN_SUBS)
        if self.dispositions or (ACTION_FIX_DEFAULT in self.requested and self.default_by_new_track):
            done.append(ACTION_FIX_DEFAULT)
        return done

    def describe(self) -> List[str]:
        lines = [self.item.report.name]
        if self.audio_job:
            j = self.audio_job
            lines.append(f"  • {ACTION_LABELS[j.action]}: audio #{j.track.index} {j.track.label} → {j.target_label}")
            for w in j.warnings:
                lines.append(f"      ⚠ {w}")
        if self.subtitle_plan and self.remove_streams:
            sp = self.subtitle_plan
            lines.append(f"  • Clean subtitles: keep {len(sp.keep)}, remove {len(sp.remove)}")
            for s, reason in sp.keep:
                lines.append(f"      keep    {sub_description(s)} - {reason}")
            for s, reason in sp.remove:
                lines.append(f"      REMOVE  {sub_description(s)} - {reason}")
            for note in sp.notes:
                lines.append(f"      note: {note}")
        if self.dispositions:
            lines.append(f"  • Fix default audio: {self.default_description}")
        for note in self.notes:
            lines.append(f"  • {note}")
        for action, reason in self.skipped:
            lines.append(f"  - skipped {ACTION_LABELS.get(action, action)}: {reason}")
        if not self.method:
            lines.append("  (nothing to do)")
        return lines


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def stream_info(a: AudioTrack) -> AudioStreamInfo:
    """Adapt a scanner AudioTrack to the model used by the conversion code."""
    return AudioStreamInfo(
        stream_index=a.index, codec_name=a.codec.upper(),
        channels=a.channel_layout or f"{a.channels} channels", bitrate=None, language=a.language,
        audio_index=a.audio_index, format_name=a.format_name, profile=a.profile,
        channel_count=a.channels, channel_layout=a.channel_layout, sample_rate=a.sample_rate,
        sample_fmt=a.sample_fmt, bit_depth=a.bit_depth, title=a.title,
        is_default=a.default, is_forced=a.forced,
    )


def _pick(tracks: Sequence[AudioTrack]) -> Optional[AudioTrack]:
    """Default track first, then most channels."""
    return max(tracks, key=lambda a: (a.default, a.channels or 0, -a.index), default=None)


def _already_has(report, codec: str, channels: Optional[int]) -> bool:
    return any(a.codec == codec and a.channels == channels for a in report.audio)


def _plan_flac(plan: RepairPlan, options: RepairOptions, encoder: Optional[EncoderReport]) -> None:
    report = plan.item.report
    flac = [a for a in report.audio if a.codec == "flac"]
    if not flac:
        plan.skipped.append((ACTION_FLAC_TRUEHD, "no FLAC track"))
        return
    eligible = [a for a in flac if flac_target(a)[0]]
    if not eligible:
        plan.skipped.append((ACTION_FLAC_TRUEHD, flac_target(flac[0])[1][0]))
        return
    multichannel = [a for a in eligible if (a.channels or 0) > 2]
    track = _pick(multichannel or eligible)
    if _already_has(report, "truehd", track.channels):
        plan.skipped.append((ACTION_FLAC_TRUEHD, f"the file already has a {track.channels}-channel TrueHD track"))
        return
    stream = stream_info(track)
    problems = validate_source(stream, encoder)
    if problems:
        plan.skipped.append((ACTION_FLAC_TRUEHD, problems[0]))
        return
    target = truehd_target_layout(stream)
    plan.audio_job = AudioJob("truehd", ACTION_FLAC_TRUEHD, track,
                              f"Dolby TrueHD {LAYOUT_LABELS[target]} (lossless, verified)",
                              source_warnings(stream))


def _plan_dts(plan: RepairPlan, options: RepairOptions, encoder: Optional[EncoderReport]) -> None:
    report = plan.item.report
    dts = [a for a in report.audio if a.codec == "dts"]
    if not dts:
        plan.skipped.append((ACTION_DTS_CONVERT, "no DTS track"))
        return
    track = _pick([a for a in dts if (a.profile or "").startswith("DTS-HD MA")] or dts)
    stream = stream_info(track)
    target = options.dts_target
    truehd_problems = validate_source(stream, encoder)
    if target in ("auto", "truehd"):
        if not truehd_problems:
            if _already_has(report, "truehd", track.channels):
                plan.skipped.append((ACTION_DTS_CONVERT, "the file already has a TrueHD track with these channels"))
                return
            plan.audio_job = AudioJob("truehd", ACTION_DTS_CONVERT, track,
                                      f"Dolby TrueHD {LAYOUT_LABELS[truehd_target_layout(stream)]} "
                                      "(lossless, verified)", source_warnings(stream))
            return
        if target == "truehd":
            plan.skipped.append((ACTION_DTS_CONVERT, truehd_problems[0]))
            return
        target = "eac3"
    codec = "eac3" if target == "eac3" else "ac3"
    if _already_has(report, codec, min(track.channels or 6, 6)):
        plan.skipped.append((ACTION_DTS_CONVERT, f"the file already has a {codec.upper()} track"))
        return
    bitrate = options.lossy_bitrate if codec == "eac3" else min(options.lossy_bitrate, "640k",
                                                                 key=lambda b: int(b.rstrip("k")))
    warnings = ["Lossy conversion (the existing DTS2DD AC-3/E-AC-3 workflow)."]
    if (track.channels or 0) > 6:
        warnings.append(f"{track.channels}-channel source: AC-3/E-AC-3 carry at most 5.1, so FFmpeg "
                        "downmixes to 5.1.")
    name = "Dolby Digital Plus" if codec == "eac3" else "Dolby Digital"
    plan.audio_job = AudioJob(codec, ACTION_DTS_CONVERT, track, f"{name} {bitrate}", warnings, bitrate)


def _plan_default(plan: RepairPlan, options: RepairOptions) -> None:
    report = plan.item.report
    defaults = [a for a in report.audio if a.default]
    if plan.audio_job and options.make_new_default:
        plan.default_by_new_track = True
        plan.notes.append("Default audio: the new track becomes the only default track.")
        return
    if plan.audio_job:
        plan.skipped.append((ACTION_FIX_DEFAULT, "an audio conversion runs in this pass without making the new "
                                                 "track default; run the fix again afterwards"))
        return
    if not report.audio:
        plan.skipped.append((ACTION_FIX_DEFAULT, "no audio tracks"))
        return
    if len(defaults) == 1:
        plan.skipped.append((ACTION_FIX_DEFAULT, "exactly one default audio track already"))
        return
    target = defaults[0] if defaults else report.audio[0]
    for a in report.audio:
        plan.dispositions[f"a:{a.audio_index}"] = "+default" if a is target else "-default"
    reason = "first of the tracks flagged default" if defaults else "first audio track"
    plan.default_description = f"audio #{target.index} {target.label} ({reason}) becomes the only default"


def plan_repair(item: ScanItem, actions: Sequence[str], options: RepairOptions, config: ScannerConfig,
                encoder: Optional[EncoderReport]) -> RepairPlan:
    plan = RepairPlan(item=item, requested=list(actions))
    report = item.report
    if report.errors:
        plan.skipped += [(a, "the file could not be analysed") for a in actions]
        return plan
    if not report.is_mkv:
        plan.skipped += [(a, "repairs are only performed on MKV files") for a in actions]
        return plan
    if ACTION_CLEAN_SUBS in actions:
        sp = subtitle_cleanup_plan(report.subtitles, config, options.remove_unlabelled_subs,
                                   options.remove_duplicate_subs)
        if sp.remove:
            plan.subtitle_plan = sp
        else:
            plan.skipped.append((ACTION_CLEAN_SUBS, "no subtitle track would be removed with these preferences"))
    if ACTION_FLAC_TRUEHD in actions:
        _plan_flac(plan, options, encoder)
    if ACTION_DTS_CONVERT in actions:
        if plan.audio_job:
            plan.skipped.append((ACTION_DTS_CONVERT, "one audio conversion per file per run (FLAC first); "
                                                     "run it again afterwards"))
        else:
            _plan_dts(plan, options, encoder)
    if ACTION_FIX_DEFAULT in actions:
        _plan_default(plan, options)
    if plan.audio_job and not options.keep_original_audio:
        plan.notes.append(f"The original audio #{plan.audio_job.track.index} is removed after conversion.")
    if plan.audio_job and plan.audio_job.kind == "truehd" and not options.replace_original:
        plan.notes.append("The verified result is saved as a new …_Sound_TrueHD.mkv beside the original.")
    return plan


# --------------------------------------------------------------------------
# Validation of a remuxed / appended file
# --------------------------------------------------------------------------

@dataclass
class Expectation:
    removed: List[int]                 # absolute source indices left out
    new_audio_codec: Optional[str] = None
    default_audio: Optional[List[int]] = None   # expected output audio indices flagged default


def validate_output(ffmpeg: str, ffprobe: str, source_path: str, source_probe: Dict, out_path: str,
                    expect: Expectation, log: Callable[[str], None] = print,
                    packet_seconds: float = REMUX_PACKET_CHECK_SECONDS) -> None:
    """Raise RuntimeError unless ``out_path`` is the source minus ``removed``
    (plus one new audio track) with every kept track unchanged."""
    out = run_ffprobe(ffprobe, out_path)
    problems = []
    removed = set(expect.removed)

    def same(a: Dict, b: Dict, *, flags=("forced",)) -> bool:
        return (a.get("codec_name") == b.get("codec_name") and tag(a, "language") == tag(b, "language")
                and tag(a, "title") == tag(b, "title")
                and all((a.get("disposition") or {}).get(f) == (b.get("disposition") or {}).get(f) for f in flags))

    # Video: same tracks, same format and HDR / Dolby Vision signature.
    src_v, out_v = streams_of_type(source_probe, "video"), streams_of_type(out, "video")
    if len(src_v) != len(out_v):
        problems.append(f"video tracks {len(out_v)} (expected {len(src_v)})")
    for i, (a, b) in enumerate(zip(src_v, out_v)):
        if video_signature(a) != video_signature(b):
            problems.append(f"video {i + 1} format / HDR / Dolby Vision metadata changed")
        if not same(a, b, flags=("default", "forced", "attached_pic")):
            problems.append(f"video {i + 1} flags or tags changed")

    src_a = [s for s in streams_of_type(source_probe, "audio") if s["index"] not in removed]
    out_a = streams_of_type(out, "audio")
    extra = 1 if expect.new_audio_codec else 0
    if len(out_a) != len(src_a) + extra:
        problems.append(f"audio tracks {len(out_a)} (expected {len(src_a) + extra})")
    else:
        for i, (a, b) in enumerate(zip(src_a, out_a)):
            if not same(a, b) or a.get("channels") != b.get("channels"):
                problems.append(f"audio track {i + 1} changed")
        if extra and out_a[-1].get("codec_name") != expect.new_audio_codec:
            problems.append(f"new audio track is {out_a[-1].get('codec_name')}, expected {expect.new_audio_codec}")
    defaults = [i for i, s in enumerate(out_a) if (s.get("disposition") or {}).get("default")]
    wanted = expect.default_audio
    if wanted is None:
        wanted = [i for i, s in enumerate(src_a) if (s.get("disposition") or {}).get("default")]
    if defaults != wanted:
        problems.append(f"default audio flags on {[d + 1 for d in defaults]}, expected {[w + 1 for w in wanted]}")

    for kind in ("subtitle", "attachment"):
        before = [s for s in streams_of_type(source_probe, kind) if s["index"] not in removed]
        after = streams_of_type(out, kind)
        if len(before) != len(after) or not all(
                same(a, b, flags=("forced", "default", "hearing_impaired")) and tag(a, "filename") == tag(b, "filename")
                for a, b in zip(before, after)):
            problems.append(f"{kind} tracks or their language/title/forced/default flags changed "
                            f"({len(after)} of {len(before)})")
    if len(source_probe.get("chapters") or []) != len(out.get("chapters") or []):
        problems.append("chapters changed")
    d_src, d_out = format_duration(source_probe), format_duration(out)
    if d_src and d_out and abs(d_src - d_out) > max(2.0, d_src * 0.002):
        problems.append(f"duration {d_out:.1f}s, expected {d_src:.1f}s")

    if not problems:
        # Copied tracks must be byte-identical (read by FFmpeg from both files).
        src_ids = [s["index"] for s in source_probe.get("streams", [])
                   if s.get("codec_type") in ("video", "audio", "subtitle") and s["index"] not in removed]
        out_streams = [s for s in out.get("streams", []) if s.get("codec_type") in ("video", "audio", "subtitle")]
        if extra:
            new_index = out_a[-1]["index"]
            out_streams = [s for s in out_streams if s["index"] != new_index]
        out_ids = [s["index"] for s in out_streams]
        try:
            src_hashes = stream_packet_hashes(ffmpeg, source_path, src_ids, packet_seconds)
            out_hashes = stream_packet_hashes(ffmpeg, out_path, out_ids, packet_seconds)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            problems.append(f"could not compare packets: {e}")
        else:
            compared = 0
            for si, oi in zip(src_ids, out_ids):
                o = out_hashes[oi]
                if o:
                    end = max(p[2] for p in o) - 2.0
                    o = [p for p in o if p[2] <= end] or o[:1]
                S = [(p[0], p[1]) for p in src_hashes[si]]
                O = [(p[0], p[1]) for p in o]
                if S[:len(O)] != O or (S and not O):
                    problems.append(f"stream #{si} packets differ from the original")
                compared += len(O)
            if not problems:
                log(f"  ✓ {compared:,} copied packets byte-identical over the first {int(packet_seconds)} s")
    if problems:
        raise RuntimeError("Validation failed - the original was not changed:\n  " + "\n  ".join(problems))
    log("  ✓ Tracks, flags, HDR/Dolby Vision metadata, chapters and attachments verified")


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

class RepairSkipped(RuntimeError):
    pass


@dataclass
class RepairContext:
    ffmpeg: str
    ffprobe: str
    encoder: Optional[EncoderReport]
    options: RepairOptions
    handle: ProcessHandle = field(default_factory=ProcessHandle)
    log: Callable[[str], None] = print
    progress: Callable[[str, Optional[float], str], None] = lambda phase, fraction, detail: None


@dataclass
class RepairOutcome:
    path: str
    status: str
    done: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    message: str = ""
    final_path: Optional[str] = None


def _check_unchanged(plan: RepairPlan) -> None:
    report = plan.item.report
    try:
        st = os.stat(plan.path)
    except OSError as e:
        raise RepairSkipped(f"file not accessible: {e}")
    if st.st_size != report.size or st.st_mtime_ns != report.mtime_ns:
        raise RepairSkipped("the file changed after it was scanned; scan again first")


def _check_space(path: str, extra: int = 0) -> None:
    size = os.path.getsize(path)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free
    needed = size + extra + DISK_MARGIN_BYTES
    if free < needed:
        raise RuntimeError(f"Not enough free disk space: need about {needed / 1e9:.1f} GB, "
                           f"{free / 1e9:.1f} GB available")


def _ffmpeg_progress(ctx: RepairContext, phase: str):
    from media_probe import format_seconds

    def report(p):
        detail = f"{format_seconds(p.elapsed)} elapsed"
        if p.eta is not None:
            detail += f" • ~{format_seconds(p.eta)} remaining"
        if p.write_rate:
            detail += f" • {p.write_rate / 1e6:.0f} MB/s"
        ctx.progress(phase, p.fraction, detail)
    return report


def execute_plan(plan: RepairPlan, ctx: RepairContext) -> RepairOutcome:
    outcome = RepairOutcome(plan.path, STATUS_SKIPPED, skipped=list(plan.skipped))
    method = plan.method
    if method is None:
        outcome.message = "nothing to do"
        return outcome
    try:
        _check_unchanged(plan)
    except RepairSkipped as e:
        outcome.message = str(e)
        outcome.skipped += [(a, str(e)) for a in plan.actions]
        return outcome

    name = Path(plan.path).name
    ctx.log("========================================")
    for line in plan.describe():
        ctx.log(line)
    try:
        if method == "truehd":
            outcome.final_path = _run_truehd(plan, ctx)
        else:
            _check_space(plan.path)
            source_probe = run_ffprobe(ctx.ffprobe, plan.path)
            if method == "lossy":
                _run_lossy(plan, ctx, source_probe)
            else:
                expect = Expectation(removed=plan.remove_streams)
                if plan.dispositions:
                    expect.default_audio = [int(spec.split(":")[1]) for spec, v in plan.dispositions.items()
                                            if v == "+default"]
                validate = lambda tmp: validate_output(ctx.ffmpeg, ctx.ffprobe, plan.path, source_probe,
                                                       tmp, expect, ctx.log)
                remux_in_place(plan.path, ctx.ffmpeg, plan.remove_streams, plan.dispositions,
                               progress_callback=ctx.log, handle=ctx.handle,
                               on_progress=_ffmpeg_progress(ctx, "Remuxing"), validate=validate)
            outcome.final_path = plan.path
        outcome.done = plan.actions
        outcome.status = STATUS_PARTIAL if plan.skipped else STATUS_REPAIRED
        outcome.message = "verified and saved"
        ctx.log(f"✓ {name}: {', '.join(ACTION_LABELS[a] for a in outcome.done)}")
    except ConversionCancelled:
        outcome.status = STATUS_CANCELLED
        outcome.message = "cancelled - the original was not changed"
        ctx.log(f"Cancelled: {name} - the original was not changed")
    except (TrueHDError, RuntimeError, OSError) as e:
        outcome.status = STATUS_FAILED
        outcome.message = str(e)
        ctx.log(f"✗ {name}: {e}")
    return outcome


def _run_truehd(plan: RepairPlan, ctx: RepairContext) -> str:
    job = plan.audio_job
    encoder = ctx.encoder
    if encoder is None or not encoder.path:
        raise TrueHDError("No verified TrueHD encoder")
    options = ctx.options
    converter = TrueHDConverter(encoder=encoder.path, ffprobe=ctx.ffprobe, reference_ffmpeg=ctx.ffmpeg,
                                report=encoder, log=ctx.log, progress=ctx.progress, handle=ctx.handle)
    truehd_options = TrueHDOptions(
        remove_source=not options.keep_original_audio,
        make_default=options.make_new_default,
        strict_verify=options.strict_verify,
        replace_original=options.replace_original,
        remove_streams=tuple(plan.remove_streams),
    )
    result = converter.convert(plan.path, job.track.index, truehd_options)
    if result.awaiting_replace_confirmation:
        # The user approved replacing originals when starting the batch.
        try:
            return converter.finalize_replace(result)
        except OSError as e:
            path = converter.finalize_as_new(result)
            ctx.log(f"Could not replace the original ({e}); saved as {Path(path).name}")
            plan.notes.append(f"original could not be replaced ({e}); saved as {Path(path).name}")
            return path
    return result.final_path


def _run_lossy(plan: RepairPlan, ctx: RepairContext, source_probe: Dict) -> None:
    job = plan.audio_job
    options = ctx.options
    report = plan.item.report
    bitrate = job.bitrate
    remove_source = not options.keep_original_audio
    removed = list(plan.remove_streams) + ([job.track.index] if remove_source else [])
    kept_audio = [a for a in report.audio if a.index not in removed]
    new_index = len(kept_audio)
    expect = Expectation(removed=removed, new_audio_codec=job.kind,
                         default_audio=[new_index] if options.make_new_default else None)
    channels = min(job.track.channels or 6, 6)
    layout = {6: "5.1", 2: "2.0", 1: "1.0"}.get(channels, f"{channels}ch")
    title = f"{'Dolby Digital Plus' if job.kind == 'eac3' else 'Dolby Digital'} {layout}"
    validate = lambda tmp: validate_output(ctx.ffmpeg, ctx.ffprobe, plan.path, source_probe, tmp, expect, ctx.log)
    convert_and_append_audio(
        plan.path, bitrate, ctx.ffmpeg, job.track.index, len(report.audio), codec=job.kind,
        remove_source_stream=remove_source, progress_callback=ctx.log, handle=ctx.handle,
        remove_streams=plan.remove_streams, removed_other_audio=0,
        on_progress=_ffmpeg_progress(ctx, f"Converting to {job.kind.upper()}"),
        make_default=options.make_new_default, validate=validate, title=title)


@dataclass
class BatchReport:
    outcomes: List[RepairOutcome] = field(default_factory=list)

    def count(self, *statuses: str) -> int:
        return sum(1 for o in self.outcomes if o.status in statuses)

    def text(self) -> str:
        lines = [f"Repaired: {self.count(STATUS_REPAIRED, STATUS_PARTIAL)}   "
                 f"Skipped: {self.count(STATUS_SKIPPED)}   Failed: {self.count(STATUS_FAILED)}   "
                 f"Cancelled: {self.count(STATUS_CANCELLED)}", ""]
        for status in (STATUS_REPAIRED, STATUS_PARTIAL, STATUS_SKIPPED, STATUS_FAILED, STATUS_CANCELLED):
            group = [o for o in self.outcomes if o.status == status]
            if not group:
                continue
            lines.append(f"{status}:")
            for o in group:
                lines.append(f"  {Path(o.path).name}")
                if o.done:
                    lines.append("    done: " + ", ".join(ACTION_LABELS[a] for a in o.done))
                if o.final_path and o.final_path != o.path:
                    lines.append(f"    saved as: {Path(o.final_path).name}")
                for action, reason in o.skipped:
                    lines.append(f"    skipped {ACTION_LABELS.get(action, action)}: {reason}")
                if o.message and o.status in (STATUS_FAILED, STATUS_SKIPPED, STATUS_CANCELLED):
                    lines.append(f"    {o.message.splitlines()[0]}")
            lines.append("")
        return "\n".join(lines)


def run_batch(plans: Sequence[RepairPlan], ctx: RepairContext,
              on_outcome: Callable[[RepairOutcome], None] = lambda o: None,
              on_start: Callable[[RepairPlan], None] = lambda p: None,
              executor: Callable[[RepairPlan, RepairContext], RepairOutcome] = execute_plan) -> BatchReport:
    """Repair files one after another; a cancel stops the current file and skips the rest."""
    batch = BatchReport()
    for plan in plans:
        if ctx.handle.cancelled.is_set():
            outcome = RepairOutcome(plan.path, STATUS_CANCELLED, message="not started (batch cancelled)")
        else:
            on_start(plan)
            outcome = executor(plan, ctx)
        batch.outcomes.append(outcome)
        on_outcome(outcome)
    return batch
