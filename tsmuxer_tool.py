"""tsMuxeR integration: discovery, version check, track detection and muxing.

Only the documented command line is used (justdan96/tsMuxer docs/USAGE.md):

    tsMuxeR <media file>                    -> prints the tracks it can read
    tsMuxeR <file.meta> <out.m2ts>          -> muxes; M2TS mode is chosen by
                                               the output extension

Facts taken from the tsMuxer 2.7.0 source (the last official release, April
2025) that this module relies on:
  * Track detection prints blocks of "Track ID:", "Stream type:",
    "Stream ID:" (the codec name for the meta file), "Stream info:",
    "Stream lang:" and, only when non-zero, "Stream delay:" (ms). A track it
    cannot read prints "Can't detect stream type". MKV track IDs are the
    track's position in the MKV (1-based).
  * Progress is printed as "<n.n>% complete" lines; success ends with
    "Mux successful complete". Exit codes: 0 ok, -1..-4 errors.
  * A TrueHD track without an interleaved AC-3 core (the normal case in MKV)
    is detected as "A_MLP" and copied unchanged as M2TS stream type 0x83
    ("TRUE-HD + ATMOS" in the info when 4 substreams are present). tsMuxeR
    does not create an AC-3 core; with "A_AC3" + TrueHD it keeps an existing
    one.
  * HEVC: in non-Blu-ray mode a "DOVI" descriptor is written when Dolby
    Vision RPU NAL units are found; the profile is derived from the stream
    (PQ single layer -> 8.1). HDR10+ / HDR10 SEI messages stay in the video
    bitstream.
  * SRT (S_TEXT/UTF8) is rendered to PGS bitmaps; ASS/SSA, VobSub, FLAC,
    Opus and Vorbis are not supported.

The Windows installer ships the official tsMuxeR 2.7.0 in a ``tsmuxer``
folder beside the app (CI downloads it pinned by SHA-256). The app itself
never downloads anything. Nothing here writes to the source file.
"""
import collections
import glob
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from app_paths import app_dir, find_tool, user_data_dir
from ffmpeg_helper import ConversionCancelled, ProcessHandle
from media_probe import subprocess_flags

SETTING_KEY = "tsmuxer_path"
OFFICIAL_RELEASES_URL = "https://github.com/justdan96/tsMuxer/releases"
# 2.6.16 added Dolby Vision descriptors, HDR10+ fixes and MKV audio delays.
MIN_VERSION = (2, 6, 16)

STATUS_READY = "tsMuxeR ready"
STATUS_OLD = "tsMuxeR too old"
STATUS_MISSING = "tsMuxeR not found"
STATUS_BROKEN = "tsMuxeR not working"

TOOL_NAMES = ("tsMuxeR", "tsmuxer")

# Documented global options; the same defaults the tsMuxeR GUI uses for M2TS.
MUXOPT = "MUXOPT --new-audio-pes --vbr --vbv-len=500"


class TsMuxerError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Discovery and version check
# --------------------------------------------------------------------------

@dataclass
class TsMuxerInfo:
    path: Optional[str]
    origin: str = ""
    version: Optional[Tuple[int, int, int]] = None
    version_text: str = ""
    status: str = STATUS_MISSING
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_READY and bool(self.path)

    @property
    def version_label(self) -> str:
        return ".".join(map(str, self.version)) if self.version else (self.version_text or "unknown")


def _system_candidates() -> List[Tuple[str, str]]:
    found = []
    for name in TOOL_NAMES:
        on_path = shutil.which(name)
        if on_path:
            found.append((on_path, "PATH"))
    system = platform.system()
    if system == "Windows":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            for folder in ("tsMuxeR", "tsMuxer"):
                found.append((os.path.join(base, folder, "tsMuxeR.exe"), "Program Files"))
    elif system == "Darwin":
        for app in sorted(glob.glob("/Applications/tsMuxer*.app/Contents/MacOS/tsMuxeR"), reverse=True):
            found.append((app, "Applications"))
        found += [("/opt/homebrew/bin/tsMuxeR", "Homebrew"), ("/usr/local/bin/tsMuxeR", "/usr/local")]
    else:
        found += [("/usr/bin/tsMuxeR", "/usr/bin"), ("/usr/local/bin/tsMuxeR", "/usr/local")]
    return found


