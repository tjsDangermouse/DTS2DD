"""Creating queue jobs and running them with the existing workflows.

Nothing new is done to the films here: every executor calls the same code as
the Convert File tab (convert_and_append_audio, remove_streams_in_place,
TrueHDConverter) or the Library Scanner (plan_repair / execute_plan).
"""
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from compatibility_rules import ACTION_LABELS, ScannerConfig
from ffmpeg_helper import (
    ConversionCancelled, ProcessHandle, convert_and_append_audio, get_tracks, remove_streams_in_place,
)
from job_queue import (
    CANCELLED, DONE, FAILED, KIND_LOSSY, KIND_M2TS, KIND_REMOVE, KIND_REPAIR, KIND_TRUEHD, PARTIAL, SKIPPED,
    JobResult, QueueJob, TrackNotFound, make_ref, ref_from_dict, ref_to_dict, resolve_ref,
)
from library_repair import (
    STATUS_CANCELLED, STATUS_FAILED, STATUS_PARTIAL, STATUS_REPAIRED, RepairContext, RepairOptions,
    RepairPlan, execute_plan, plan_repair,
)
from library_scanner import LibraryScanner, ScanCache
from media_inspector import ToolSet
from media_probe import format_seconds
from m2ts_remux import (
    M2TSRemuxer, OutputExists, RemuxError, RemuxOptions, VerificationFailed, default_output_path, unique_output,
)
from truehd_encoder import EncoderReport, TrueHDError
from tsmuxer_tool import TsMuxerError, TsMuxerInfo
from truehd_workflow import TrueHDConverter, TrueHDOptions

PREF_KEYS = ("keep_languages", "keep_forced", "keep_sdh", "remove_duplicate_subs")


# --------------------------------------------------------------------------
# Building jobs
# --------------------------------------------------------------------------

def repair_job(plan: RepairPlan, options: RepairOptions, config: ScannerConfig) -> QueueJob:
    """A Library Scanner repair. The plan is shown now and rebuilt when it runs."""
    summary = ", ".join(ACTION_LABELS[a] for a in plan.actions) or "repair"
    params = {"actions": list(plan.requested), "options": asdict(options),
              "prefs": {k: getattr(config, k) for k in PREF_KEYS}}
    description = plan.describe() + ["", "(The plan is checked again against the film just before it runs.)"]
    return QueueJob.for_file(KIND_REPAIR, plan.path, summary, description, params)


def _is_audio(track) -> bool:
    return hasattr(track, "channel_count")


def _refs(tracks, audio, subtitles) -> List[Dict]:
    return [ref_to_dict(make_ref(t, audio if _is_audio(t) else subtitles)) for t in tracks]


def _track_list(tracks, label) -> List[str]:
    return [f"      {label} #{t.stream_index}: " + str(t).replace(f"Stream #{t.stream_index}: ", "")
            for t in tracks]


def convert_job(path: str, audio: Sequence, subtitles: Sequence, source, codec: str, bitrate: str,
                remove_source: bool, removes: Sequence) -> QueueJob:
    """Convert File tab: AC-3 / E-AC-3 (the film is updated in place)."""
    removes = [t for t in removes if t.stream_index != source.stream_index]
    name = "E-AC-3" if codec == "eac3" else "AC-3"
    summary = f"{name} {bitrate} from #{source.stream_index}" + (" (original removed)" if remove_source else "")
    if removes:
        summary += f", remove {len(removes)} track(s)"
    description = [os.path.basename(path), f"  • Convert audio #{source.stream_index} to {name} {bitrate}: {source}",
                   f"  • {'Remove' if remove_source else 'Keep'} the original track"]
    if removes:
        description.append("  • Also remove:")
        description += _track_list(removes, "track")
    description.append("  • The film is updated in place after FFmpeg succeeds.")
    params = {"codec": codec, "bitrate": bitrate, "remove_source": remove_source,
              "source": ref_to_dict(make_ref(source, audio)),
              "removes": _refs(removes, audio, subtitles)}
    return QueueJob.for_file(KIND_LOSSY, path, summary, description, params)


