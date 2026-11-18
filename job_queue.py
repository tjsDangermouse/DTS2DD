"""A persistent queue of jobs, run one after another.

Jobs come from the Convert File tab (AC-3 / E-AC-3 / TrueHD conversion,
track removal) and from the Library Scanner (repairs). Each job records the
film's size and modification time when it was queued. Because an earlier job
in the queue may change the same film, every job re-reads the film just
before it runs:

  * Library Scanner repairs are re-planned from what was asked for
    ("clean subtitles", "FLAC -> TrueHD", ...), so they always use the
    film's current tracks.
  * Convert File jobs refer to concrete tracks. They are found again by a
    signature (type, codec, profile, channels, layout, rate, language, title,
    forced) and their position among identical tracks. If that is
    ambiguous or the track has gone, the job is skipped - never guessed.

The queue is stored as JSON in the per-user data folder, so it survives
restarting the app. A job that was running when the app closed is marked
"Interrupted" (its original film is unchanged) and can be retried.
"""
import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from app_paths import user_data_dir

QUEUED = "Queued"
RUNNING = "Running"
DONE = "Done"
PARTIAL = "Done (some steps skipped)"
FAILED = "Failed"
SKIPPED = "Skipped"
CANCELLED = "Cancelled"
INTERRUPTED = "Interrupted"
FINISHED = (DONE, PARTIAL, FAILED, SKIPPED, CANCELLED, INTERRUPTED)

KIND_REPAIR = "repair"      # Library Scanner repair (re-planned when it runs)
KIND_LOSSY = "lossy"        # Convert File: AC-3 / E-AC-3
KIND_TRUEHD = "truehd"      # Convert File: Dolby TrueHD
KIND_REMOVE = "remove"      # Convert File: remove ticked tracks
KIND_LABELS = {KIND_REPAIR: "Library repair", KIND_LOSSY: "Convert", KIND_TRUEHD: "TrueHD",
               KIND_REMOVE: "Remove tracks"}


# --------------------------------------------------------------------------
# Track references that survive changes made by earlier jobs
# --------------------------------------------------------------------------

def track_signature(track) -> List:
    """Signature of an AudioStreamInfo or SubtitleStreamInfo."""
    if hasattr(track, "channel_count"):         # audio
        return ["audio", (track.codec_name or "").lower(), track.profile, track.channel_count,
                track.channel_layout, track.sample_rate, track.language, track.title, bool(track.is_forced)]
    return ["subtitle", (track.codec_name or "").lower(), None, None, None, None,
            track.language, track.title, bool(track.is_forced)]


@dataclass
class TrackRef:
    index: int                 # absolute stream index when queued
    signature: List
    occurrence: int            # position among tracks with the same signature
    count: int                 # how many tracks had this signature
    label: str = ""


def make_ref(track, all_tracks: Sequence) -> TrackRef:
    sig = track_signature(track)
    same = [t for t in all_tracks if track_signature(t) == sig]
    occurrence = next(i for i, t in enumerate(same) if t.stream_index == track.stream_index)
    label = str(track)
    prefix = f"Stream #{track.stream_index}: "
    return TrackRef(track.stream_index, sig, occurrence, len(same),
                    label[len(prefix):] if label.startswith(prefix) else label)


class TrackNotFound(RuntimeError):
    pass


def resolve_ref(ref: TrackRef, tracks: Sequence, unchanged: bool) -> int:
    """Current stream index for ``ref`` (raises TrackNotFound when unsure)."""
    if unchanged:
        match = next((t for t in tracks if t.stream_index == ref.index), None)
        if match is not None and track_signature(match) == ref.signature:
            return ref.index
    same = [t for t in tracks if track_signature(t) == ref.signature]
    if not same:
        raise TrackNotFound(f"track no longer present: {ref.label}")
    if len(same) != ref.count:
        raise TrackNotFound(f"cannot tell which track is meant any more ({len(same)} identical tracks, "
                            f"{ref.count} when queued): {ref.label}")
    return same[ref.occurrence].stream_index


def ref_to_dict(ref: TrackRef) -> Dict:
    return asdict(ref)


def ref_from_dict(data: Dict) -> TrackRef:
    return TrackRef(**data)


# --------------------------------------------------------------------------
# Jobs and the queue
# --------------------------------------------------------------------------

@dataclass
class QueueJob:
    kind: str
    path: str
    summary: str                         # one line for the table
    description: List[str]               # full plan shown in the details panel
    params: Dict                         # kind-specific, JSON-safe
    size: int = 0
    mtime_ns: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = QUEUED
    message: str = ""
    final_path: Optional[str] = None
    added: float = field(default_factory=time.time)
    finished: Optional[float] = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @classmethod
    def for_file(cls, kind: str, path: str, summary: str, description: List[str], params: Dict) -> "QueueJob":
        path = os.path.abspath(path)
        st = os.stat(path)
        return cls(kind, path, summary, list(description), params, st.st_size, st.st_mtime_ns)

    def file_unchanged(self) -> bool:
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        return st.st_size == self.size and st.st_mtime_ns == self.mtime_ns