def bundled_path() -> Optional[str]:
    for name in TOOL_NAMES:
        tool = find_tool(name, [app_dir() / "tsmuxer"])
        if tool:
            return str(tool)
    return None


def candidates(configured: Optional[str] = None) -> List[Tuple[str, str]]:
    """(path, origin) in search order: configured, beside the app, per-user folder, system."""
    result = []
    if configured:
        result.append((configured, "configured"))
    for folder, origin in ((app_dir() / "tsmuxer", "bundled"), (user_data_dir() / "tsmuxer", "user folder")):
        for name in TOOL_NAMES:
            tool = find_tool(name, [folder])
            if tool:
                result.append((str(tool), origin))
                break
    result += _system_candidates()
    unique, seen = [], set()
    for path, origin in result:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append((path, origin))
    return unique


def find_tsmuxer(configured: Optional[str] = None) -> Tuple[Optional[str], str]:
    for path, origin in candidates(configured):
        if os.path.isfile(path):
            return path, origin
    return None, ""


_VERSION_RE = re.compile(r"tsMuxeR\s+version\s+(\S+)", re.IGNORECASE)


def parse_version(text: str) -> Tuple[Optional[Tuple[int, int, int]], str]:
    """(numeric version or None, raw version text) from the tsMuxeR banner."""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None, ""
    raw = m.group(1).rstrip(".")
    nums = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?", raw)
    if not nums:
        return None, raw
    return (int(nums.group(1)), int(nums.group(2)), int(nums.group(3) or 0)), raw


def _run(cmd: Sequence[str], timeout: float, cwd: Optional[str] = None) -> Tuple[int, str]:
    result = subprocess.run(list(cmd), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                            cwd=cwd, creationflags=subprocess_flags())
    out = result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")
    return result.returncode, out