def removal_job(path: str, audio: Sequence, subtitles: Sequence, removes: Sequence) -> QueueJob:
    a = [t for t in removes if _is_audio(t)]
    s = [t for t in removes if not _is_audio(t)]
    summary = "Remove " + " + ".join(p for p in (f"{len(a)} audio" if a else "", f"{len(s)} subtitle" if s else "")
                                     if p) + " track(s)"
    description = [os.path.basename(path), "  • Remove (nothing is converted):"]
    description += _track_list(removes, "track")
    params = {"removes": _refs(removes, audio, subtitles)}
    return QueueJob.for_file(KIND_REMOVE, path, summary, description, params)


def truehd_job(path: str, audio: Sequence, subtitles: Sequence, source, options: TrueHDOptions,
               removes: Sequence, target_label: str) -> QueueJob:
    opts = asdict(options)
    opts.pop("remove_streams", None)
    removes = [t for t in removes if t.stream_index != source.stream_index]
    if options.test_clip:
        output = f"{options.clip_seconds}-second test clip from {format_seconds(options.clip_start)} (new file)"
    elif options.replace_original:
        output = "replace the original after verification (no further question)"
    else:
        output = "new …_Sound_TrueHD.mkv"
    summary = f"TrueHD {target_label} from #{source.stream_index} → {output.split(' (')[0]}"
    description = [os.path.basename(path), f"  • Convert audio #{source.stream_index} to Dolby TrueHD {target_label}: {source}",
                   f"  • Output: {output}",
                   f"  • Keep original track: {'no' if options.remove_source else 'yes'}; "
                   f"TrueHD default: {'yes' if options.make_default else 'no'}; "
                   f"strict verification: {'yes' if options.strict_verify else 'no'}"]
    if removes:
        description.append("  • Leave out:")
        description += _track_list(removes, "track")
    params = {"options": opts, "source": ref_to_dict(make_ref(source, audio)),
              "removes": _refs(removes, audio, subtitles)}
    return QueueJob.for_file(KIND_TRUEHD, path, summary, description, params)


def m2ts_output_for(path: str, output_dir: Optional[str]) -> str:
    out = default_output_path(path)
    return os.path.join(output_dir, os.path.basename(out)) if output_dir else out


def m2ts_job(path: str, *, output: Optional[str] = None, output_dir: Optional[str] = None,
             on_exists: str = "skip", subtitle_mode: str = "preferred", audio: Optional[Sequence] = None,
             subtitles: Optional[Sequence] = None, all_audio: Sequence = (), all_subtitles: Sequence = (),
             first_audio=None, full_verify: bool = True, accept_metadata_risk: bool = False,
             config: Optional[ScannerConfig] = None, description: Optional[List[str]] = None) -> QueueJob:
    """MKV -> M2TS remux (a new file next to the film or in ``output_dir``).

    ``audio`` / ``subtitles`` (AudioStreamInfo / SubtitleStreamInfo) pin the
    exact tracks chosen in the remux dialog; None = all compatible audio and
    the subtitle policy ``subtitle_mode`` (preferred / none / all), worked
    out when the job runs. ``on_exists``: skip / rename / overwrite (only
    after the user confirmed replacing that file).
    """
    target = output or m2ts_output_for(path, output_dir)
    summary = f"M2TS → {os.path.basename(target) if on_exists != 'rename' else os.path.basename(target) + ' (new name if taken)'}"
    lines = description or [os.path.basename(path),
                            "  • Remux to M2TS with tsMuxeR (video and audio copied, nothing re-encoded)",
                            "  • Audio: " + ("as chosen" if audio is not None else "every compatible track"),
                            "  • Subtitles: " + ("as chosen" if subtitles is not None else
                                                 {"preferred": "compatible tracks in your kept languages",
                                                  "none": "none", "all": "every PGS track"}[subtitle_mode])]
    lines += [f"  • Output: {target}", f"  • If the output exists: {on_exists}",
              f"  • Whole-stream audio comparison: {'yes' if full_verify else 'no'}",
              "  • The MKV is never changed."]
    params = {"output": output, "output_dir": output_dir, "on_exists": on_exists, "subtitle_mode": subtitle_mode,
              "audio": _refs(audio, all_audio, all_subtitles) if audio is not None else None,
              "subtitles": _refs(subtitles, all_audio, all_subtitles) if subtitles is not None else None,
              "first_audio": ref_to_dict(make_ref(first_audio, all_audio)) if first_audio is not None else None,
              "full_verify": full_verify, "accept_metadata_risk": accept_metadata_risk,
              "prefs": {k: getattr(config, k) for k in PREF_KEYS} if config else {}}
    return QueueJob.for_file(KIND_M2TS, path, summary, lines, params)


