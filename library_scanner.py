"""Recursive, read-only library scanning with a result cache.

The scanner never writes to media files. Reports are cached in the per-user
data folder keyed by absolute path and validated by file size + mtime (and
the inspector version), so a re-scan only inspects new or changed files.
Findings are always recomputed from the cached report, so editing the rules
takes effect without re-reading the films.
"""
import json
import os
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from app_paths import user_data_dir
from compatibility_rules import (
    ScannerConfig, Finding, evaluate, issue_findings, recommended_actions, worst_severity,
)
from media_inspector import (
    INSPECTOR_VERSION, SUPPORTED_EXTENSIONS, InspectOptions, InspectionCancelled, MediaReport, ToolSet,
    inspect_file,
)

# Temporary files written beside films by DTS2DD itself.
_TEMP_MARKERS = (".truehd-partial-", ".converting-")


def is_candidate(name: str) -> bool:
    if name.startswith(".") or any(m in name for m in _TEMP_MARKERS):
        return False
    return name.lower().endswith(SUPPORTED_EXTENSIONS)


def find_media_files(root: str, cancel: Optional[threading.Event] = None) -> List[str]:
    """All MKV/MP4 files below ``root`` (hidden folders and DTS2DD temp files skipped)."""
    found = []
    for folder, dirs, files in os.walk(root):
        if cancel is not None and cancel.is_set():
            break
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not d.startswith("@eaDir"))
        for name in sorted(files):
            if is_candidate(name):
                found.append(os.path.abspath(os.path.join(folder, name)))
    return found


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def default_cache_path() -> Path:
    return user_data_dir() / "library_scan_cache.json"