def check_tsmuxer(configured: Optional[str] = None,
                  runner: Callable[[Sequence[str], float], Tuple[int, str]] = None) -> TsMuxerInfo:
    """Find tsMuxeR and check that it runs and is new enough."""
    path, origin = find_tsmuxer(configured)
    if configured and not os.path.isfile(configured):
        note = f"The configured tsMuxeR ({configured}) does not exist. "
    else:
        note = ""
    if not path:
        if getattr(sys, "frozen", False) and platform.system() == "Windows":
            hint = ("The tsMuxeR included with DTS2DD is missing from the program folder "
                    f"({app_dir() / 'tsmuxer'}); reinstall DTS2DD to restore it.")
        else:
            hint = (f"tsMuxeR was not found. For a source / macOS run, get it from {OFFICIAL_RELEASES_URL}, "
                    "put it in a 'tsmuxer' folder beside the app or choose it with Browse.")
        return TsMuxerInfo(None, status=STATUS_MISSING, message=note + hint)
    runner = runner or (lambda cmd, timeout: _run(cmd, timeout))
    try:
        code, out = runner([path], 20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return TsMuxerInfo(path, origin, status=STATUS_BROKEN, message=f"{note}Could not run {path}: {e}")
    version, raw = parse_version(out)
    if not raw:
        return TsMuxerInfo(path, origin, status=STATUS_BROKEN,
                           message=f"{note}{Path(path).name} did not print a tsMuxeR version banner; "
                                   "it does not look like tsMuxeR.")
    info = TsMuxerInfo(path, origin, version, raw)
    if version is None:
        info.status = STATUS_READY
        info.message = (f"{note}tsMuxeR {raw} (version format not recognised - assumed to be a development "
                        "build; results are verified after every remux).")
    elif version < MIN_VERSION:
        info.status = STATUS_OLD
        info.message = (f"{note}tsMuxeR {raw} is older than {'.'.join(map(str, MIN_VERSION))}, which added "
                        "Dolby Vision descriptors, HDR10+ fixes and MKV audio delays. Install the current "
                        f"release from {OFFICIAL_RELEASES_URL}.")
    else:
        info.status = STATUS_READY
        info.message = f"{note}tsMuxeR {raw} ({origin})."
    return info


# --------------------------------------------------------------------------
# Track detection
# --------------------------------------------------------------------------

@dataclass
class TsTrack:
    track_id: int
    stream_type: str = ""        # display name, e.g. "TRUE-HD", "HEVC", "PGS"
    codec_id: str = ""           # meta-file codec name, e.g. "A_MLP"; "" when undetected
    info: str = ""
    lang: str = ""
    delay_ms: int = 0
    detected: bool = True

    @property
    def atmos(self) -> bool:
        return "ATMOS" in self.info.upper()

    @property
    def kind(self) -> str:
        return {"V": "video", "A": "audio", "S": "subtitle"}.get(self.codec_id[:1], "unknown")


@dataclass
class TsDetection:
    tracks: List[TsTrack] = field(default_factory=list)
    duration: Optional[float] = None
    raw: str = ""

    def track(self, track_id: int) -> Optional[TsTrack]:
        return next((t for t in self.tracks if t.track_id == track_id), None)


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean_line(line: str) -> str:
    return _ANSI.sub("", line).replace("\r", "").strip()


def parse_time(text: str) -> Optional[float]:
    m = re.match(r"(\d+):(\d{2}):(\d{2})(?:[.,](\d+))?", text.strip())
    if not m:
        return None
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + (int(frac) / 10 ** len(frac) if frac else 0.0)


def parse_detection(output: str) -> TsDetection:
    det = TsDetection(raw=output)
    current: Optional[TsTrack] = None
    for raw_line in output.splitlines():
        line = clean_line(raw_line)
        key, sep, value = line.partition(":")
        value = value.strip()
        if not sep and line.startswith("Can't detect stream type"):
            if current is not None:
                current.detected = False
            continue
        if key == "Track ID":
            try:
                current = TsTrack(int(value))
            except ValueError:
                current = None
                continue
            det.tracks.append(current)
        elif key == "Duration":
            det.duration = parse_time(value)
        elif current is None:
            continue
        elif key == "Stream type":
            current.stream_type = value
        elif key == "Stream ID":
            current.codec_id = value
        elif key == "Stream info":
            current.info = value
        elif key == "Stream lang":
            current.lang = value
        elif key == "Stream delay":
            try:
                current.delay_ms = int(float(value))
            except ValueError:
                pass
    for t in det.tracks:
        if not t.codec_id:
            t.detected = False
    return det


def detect_tracks(tsmuxer: str, path: str, timeout: float = 300,
                  runner: Callable[[Sequence[str], float], Tuple[int, str]] = None) -> TsDetection:
    runner = runner or (lambda cmd, t: _run(cmd, t))
    try:
        code, out = runner([tsmuxer, path], timeout)
    except subprocess.TimeoutExpired:
        raise TsMuxerError("tsMuxeR timed out while reading the tracks")
    except OSError as e:
        raise TsMuxerError(f"Could not run tsMuxeR: {e}")
    det = parse_detection(out)
    if not det.tracks:
        last = [clean_line(l) for l in out.splitlines() if clean_line(l)][-3:]
        raise TsMuxerError("tsMuxeR found no tracks in this file" + (": " + " / ".join(last) if last else ""))
    return det


# --------------------------------------------------------------------------
# Meta file
# --------------------------------------------------------------------------

@dataclass
class MetaTrack:
    codec_id: str
    track_id: int
    lang: Optional[str] = None
    options: List[Tuple[str, str]] = field(default_factory=list)   # extra name=value pairs in order


def _check_path(path: str) -> None:
    if '"' in path or "\n" in path or "\r" in path:
        raise TsMuxerError(f"The path contains a character tsMuxeR meta files cannot hold: {path}")


def meta_value(value: str) -> str:
    return f'"{value}"' if re.search(r"[\s,]", value) else value


def build_meta(source_path: str, tracks: Sequence[MetaTrack], muxopt: str = MUXOPT) -> str:
    """Text of a .meta file muxing ``tracks`` of ``source_path`` (in this order)."""
    _check_path(source_path)
    lines = [muxopt]
    for t in tracks:
        parts = [t.codec_id, f'"{source_path}"']
        parts += [f"{name}={meta_value(value)}" if value != "" else name for name, value in t.options]
        parts.append(f"track={t.track_id}")
        lang = (t.lang or "").strip().lower()
        if re.fullmatch(r"[a-z]{3}", lang) and lang != "und":
            parts.append(f"lang={lang}")
        lines.append(", ".join(parts))
    return "\n".join(lines) + "\n"


def fps_value(rate: Optional[str]) -> Optional[str]:
    """tsMuxeR fps text for an ffprobe frame rate ('24000/1001' -> '23.976'); None if unusual."""
    if not rate or "/" not in rate:
        return None
    try:
        num, den = (int(x) for x in rate.split("/"))
    except ValueError:
        return None
    if not den:
        return None
    exact = {(24000, 1001): "23.976", (30000, 1001): "29.97", (60000, 1001): "59.94"}
    if (num, den) in exact:
        return exact[(num, den)]
    value = num / den
    for standard in (24, 25, 30, 48, 50, 60):
        if abs(value - standard) < 1e-6:
            return str(standard)
    return None


def write_meta(text: str) -> str:
    """Write a meta file (UTF-8, as documented) in a fresh temporary folder."""
    folder = tempfile.mkdtemp(prefix="dts2dd-tsmuxer-")
    path = os.path.join(folder, "remux.meta")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


# --------------------------------------------------------------------------
# Running tsMuxeR
# --------------------------------------------------------------------------

@dataclass
class MuxProgress:
    percent: float
    elapsed: float

    @property
    def fraction(self) -> float:
        return max(0.0, min(1.0, self.percent / 100.0))

    @property
    def eta(self) -> Optional[float]:
        f = self.fraction
        if f <= 0.005:
            return None
        return self.elapsed * (1 - f) / f


@dataclass
class MuxResult:
    returncode: int
    success_message: bool
    lines: List[str]

    def error_summary(self, max_lines: int = 8) -> str:
        useful = [l for l in self.lines if l and not _PROGRESS_RE.search(l)]
        return "\n".join(useful[-max_lines:]) or f"exit code {self.returncode}"


_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)%\s+complete")
SUCCESS_TEXT = "Mux successful complete"