# --------------------------------------------------------------------------
# Running jobs
# --------------------------------------------------------------------------

@dataclass
class QueueEnv:
    ffmpeg: str
    ffprobe: Optional[str]
    encoder: Callable[[], Optional[EncoderReport]]
    tools: ToolSet
    config: ScannerConfig
    cache: ScanCache
    log: Callable[[str], None] = print
    progress: Callable[[str, Optional[float], str], None] = lambda phase, fraction, detail: None
    handle: ProcessHandle = field(default_factory=ProcessHandle)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    tsmuxer: Callable[[], Optional[TsMuxerInfo]] = lambda: None

    def new_handle(self) -> ProcessHandle:
        """Process handle for the next job; already cancelled if the queue was cancelled."""
        self.handle = ProcessHandle()
        if self.cancel_event.is_set():
            self.handle.cancel()
        return self.handle

    def cancel(self) -> None:
        self.cancel_event.set()
        self.handle.cancel()


def _ffmpeg_progress(env: QueueEnv, phase: str):
    def report(p):
        detail = f"{format_seconds(p.elapsed)} elapsed"
        if p.eta is not None:
            detail += f" • ~{format_seconds(p.eta)} remaining"
        if p.write_rate:
            detail += f" • {p.write_rate / 1e6:.0f} MB/s"
        env.progress(phase, p.fraction, detail)
    return report


def _current_tracks(job: QueueJob, env: QueueEnv):
    audio, subtitles = get_tracks(job.path, env.ffmpeg, env.ffprobe)
    return audio, subtitles


def _resolve(job: QueueJob, env: QueueEnv, refs: Sequence[Dict]):
    """Current indices for the job's tracks; logs when the film was changed by an earlier job."""
    audio, subtitles = _current_tracks(job, env)
    unchanged = job.file_unchanged()
    if not unchanged:
        env.log("  The film changed since this job was queued (probably by an earlier job); "
                "finding its tracks again...")
    indices = []
    for data in refs:
        ref = ref_from_dict(data)
        pool = audio if ref.signature[0] == "audio" else subtitles
        index = resolve_ref(ref, pool, unchanged)
        if index != ref.index:
            env.log(f"  Track #{ref.index} is now #{index}: {ref.label}")
        indices.append(index)
    return audio, subtitles, indices


def _start(job: QueueJob, env: QueueEnv) -> Optional[JobResult]:
    env.log("========================================")
    env.log(f"Queue: {job.name} - {job.summary}")
    if not os.path.exists(job.path):
        return JobResult(SKIPPED, "the film no longer exists at this path")
    return None


def run_lossy(job: QueueJob, env: QueueEnv) -> JobResult:
    early = _start(job, env)
    if early:
        return early
    p = job.params
    try:
        audio, subtitles, indices = _resolve(job, env, [p["source"]] + p["removes"])
    except (TrackNotFound, RuntimeError) as e:
        return JobResult(SKIPPED, str(e))
    source, removes = indices[0], indices[1:]
    audio_ids = {a.stream_index for a in audio}
    other_audio = [i for i in removes if i in audio_ids]
    try:
        convert_and_append_audio(
            job.path, p["bitrate"], env.ffmpeg, source, len(audio), codec=p["codec"],
            remove_source_stream=p["remove_source"], progress_callback=env.log, handle=env.new_handle(),
            remove_streams=removes, removed_other_audio=len(other_audio),
            on_progress=_ffmpeg_progress(env, f"Converting {job.name}"))
    except ConversionCancelled:
        return JobResult(CANCELLED, "cancelled - the original was not changed")
    except RuntimeError as e:
        return JobResult(FAILED, str(e))
    return JobResult(DONE, "converted; film updated", job.path)