def same_path(a: str, b: str) -> bool:
    """True when two paths name the same file (Windows: case-insensitive, either slash)."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def default_queue_path() -> Path:
    return user_data_dir() / "job_queue.json"


class JobQueue:
    """Ordered list of jobs, saved after every change. Thread-safe."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_queue_path()
        self._lock = threading.RLock()
        self.jobs: List[QueueJob] = []
        self._load()

    # ---- persistence ----------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for item in data.get("jobs", []) if isinstance(data, dict) else []:
            try:
                job = QueueJob(**{k: v for k, v in item.items() if k in QueueJob.__dataclass_fields__})
            except TypeError:
                continue
            if job.status == RUNNING:
                job.status = INTERRUPTED
                job.message = "the app closed while this job was running; the original was not changed"
            self.jobs.append(job)

    def save(self) -> None:
        with self._lock:
            data = json.dumps({"version": 1, "jobs": [asdict(j) for j in self.jobs]}, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".queue-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError:
            pass            # the queue still works in memory

    # ---- editing --------------------------------------------------------

    def get(self, job_id: str) -> Optional[QueueJob]:
        with self._lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def add(self, job: QueueJob) -> QueueJob:
        with self._lock:
            self.jobs.append(job)
        self.save()
        return job

    def remove(self, job_ids: Sequence[str]) -> int:
        """Remove jobs that are not running; returns how many were removed."""
        ids = set(job_ids)
        with self._lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j.id not in ids or j.status == RUNNING]
            removed = before - len(self.jobs)
        self.save()
        return removed

    def move(self, job_ids: Sequence[str], delta: int) -> None:
        """Move the given jobs up (delta < 0) or down, keeping their order."""
        ids = set(job_ids)
        with self._lock:
            order = list(self.jobs)
            positions = [i for i, j in enumerate(order) if j.id in ids]
            if delta < 0:
                for i in positions:
                    if i > 0 and order[i - 1].id not in ids:
                        order[i - 1], order[i] = order[i], order[i - 1]
            else:
                for i in reversed(positions):
                    if i < len(order) - 1 and order[i + 1].id not in ids:
                        order[i + 1], order[i] = order[i], order[i + 1]
            self.jobs = order
        self.save()

    def retry(self, job_ids: Sequence[str]) -> int:
        n = 0
        with self._lock:
            for j in self.jobs:
                if j.id in job_ids and j.status in FINISHED and j.status != DONE:
                    j.status, j.message, j.finished = QUEUED, "", None
                    n += 1
        self.save()
        return n

    def clear_finished(self) -> int:
        with self._lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j.status not in (DONE, PARTIAL)]
            removed = before - len(self.jobs)
        self.save()
        return removed

    def pending(self) -> List[QueueJob]:
        with self._lock:
            return [j for j in self.jobs if j.status == QUEUED]

    def jobs_for(self, path: str) -> List[QueueJob]:
        with self._lock:
            return [j for j in self.jobs if same_path(j.path, path) and j.status in (QUEUED, RUNNING)]

    def next_pending(self) -> Optional[QueueJob]:
        with self._lock:
            return next((j for j in self.jobs if j.status == QUEUED), None)

    def set_status(self, job: QueueJob, status: str, message: str = "",
                   final_path: Optional[str] = None) -> None:
        with self._lock:
            job.status = status
            job.message = message
            if final_path:
                job.final_path = final_path
            if status in FINISHED:
                job.finished = time.time()
        self.save()


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

@dataclass
class JobResult:
    status: str
    message: str = ""
    final_path: Optional[str] = None


Executor = Callable[[QueueJob], JobResult]


class QueueRunner:
    """Runs QUEUED jobs top to bottom until the queue is empty or stopped.

    The next job is taken from the queue each time, so jobs added, removed or
    re-ordered while the queue runs are respected. ``stop_after_current``
    finishes the running job and then stops; ``cancel`` (via the executor's
    process handle) stops the running job immediately and leaves the rest
    queued.
    """

    def __init__(self, queue: JobQueue, executors: Dict[str, Executor],
                 cancelled: Optional[threading.Event] = None):
        self.queue = queue
        self.executors = executors
        self.stop_after_current = threading.Event()
        self.cancelled = cancelled or threading.Event()

    def run(self, on_start: Callable[[QueueJob], None] = lambda j: None,
            on_finish: Callable[[QueueJob], None] = lambda j: None) -> List[QueueJob]:
        done = []
        while not (self.stop_after_current.is_set() or self.cancelled.is_set()):
            job = self.queue.next_pending()
            if job is None:
                break
            self.queue.set_status(job, RUNNING)
            on_start(job)
            executor = self.executors.get(job.kind)
            try:
                if executor is None:
                    result = JobResult(FAILED, f"unknown job type '{job.kind}'")
                else:
                    result = executor(job)
            except Exception as e:          # an executor bug must not stop the queue
                result = JobResult(FAILED, f"{type(e).__name__}: {e}")
            if self.cancelled.is_set() and result.status not in (DONE, PARTIAL):
                result = JobResult(CANCELLED, result.message or "cancelled - the original was not changed")
            self.queue.set_status(job, result.status, result.message, result.final_path)
            done.append(job)
            on_finish(job)
        return done


def summary_text(jobs: Sequence[QueueJob]) -> str:
    counts: Dict[str, int] = {}
    for j in jobs:
        counts[j.status] = counts.get(j.status, 0) + 1
    head = "   ".join(f"{status}: {n}" for status, n in counts.items()) or "No jobs ran"
    lines = [head, ""]
    for j in jobs:
        lines.append(f"[{j.status}] {j.name} - {j.summary}")
        if j.final_path and j.final_path != j.path:
            lines.append(f"    saved as: {os.path.basename(j.final_path)}")
        if j.message and j.status not in (DONE,):
            for line in j.message.splitlines()[:6]:
                lines.append(f"    {line}")
    return "\n".join(lines)