def run_tsmuxer(tsmuxer: str, meta_path: str, output_path: str,
                on_progress: Optional[Callable[[MuxProgress], None]] = None,
                handle: Optional[ProcessHandle] = None,
                keep_lines: int = 300) -> MuxResult:
    """Run ``tsMuxeR meta output``; stdout is parsed for progress, stderr kept for errors."""
    _check_path(output_path)
    lines = collections.deque(maxlen=keep_lines)
    started = time.monotonic()
    try:
        proc = subprocess.Popen([tsmuxer, meta_path, output_path], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=os.path.dirname(meta_path) or None, creationflags=subprocess_flags())
    except OSError as e:
        raise TsMuxerError(f"Could not start tsMuxeR: {e}")
    if handle:
        handle.attach(proc)
    success = False

    def drain_stderr():
        for raw in proc.stderr:
            lines.append(clean_line(raw.decode("utf-8", "replace")))

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()
    try:
        for raw in proc.stdout:
            text = raw.decode("utf-8", "replace")
            # The progress line may be preceded by a cursor-up escape on POSIX.
            for part in re.split(r"[\r\n]", text):
                line = clean_line(part)
                if not line:
                    continue
                m = _PROGRESS_RE.search(line)
                if m:
                    if on_progress:
                        on_progress(MuxProgress(float(m.group(1)), time.monotonic() - started))
                    continue
                if SUCCESS_TEXT in line:
                    success = True
                lines.append(line)
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t.join(timeout=5)
        if handle:
            handle.detach()
    if handle and handle.cancelled.is_set():
        raise ConversionCancelled("Remux cancelled by user")
    return MuxResult(proc.returncode, success, list(lines))