def run_remove(job: QueueJob, env: QueueEnv) -> JobResult:
    early = _start(job, env)
    if early:
        return early
    try:
        _, _, removes = _resolve(job, env, job.params["removes"])
    except (TrackNotFound, RuntimeError) as e:
        return JobResult(SKIPPED, str(e))
    try:
        remove_streams_in_place(job.path, env.ffmpeg, removes, progress_callback=env.log,
                                handle=env.new_handle(), on_progress=_ffmpeg_progress(env, f"Removing tracks {job.name}"))
    except ConversionCancelled:
        return JobResult(CANCELLED, "cancelled - the original was not changed")
    except RuntimeError as e:
        return JobResult(FAILED, str(e))
    return JobResult(DONE, f"removed {len(removes)} track(s)", job.path)


def run_truehd(job: QueueJob, env: QueueEnv) -> JobResult:
    early = _start(job, env)
    if early:
        return early
    report = env.encoder()
    if report is None or not report.path:
        return JobResult(SKIPPED, "the TrueHD encoder is not ready (check the Convert File tab)")
    if not env.ffprobe:
        return JobResult(SKIPPED, "TrueHD conversion needs ffprobe for verification")
    p = job.params
    try:
        _, _, indices = _resolve(job, env, [p["source"]] + p["removes"])
    except (TrackNotFound, RuntimeError) as e:
        return JobResult(SKIPPED, str(e))
    options = TrueHDOptions(**{**p["options"], "remove_streams": tuple(indices[1:])})
    converter = TrueHDConverter(encoder=report.path, ffprobe=env.ffprobe, reference_ffmpeg=env.ffmpeg,
                                report=report, log=env.log, progress=env.progress, handle=env.new_handle())
    try:
        result = converter.convert(job.path, indices[0], options)
        if result.awaiting_replace_confirmation:
            # Replacing was approved when the job was queued.
            try:
                final = converter.finalize_replace(result)
                return JobResult(DONE, "verified; original replaced" + _lossless_note(result), final)
            except OSError as e:
                final = converter.finalize_as_new(result)
                return JobResult(PARTIAL, f"verified, but the original could not be replaced ({e}); "
                                          f"saved as {os.path.basename(final)}", final)
        return JobResult(DONE, "verified; saved" + _lossless_note(result), result.final_path)
    except ConversionCancelled:
        return JobResult(CANCELLED, "cancelled - the original was not changed")
    except (TrueHDError, RuntimeError, OSError) as e:
        return JobResult(FAILED, str(e))


def _lossless_note(result) -> str:
    return {True: " (bit-identical)", False: " (NOT lossless)", None: " (lossless check off)"}[result.lossless]


_REPAIR_STATUS = {STATUS_REPAIRED: DONE, STATUS_PARTIAL: PARTIAL, STATUS_FAILED: FAILED,
                  STATUS_CANCELLED: CANCELLED}


def run_repair(job: QueueJob, env: QueueEnv) -> JobResult:
    early = _start(job, env)
    if early:
        return early
    p = job.params
    config = ScannerConfig(**{k: getattr(env.config, k) for k in ScannerConfig.__dataclass_fields__})
    for key, value in p.get("prefs", {}).items():
        setattr(config, key, value)
    options = RepairOptions(**p["options"])
    try:
        item = LibraryScanner(env.tools, config, env.cache).inspect_one(job.path)
        env.cache.save()
    except Exception as e:
        return JobResult(FAILED, f"could not analyse the film: {e}")
    plan = plan_repair(item, p["actions"], options, config, env.encoder())
    if plan.method is None:
        reasons = "; ".join(f"{ACTION_LABELS.get(a, a)}: {r}" for a, r in plan.skipped) or "nothing to do"
        env.log(f"  Nothing to do: {reasons}")
        return JobResult(SKIPPED, reasons)
    handle = env.new_handle()
    ctx = RepairContext(env.ffmpeg, env.ffprobe, env.encoder(), options, handle=handle, log=env.log,
                        progress=env.progress)
    outcome = execute_plan(plan, ctx)
    message = outcome.message
    if outcome.skipped:
        message += "\n" + "\n".join(f"skipped {ACTION_LABELS.get(a, a)}: {r}" for a, r in outcome.skipped)
    return JobResult(_REPAIR_STATUS.get(outcome.status, SKIPPED), message, outcome.final_path)