class ScanCache:
    """JSON cache of MediaReports, safe to use from several threads."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_cache_path()
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                self._entries = data["entries"]
        except (OSError, ValueError):
            self._entries = {}

    def get(self, path: str, size: int, mtime_ns: int) -> Optional[MediaReport]:
        with self._lock:
            entry = self._entries.get(os.path.abspath(path))
        if not entry:
            return None
        if (entry.get("size") != size or entry.get("mtime_ns") != mtime_ns
                or entry.get("inspector_version") != INSPECTOR_VERSION):
            return None
        try:
            return MediaReport.from_dict(entry)
        except (TypeError, ValueError, KeyError):
            return None

    def is_fresh(self, path: str) -> bool:
        try:
            st = os.stat(path)
        except OSError:
            return False
        return self.get(path, st.st_size, st.st_mtime_ns) is not None

    def put(self, report: MediaReport) -> None:
        if report.errors:
            return          # retry unreadable files next time
        with self._lock:
            self._entries[os.path.abspath(report.path)] = report.to_dict()
            self._dirty = True

    def forget(self, path: str) -> None:
        with self._lock:
            if self._entries.pop(os.path.abspath(path), None) is not None:
                self._dirty = True

    def __len__(self) -> int:
        return len(self._entries)

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            data = json.dumps({"version": 1, "entries": self._entries})
            self._dirty = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".scan-cache-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass
class ScanItem:
    report: MediaReport
    findings: List[Finding] = field(default_factory=list)
    from_cache: bool = False
    repair_status: str = ""

    @property
    def path(self) -> str:
        return self.report.path

    @property
    def severity(self) -> Optional[str]:
        return worst_severity(self.findings)

    @property
    def issues(self) -> List[Finding]:
        return issue_findings(self.findings)

    @property
    def recommended(self) -> List[str]:
        return recommended_actions(self.findings)

    def refresh(self, config: ScannerConfig) -> None:
        self.findings = evaluate(self.report, config)


@dataclass
class ScanProgress:
    total: int
    done: int
    current: str
    issues: int
    cached: int
    errors: int
    elapsed: float
    eta: Optional[float]


@dataclass
class ScanSummary:
    root: str
    total: int = 0
    scanned: int = 0
    cached: int = 0
    errors: int = 0
    cancelled: bool = False
    elapsed: float = 0.0


class LibraryScanner:
    """Inspect every candidate file below a folder, using the cache."""

    def __init__(self, tools: ToolSet, config: ScannerConfig, cache: Optional[ScanCache] = None,
                 inspector: Callable = inspect_file):
        self.tools = tools
        self.config = config
        self.cache = cache if cache is not None else ScanCache()
        self.inspector = inspector

    def options(self) -> InspectOptions:
        return InspectOptions(bitstream=self.config.bitstream_analysis,
                              video_frames=self.config.video_frame_check)

    def inspect_one(self, path: str, cancel: Optional[threading.Event] = None,
                    use_cache: bool = True) -> ScanItem:
        st = os.stat(path)
        report = self.cache.get(path, st.st_size, st.st_mtime_ns) if use_cache else None
        from_cache = report is not None
        if report is None:
            report = self.inspector(path, self.tools, self.options(), cancel)
            self.cache.put(report)
        item = ScanItem(report=report, from_cache=from_cache)
        item.refresh(self.config)
        return item

    def scan(self, root: str, on_item: Callable[[ScanItem], None],
             on_progress: Optional[Callable[[ScanProgress], None]] = None,
             cancel: Optional[threading.Event] = None, force: bool = False,
             files: Optional[Iterable[str]] = None) -> ScanSummary:
        cancel = cancel or threading.Event()
        started = time.monotonic()
        summary = ScanSummary(root=root)
        if on_progress:
            on_progress(ScanProgress(0, 0, "Finding films...", 0, 0, 0, 0.0, None))
        paths = list(files) if files is not None else find_media_files(root, cancel)
        summary.total = len(paths)

        # Split cached and new work up front so the ETA only counts real work.
        pending_new = sum(1 for p in paths if force or not self.cache.is_fresh(p))
        lock = threading.Lock()
        state = {"done": 0, "issues": 0, "new_done": 0, "new_time": 0.0}

        def work(path: str) -> Optional[ScanItem]:
            if cancel.is_set():
                return None
            t0 = time.monotonic()
            try:
                item = self.inspect_one(path, cancel, use_cache=not force)
            except InspectionCancelled:
                return None
            except OSError as e:        # vanished / unreadable file
                report = MediaReport(path=path, size=0, mtime_ns=0, errors=[f"Cannot read file: {e}"])
                item = ScanItem(report=report)
                item.refresh(self.config)
            except Exception as e:      # never let one film stop the scan
                report = MediaReport(path=path, size=0, mtime_ns=0, errors=[f"Inspection failed: {e}"])
                item = ScanItem(report=report)
                item.refresh(self.config)
            if not item.from_cache:
                with lock:
                    state["new_done"] += 1
                    state["new_time"] += time.monotonic() - t0
            return item

        def progress(current: str) -> None:
            if not on_progress:
                return
            with lock:
                remaining_new = max(0, pending_new - state["new_done"])
                eta = None
                if state["new_done"]:
                    per_file = state["new_time"] / state["new_done"] / max(1, self.config.workers)
                    eta = per_file * remaining_new
                elif pending_new == 0:
                    eta = 0.0
                on_progress(ScanProgress(summary.total, state["done"], current, state["issues"],
                                         summary.cached, summary.errors, time.monotonic() - started, eta))

        workers = max(1, self.config.workers)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as pool:
            queue = iter(paths)
            running = {}
            for _ in range(workers):
                path = next(queue, None)
                if path is None:
                    break
                running[pool.submit(work, path)] = path
            progress(os.path.basename(paths[0]) if paths else "")
            while running:
                finished, _ = wait(list(running), timeout=0.5, return_when=FIRST_COMPLETED)
                for future in finished:
                    path = running.pop(future)
                    item = future.result()
                    if item is not None:
                        with lock:
                            state["done"] += 1
                            if item.issues:
                                state["issues"] += 1
                        if item.from_cache:
                            summary.cached += 1
                        else:
                            summary.scanned += 1
                        if item.report.errors:
                            summary.errors += 1
                        on_item(item)
                    if not cancel.is_set():
                        nxt = next(queue, None)
                        if nxt is not None:
                            running[pool.submit(work, nxt)] = nxt
                current = ", ".join(os.path.basename(p) for p in running.values())
                progress(current)
        self.cache.save()
        summary.cancelled = cancel.is_set()
        summary.elapsed = time.monotonic() - started
        return summary
