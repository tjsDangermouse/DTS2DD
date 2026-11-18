"""Queue tab: jobs from the Convert File tab and the Library Scanner, run in order."""
import threading
from tkinter import Button, END, Frame, Label, Scrollbar, Text, messagebox, ttk
from typing import List, Optional

from job_queue import (
    CANCELLED, DONE, FAILED, INTERRUPTED, KIND_LABELS, PARTIAL, QUEUED, RUNNING, SKIPPED,
    JobQueue, QueueJob, QueueRunner, summary_text,
)
from library_scanner import ScanCache
from media_inspector import discover_tools
from queue_jobs import QueueEnv, make_executors

STATUS_COLOURS = {QUEUED: "#333333", RUNNING: "#0b5cad", DONE: "#1b7f2a", PARTIAL: "#1b7f2a",
                  FAILED: "#b00020", SKIPPED: "#b36b00", CANCELLED: "#b36b00", INTERRUPTED: "#b36b00"}


class QueueTab:
    def __init__(self, parent, app, queue: Optional[JobQueue] = None):
        self.app = app
        self.root = app.root
        self.queue = queue or JobQueue()
        self.frame = Frame(parent, padx=10, pady=10)
        self.runner: Optional[QueueRunner] = None
        self.env: Optional[QueueEnv] = None
        self.worker: Optional[threading.Thread] = None
        self.running = False
        self._build()
        self.refresh()

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------

    def _build(self):
        f = self.frame
        Label(f, justify="left", fg="#555555", anchor="w", wraplength=900, text=(
            "Add jobs with 'Add to Queue' in the Convert File tab or the Library Scanner's repair dialog, "
            "then press Start Queue. Jobs run one at a time from the top. Each job re-reads its film just "
            "before it starts, so several jobs for the same film are fine.")).pack(fill="x")

        panes = ttk.PanedWindow(f, orient="vertical")
        panes.pack(fill="both", expand=True, pady=(6, 0))
        table = Frame(panes)
        cols = (("pos", "#", 40), ("film", "Film", 280), ("type", "Type", 100), ("job", "Job", 330),
                ("status", "Status", 150))
        self.tree = ttk.Treeview(table, columns=[c[0] for c in cols], show="headings", selectmode="extended",
                                 height=10)
        for key, title, width in cols:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=30, stretch=key in ("film", "job"))
        for status, colour in STATUS_COLOURS.items():
            self.tree.tag_configure(status, foreground=colour)
        sb = Scrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_details())
        panes.add(table, weight=3)

        lower = ttk.Notebook(panes)
        self.details = self._text(lower)
        self.log_text = self._text(lower)
        lower.add(self.details.master, text="Job details")
        lower.add(self.log_text.master, text="Activity")
        self.lower = lower
        panes.add(lower, weight=2)

        edit = Frame(f)
        edit.pack(fill="x", pady=(8, 0))
        self.edit_buttons = [
            Button(edit, text="▲ Move Up", command=lambda: self._move(-1)),
            Button(edit, text="▼ Move Down", command=lambda: self._move(1)),
            Button(edit, text="Remove", command=self._remove),
            Button(edit, text="Retry", command=self._retry),
            Button(edit, text="Clear Finished", command=self._clear_finished),
        ]
        for b in self.edit_buttons:
            b.pack(side="left", padx=(0, 6))
        self.cancel_button = Button(edit, text="Cancel Current", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right")
        self.stop_button = Button(edit, text="Stop After Current", command=self._stop_after, state="disabled")
        self.stop_button.pack(side="right", padx=6)
        self.start_button = Button(edit, text="Start Queue", command=self._start)
        self.start_button.pack(side="right", padx=6)

        self.progress = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 2))
        self.progress_label = Label(f, text="", fg="#555555", anchor="w")
        self.progress_label.pack(fill="x")

    def _text(self, parent) -> Text:
        holder = Frame(parent)
        sb = Scrollbar(holder)
        sb.pack(side="right", fill="y")
        text = Text(holder, height=9, wrap="word", state="disabled", yscrollcommand=sb.set)
        text.pack(fill="both", expand=True)
        sb.config(command=text.yview)
        return text

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _ui(self, func, *args):
        self.root.after(0, lambda: func(*args))

    def log(self, message: str):
        def write():
            self.log_text.config(state="normal")
            self.log_text.insert(END, message + "\n")
            self.log_text.see(END)
            self.log_text.config(state="disabled")
        self._ui(write)

    def refresh(self):
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        n = 0
        for job in self.queue.jobs:
            pos = ""
            if job.status in (QUEUED, RUNNING):
                n += 1
                pos = str(n)
            status = job.status if job.status != RUNNING else "Running…"
            self.tree.insert("", END, iid=job.id, tags=(job.status,),
                             values=(pos, job.name, KIND_LABELS.get(job.kind, job.kind), job.summary, status))
        keep = [i for i in selected if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        self._update_title()
        self._update_buttons()
        self._show_details()

    def _update_title(self):
        pending = len(self.queue.pending())
        try:
            self.app.notebook.tab(self.frame, text=f"Queue ({pending})" if pending else "Queue")
        except Exception:
            pass

    def _update_buttons(self):
        can_start = bool(self.queue.pending()) and not self.running
        self.start_button.config(state="normal" if can_start else "disabled")
        self.stop_button.config(state="normal" if self.running else "disabled")
        self.cancel_button.config(state="normal" if self.running else "disabled")

    def _selected(self) -> List[QueueJob]:
        return [j for j in (self.queue.get(i) for i in self.tree.selection()) if j]

    def _show_details(self):
        jobs = self._selected()
        self.details.config(state="normal")
        self.details.delete("1.0", END)
        if jobs:
            job = jobs[0]
            self.details.insert(END, f"{KIND_LABELS.get(job.kind, job.kind)} - {job.status}\n{job.path}\n\n")
            self.details.insert(END, "\n".join(job.description) + "\n")
            if job.message:
                self.details.insert(END, f"\nResult:\n{job.message}\n")
            if job.final_path and job.final_path != job.path:
                self.details.insert(END, f"\nSaved as: {job.final_path}\n")
        self.details.config(state="disabled")

    # ------------------------------------------------------------------
    # Adding jobs (called by the other tabs)
    # ------------------------------------------------------------------

    def add_jobs(self, jobs: List[QueueJob]) -> None:
        for job in jobs:
            earlier = self.queue.jobs_for(job.path)
            if earlier:
                job.description.append(f"(Runs after {len(earlier)} earlier queued job(s) for this film; "
                                       "its tracks are matched again before it starts.)")
            self.queue.add(job)
            self.log(f"Queued: {job.name} - {job.summary}")
        self.refresh()

    # ------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------

    def _move(self, delta):
        ids = [j.id for j in self._selected() if j.status != RUNNING]
        if ids:
            self.queue.move(ids, delta)
            self.refresh()

    def _remove(self):
        jobs = [j for j in self._selected() if j.status != RUNNING]
        if not jobs:
            return
        if any(j.status == QUEUED for j in jobs) and not messagebox.askyesno(
                "Remove jobs", f"Remove {len(jobs)} job(s) from the queue?\n\nNo film is changed."):
            return
        self.queue.remove([j.id for j in jobs])
        self.refresh()

    def _retry(self):
        n = self.queue.retry([j.id for j in self._selected()])
        if n:
            self.log(f"{n} job(s) queued again")
        self.refresh()

    def _clear_finished(self):
        self.queue.clear_finished()
        self.refresh()

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def _start(self):
        pending = self.queue.pending()
        if not pending or self.running:
            return
        if self.app.busy or (self.app.scanner and self.app.scanner.repairing):
            messagebox.showinfo("Queue", "Another conversion or repair is running. Start the queue when it "
                                "has finished.")
            return
        if not self.app.ffmpeg_path:
            messagebox.showerror("Queue", "FFmpeg is still being set up.")
            return
        films = len({j.path for j in pending})
        if not messagebox.askyesno(
                "Start queue",
                f"Run {len(pending)} job(s) on {films} film(s), one after another?\n\n"
                "Each job writes a temporary file and changes the film only after it succeeds "
                "(TrueHD and repairs: after verification). Jobs set to replace an original do so without "
                "asking again. This cannot be undone.", icon="warning"):
            return
        tools = discover_tools(self.app.ffmpeg_path, self.app.ffprobe_path)
        scanner = self.app.scanner
        if scanner.cache is None:
            scanner.cache = ScanCache()
        self.env = QueueEnv(
            ffmpeg=self.app.ffmpeg_path, ffprobe=tools.ffprobe, encoder=lambda: self.app.encoder_report,
            tools=tools, config=scanner.config, cache=scanner.cache, log=self.log,
            tsmuxer=lambda: getattr(self.app, "tsmuxer_info", None),
            progress=lambda phase, fraction, detail: self._ui(self._progress, phase, fraction, detail))
        self.runner = QueueRunner(self.queue, make_executors(self.env), cancelled=self.env.cancel_event)
        self.running = True
        self.app.set_external_busy(True)
        self.lower.select(1)
        self.refresh()
        state = {"n": 0, "name": ""}

        def on_start(job):
            state["n"] += 1
            state["name"] = job.name
            self._ui(self.refresh)
            self._ui(self._progress, "Starting", None, "")

        def on_finish(job):
            self.log(f"[{job.status}] {job.name} - {job.message.splitlines()[0] if job.message else ''}")
            self._ui(self.refresh)
            self._ui(self.app.job_finished, job.final_path or job.path)
            if job.final_path and job.final_path != job.path:
                self._ui(self.app.job_finished, job.path)

        def worker():
            done = []
            try:
                done = self.runner.run(on_start, on_finish)
            except Exception as e:
                self.log(f"Queue stopped: {e}")
            finally:
                self._ui(self._finished, done)

        self._state = state
        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _progress(self, phase, fraction, detail):
        state = getattr(self, "_state", {"n": 0, "name": ""})
        text = f"Job {state['n']}: {state['name']} • {phase}" + (f": {fraction * 100:.1f}%" if fraction is not None else "")
        if detail:
            text += f" • {detail}"
        if fraction is None:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.config(mode="indeterminate")
                self.progress.start()
        else:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.config(mode="determinate")
            self.progress.config(value=fraction * 100)
        self.progress_label.config(text=text)

    def _finished(self, done):
        self.running = False
        self.progress.stop()
        self.progress.config(mode="determinate", value=100 if done else 0)
        stopped = self.runner and (self.runner.cancelled.is_set() or self.runner.stop_after_current.is_set())
        left = len(self.queue.pending())
        text = (f"Queue {'stopped' if stopped else 'finished'}: {len(done)} job(s) ran"
                + (f", {left} still queued" if left else ""))
        self.progress_label.config(text=text)
        self.log(text)
        self.app.set_external_busy(False)
        self.refresh()
        if done:
            from scanner_ui import ReportDialog
            ReportDialog(self.root, "Queue report", summary_text(done))

    def _stop_after(self):
        if self.runner:
            self.runner.stop_after_current.set()
            self.log("The queue will stop after the current job.")
            self.stop_button.config(state="disabled")

    def _cancel(self):
        if self.runner and self.env and messagebox.askyesno(
                "Cancel", "Cancel the running job and stop the queue?\n\nThe film being processed is not "
                          "changed; the remaining jobs stay queued."):
            threading.Thread(target=self.env.cancel, daemon=True).start()
            self.log("Cancelling…")

    def shutdown(self, timeout: float = 15):
        if self.env:
            self.env.cancel()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=timeout)