def run_m2ts(job: QueueJob, env: QueueEnv) -> JobResult:
    early = _start(job, env)
    if early:
        return early
    info = env.tsmuxer()
    if info is None or not info.ok:
        return JobResult(SKIPPED, (info.message if info else "tsMuxeR is not configured")
                         + " (set it up in the M2TS remux window)")
    if not env.ffprobe:
        return JobResult(SKIPPED, "M2TS remuxing needs ffprobe for verification")
    p = job.params
    config = ScannerConfig(**{k: getattr(env.config, k) for k in ScannerConfig.__dataclass_fields__})
    for key, value in (p.get("prefs") or {}).items():
        setattr(config, key, value)
    target = p.get("output") or m2ts_output_for(job.path, p.get("output_dir"))
    on_exists = p.get("on_exists", "skip")
    if os.path.exists(target):
        if on_exists == "skip":
            return JobResult(SKIPPED, f"{os.path.basename(target)} already exists")
        if on_exists == "rename":
            target = unique_output(target)
    handle = env.new_handle()
    remuxer = M2TSRemuxer(info, env.ffmpeg, env.ffprobe, env.tools, env.log, env.progress, handle)
    try:
        plan = remuxer.analyse(job.path, target, config, p.get("subtitle_mode", "preferred"))
        if p.get("audio") is not None or p.get("subtitles") is not None or p.get("first_audio"):
            refs = (p.get("audio") or []) + (p.get("subtitles") or [])
            first = [p["first_audio"]] if p.get("first_audio") else []
            _, _, indices = _resolve(job, env, refs + first)
            chosen = set(indices[:len(refs)])
            for t in plan.audio if p.get("audio") is not None else []:
                t.selected = t.index in chosen
            for t in plan.subtitles if p.get("subtitles") is not None else []:
                t.selected = t.index in chosen
            if first:
                plan.first_audio = indices[-1]
        if plan.metadata_warnings and not p.get("accept_metadata_risk"):
            return JobResult(SKIPPED, "Dolby Vision / HDR10+ metadata may change in M2TS; queue it again and "
                                      "confirm the compatibility-test warning")
        result = remuxer.remux(plan, RemuxOptions(overwrite=on_exists == "overwrite",
                                                  accept_metadata_risk=bool(p.get("accept_metadata_risk")),
                                                  full_audio_compare=bool(p.get("full_verify", True))))
    except ConversionCancelled:
        return JobResult(CANCELLED, "cancelled - the MKV was not changed; the partial M2TS was deleted")
    except TrackNotFound as e:
        return JobResult(SKIPPED, str(e))
    except OutputExists as e:
        return JobResult(SKIPPED, str(e))
    except VerificationFailed as e:
        return JobResult(FAILED, f"{e}\n\n{e.report}")
    except (RemuxError, TsMuxerError, RuntimeError, OSError) as e:
        return JobResult(FAILED, str(e))
    env.log(result.report)
    return JobResult(DONE, "; ".join(result.outcomes) + "\n\n" + result.report, result.output)


def make_executors(env: QueueEnv) -> Dict[str, Callable[[QueueJob], JobResult]]:
    return {
        KIND_REPAIR: lambda job: run_repair(job, env),
        KIND_LOSSY: lambda job: run_lossy(job, env),
        KIND_REMOVE: lambda job: run_remove(job, env),
        KIND_TRUEHD: lambda job: run_truehd(job, env),
        KIND_M2TS: lambda job: run_m2ts(job, env),
    }
