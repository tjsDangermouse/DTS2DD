"""Library Scanner tab (tkinter).

Scanning is read-only and runs on worker threads; widgets are only touched
on the Tk main thread (via ``root.after``). Repairs start only from the
Repair dialog after the user has seen the per-file plan and confirmed it.
"""
import csv
import os
import threading
from pathlib import Path
from tkinter import (
    BooleanVar, Button, Checkbutton, END, Entry, Frame, IntVar, Label, LabelFrame, Radiobutton,
    Scrollbar, Spinbox, StringVar, Text, Toplevel, filedialog, messagebox, ttk,
)
from typing import Dict, List, Optional

from app_settings import get_setting, set_setting
from compatibility_rules import (
    ACTION_CLEAN_SUBS, ACTION_DTS_CONVERT, ACTION_FIX_DEFAULT, ACTION_FLAC_TRUEHD, ACTION_LABELS,
    CAT_ATMOS, CAT_DV, CAT_SUBTITLES, DEVICE_PROFILES, ERROR, INFO, RULES, SEVERITY_RANK, WARNING,
    ScannerConfig, available_actions, language_label, normalize_language, sub_description,
)
from ffmpeg_helper import ProcessHandle
from library_repair import (
    STATUS_CANCELLED, STATUS_FAILED, STATUS_PARTIAL, STATUS_REPAIRED, STATUS_SKIPPED,
    RepairContext, RepairOptions, plan_repair, run_batch,
)
from library_scanner import LibraryScanner, ScanCache, ScanItem
from media_inspector import discover_tools
from media_probe import format_seconds

FILTERS = ["All movies", "Issues only", "Subtitle warnings", "FLAC audio", "Atmos warnings",
           "Dolby Vision", "Passed files"]
COLUMNS = (("file", "File", 260), ("video", "Video / HDR", 150), ("audio", "Audio", 210),
           ("subs", "Subs", 60), ("issues", "Issues", 220), ("severity", "Severity", 85),
           ("actions", "Recommended", 150), ("status", "Repair status", 110))
SEVERITY_COLOURS = {ERROR: "#b00020", WARNING: "#b36b00", INFO: "#333333", "Passed": "#1b7f2a"}
DTS_TARGETS = {"Auto (TrueHD if lossless, else E-AC-3)": "auto", "Dolby TrueHD only": "truehd",
               "E-AC-3 (Dolby Digital Plus)": "eac3", "AC-3 (Dolby Digital)": "ac3"}


def _rank(finding) -> int:
    return SEVERITY_RANK[finding.severity]


def matches_filter(item: ScanItem, name: str) -> bool:
    findings = item.findings
    if name == "Issues only":
        return bool(item.issues)
    if name == "Subtitle warnings":
        return any(f.category == CAT_SUBTITLES and _rank(f) >= 2 for f in findings)
    if name == "FLAC audio":
        return any(a.codec == "flac" for a in item.report.audio)
    if name == "Atmos warnings":
        return any(f.category == CAT_ATMOS and _rank(f) >= 2 for f in findings)
    if name == "Dolby Vision":
        return any(v.dv_profile is not None for v in item.report.video) or \
            any(f.category == CAT_DV and _rank(f) >= 2 for f in findings)
    if name == "Passed files":
        return not item.issues
    return True


def row_values(item: ScanItem, root: Optional[str]) -> tuple:
    report = item.report
    try:
        name = os.path.relpath(report.path, root) if root else report.name
    except ValueError:
        name = report.name
    video = next((v.label for v in report.video if not v.attached_pic), "-")
    audio = "; ".join(a.label for a in report.audio) or "-"
    pgs = sum(1 for s in report.subtitles if s.is_pgs)
    subs = f"{len(report.subtitles)}" + (f" ({pgs} PGS)" if pgs else "")
    issues = item.issues
    issue_text = f"{len(issues)}: {issues[0].title}" if issues else "-"
    severity = item.severity if item.issues else ("Passed" if not report.errors else ERROR)
    actions = ", ".join(ACTION_LABELS[a] for a in item.recommended)
    return (name, video, audio, subs, issue_text, severity, actions, item.repair_status)


class LibraryScannerTab:
    def __init__(self, parent, app):
        self.app = app
        self.root = app.root
        self.frame = Frame(parent, padx=10, pady=10)
        self.config = ScannerConfig.load()
        self.items: Dict[str, ScanItem] = {}
        self.cache: Optional[ScanCache] = None
        self.scan_root: Optional[str] = None
        self.cancel_event: Optional[threading.Event] = None
        self.repair_handle: Optional[ProcessHandle] = None
        self.worker: Optional[threading.Thread] = None
        self.scanning = False
        self.repairing = False
        self._pending: List[ScanItem] = []
        self._pending_lock = threading.Lock()
        self._sort = ("file", False)
        self._build()

    @property
    def busy(self) -> bool:
        return self.scanning or self.repairing

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------

    def _build(self):
        f = self.frame
        top = LabelFrame(f, text="Library Folder", padx=10, pady=6)
        top.pack(fill="x")
        self.folder_var = StringVar(value=get_setting("library_scanner_folder") or "")
        Entry(top, textvariable=self.folder_var, width=50).pack(side="left", fill="x", expand=True)
        Button(top, text="Browse", command=self._browse).pack(side="left", padx=4)
        self.scan_button = Button(top, text="Scan", command=self._start_scan)
        self.scan_button.pack(side="left", padx=4)
        self.cancel_button = Button(top, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=4)
        self.force_var = BooleanVar(value=False)
        Checkbutton(top, text="Rescan all (ignore cache)", variable=self.force_var).pack(side="left", padx=6)
        Button(top, text="Rules & Devices…", command=self._open_settings).pack(side="right")

        self.progress = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 2))
        self.progress_label = Label(f, text="Choose a folder and press Scan. Scanning never modifies files.",
                                    fg="#555555", anchor="w")
        self.progress_label.pack(fill="x")

        bar = Frame(f)
        bar.pack(fill="x", pady=(6, 4))
        Label(bar, text="Show:").pack(side="left")
        self.filter_var = StringVar(value=FILTERS[0])
        combo = ttk.Combobox(bar, textvariable=self.filter_var, values=FILTERS, state="readonly", width=18)
        combo.pack(side="left", padx=4)
        combo.bind("<<ComboboxSelected>>", lambda e: self._refill())
        self.count_label = Label(bar, text="", fg="#555555")
        self.count_label.pack(side="left", padx=8)
        self.tools_label = Label(bar, text="", fg="#555555")
        self.tools_label.pack(side="right")
        self._update_devices_label()

        panes = ttk.PanedWindow(f, orient="vertical")
        panes.pack(fill="both", expand=True)
        table = Frame(panes)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in COLUMNS], show="headings",
                                 selectmode="extended", height=10)
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title, command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, minwidth=50, stretch=key in ("file", "audio", "issues"))
        for severity, colour in SEVERITY_COLOURS.items():
            self.tree.tag_configure(severity, foreground=colour)
        ysb = Scrollbar(table, command=self.tree.yview)
        xsb = Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_details())
        panes.add(table, weight=3)

        lower = ttk.Notebook(panes)
        self.details = self._text_panel(lower)
        self.log_text = self._text_panel(lower)
        lower.add(self.details.master, text="Details & evidence")
        lower.add(self.log_text.master, text="Activity")
        self.lower = lower
        panes.add(lower, weight=2)

        buttons = Frame(f)
        buttons.pack(fill="x", pady=(8, 0))
        self.action_buttons = [
            Button(buttons, text="Clean Subtitles…", command=lambda: self._open_repair([ACTION_CLEAN_SUBS])),
            Button(buttons, text="Convert FLAC to TrueHD…", command=lambda: self._open_repair([ACTION_FLAC_TRUEHD])),
            Button(buttons, text="Repair Selected…", command=lambda: self._open_repair(None)),
        ]
        for b in self.action_buttons:
            b.pack(side="left", padx=(0, 6))
        Button(buttons, text="Select all shown", command=self._select_all).pack(side="left", padx=(12, 6))
        Button(buttons, text="Save Report…", command=self._save_report).pack(side="right")

    def _text_panel(self, parent) -> Text:
        holder = Frame(parent)
        sb = Scrollbar(holder)
        sb.pack(side="right", fill="y")
        text = Text(holder, height=10, wrap="word", state="disabled", yscrollcommand=sb.set)
        text.pack(fill="both", expand=True)
        sb.config(command=text.yview)
        text.tag_configure("h1", font=("TkDefaultFont", 11, "bold"))
        text.tag_configure("h2", font=("TkDefaultFont", 10, "bold"), spacing1=6)
        text.tag_configure("muted", foreground="#555555")
        for severity, colour in SEVERITY_COLOURS.items():
            text.tag_configure(severity, foreground=colour, font=("TkDefaultFont", 10, "bold"))
        return text

    def _update_devices_label(self):
        names = [DEVICE_PROFILES[k].name for k in self.config.devices if k in DEVICE_PROFILES]
        tools = discover_tools(self.app.ffmpeg_path, self.app.ffprobe_path)
        self.tools_label.config(text=tools.describe() + ("    Devices: " + ", ".join(names) if names
                                                         else "    No device profiles"))

    # ------------------------------------------------------------------
    # Helpers
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

    def _set_progress(self, fraction: Optional[float], text: str):
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

    def _set_busy(self):
        busy = self.busy
        self.scan_button.config(state="disabled" if busy else "normal")
        self.cancel_button.config(state="normal" if busy else "disabled")
        for b in self.action_buttons:
            b.config(state="disabled" if self.repairing else "normal")
        self.app.set_external_busy(self.repairing)
        if not busy:
            self.progress.stop()
            self.progress.config(mode="determinate")

    def _tools(self):
        return discover_tools(self.app.ffmpeg_path, self.app.ffprobe_path)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _browse(self):
        folder = filedialog.askdirectory(title="Choose your movie library folder",
                                         initialdir=self.folder_var.get() or None)
        if folder:
            self.folder_var.set(folder)

    def _start_scan(self):
        root = self.folder_var.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("Library Scanner", "Choose an existing folder to scan.")
            return
        if not self.app.ffmpeg_path:
            messagebox.showerror("Library Scanner", "FFmpeg is still being set up. Try again in a moment.")
            return
        tools = self._tools()
        if not tools.ffprobe:
            messagebox.showerror("Library Scanner", "ffprobe was not found; it is needed to analyse files.")
            return
        set_setting("library_scanner_folder", root)
        self.scan_root = os.path.abspath(root)
        self.items.clear()
        self.tree.delete(*self.tree.get_children())
        self.cancel_event = threading.Event()
        cancel = self.cancel_event
        self.scanning = True
        self._set_busy()
        self._set_progress(None, "Finding films…")
        self.log("========================================")
        self.log(f"Scanning {root} (read-only)")
        self.log(f"Tools: {tools.describe()}")
        force = self.force_var.get()
        config = self.config

        def worker():
            try:
                if self.cache is None:
                    self.cache = ScanCache()
                scanner = LibraryScanner(tools, config, self.cache)
                summary = scanner.scan(root, self._queue_item, self._report_progress, cancel, force=force)
                self._ui(self._scan_finished, summary)
            except Exception as e:      # keep the UI usable whatever happens
                self.log(f"Scan failed: {e}")
                self._ui(self._scan_finished, None)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _queue_item(self, item: ScanItem):
        with self._pending_lock:
            first = not self._pending
            self._pending.append(item)
        if first:
            self.root.after(150, self._flush_items)

    def _flush_items(self):
        with self._pending_lock:
            batch, self._pending = self._pending, []
        for item in batch:
            self.items[item.path] = item
            self._render_item(item)
        self._update_count()

    def _report_progress(self, p):
        if p.total:
            parts = [f"Scanning {p.done} of {p.total}"]
            if p.current:
                parts.append(p.current[:80])
            parts.append(f"{p.issues} with issues")
            if p.cached:
                parts.append(f"{p.cached} unchanged (cached)")
            if p.eta is not None and p.done < p.total:
                parts.append(f"~{format_seconds(p.eta)} remaining")
            self._ui(self._set_progress, p.done / p.total, " • ".join(parts))
        else:
            self._ui(self._set_progress, None, p.current or "Finding films…")

    def _scan_finished(self, summary):
        self._flush_items()
        self.scanning = False
        self._set_busy()
        if summary is None:
            self._set_progress(0, "Scan failed - see Activity")
            return
        issues = sum(1 for i in self.items.values() if i.issues)
        state = "cancelled" if summary.cancelled else "complete"
        text = (f"Scan {state}: {len(self.items)} of {summary.total} files in {format_seconds(summary.elapsed)} • "
                f"{issues} with issues • {summary.cached} unchanged since last scan • {summary.errors} unreadable")
        self._set_progress(1.0 if not summary.cancelled else (len(self.items) / summary.total if summary.total else 0),
                           text)
        self.log(text)

    def _cancel(self):
        if self.scanning and self.cancel_event:
            self.cancel_event.set()
            self.log("Cancelling scan…")
        elif self.repairing and self.repair_handle:
            if messagebox.askyesno("Cancel repairs", "Stop the running repair and skip the remaining files?\n\n"
                                   "The file being processed is left unchanged."):
                self.log("Cancelling repairs…")
                threading.Thread(target=self.repair_handle.cancel, daemon=True).start()

    def shutdown(self, timeout: float = 15) -> None:
        """Stop work when the window closes (temp files are cleaned by the workers)."""
        if self.cancel_event:
            self.cancel_event.set()
        if self.repair_handle:
            self.repair_handle.cancel()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _render_item(self, item: ScanItem):
        iid = item.path
        if not matches_filter(item, self.filter_var.get()):
            if self.tree.exists(iid):
                self.tree.delete(iid)
            return
        values = row_values(item, self.scan_root)
        tag = values[5]
        if self.tree.exists(iid):
            self.tree.item(iid, values=values, tags=(tag,))
        else:
            self.tree.insert("", END, iid=iid, values=values, tags=(tag,))

    def _refill(self):
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for item in self._sorted(self.items.values()):
            self._render_item(item)
        keep = [i for i in selected if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        self._update_count()

    def _sorted(self, items):
        key, reverse = self._sort
        index = [c[0] for c in COLUMNS].index(key)

        def sort_key(item):
            if key == "severity":
                return SEVERITY_RANK.get(item.severity, 0) if item.issues else 0
            if key == "issues":
                return len(item.issues)
            if key == "subs":
                return len(item.report.subtitles)
            return str(row_values(item, self.scan_root)[index]).lower()
        return sorted(items, key=sort_key, reverse=reverse)

    def _sort_by(self, key):
        current, reverse = self._sort
        self._sort = (key, not reverse if current == key else key in ("severity", "issues", "subs"))
        self._refill()

    def _update_count(self):
        shown = len(self.tree.get_children())
        issues = sum(1 for i in self.items.values() if i.issues)
        self.count_label.config(text=f"{shown} shown • {len(self.items)} scanned • {issues} with issues")

    def _select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def _selected_items(self) -> List[ScanItem]:
        return [self.items[i] for i in self.tree.selection() if i in self.items]

    # ------------------------------------------------------------------
    # Details panel
    # ------------------------------------------------------------------

    def _show_details(self):
        items = self._selected_items()
        text = self.details
        text.config(state="normal")
        text.delete("1.0", END)
        if not items:
            text.config(state="disabled")
            return
        item = items[0]
        if len(items) > 1:
            text.insert(END, f"{len(items)} files selected - showing the first.\n", "muted")
        self._write_details(text, item)
        text.config(state="disabled")
        self.lower.select(0)

    def _write_details(self, text: Text, item: ScanItem):
        r = item.report
        text.insert(END, r.name + "\n", "h1")
        size = f"{r.size / 1e9:.2f} GB" if r.size else "?"
        text.insert(END, f"{r.path}\n{size} • {format_seconds(r.duration)} • {r.container} • "
                         f"{r.chapters} chapters • {r.attachments} attachments"
                         + (" • from cache" if item.from_cache else "") + "\n", "muted")
        sources = ", ".join(k for k, used in r.tools.items() if used) or "none"
        text.insert(END, f"Metadata sources: {sources}\n", "muted")

        text.insert(END, "Confirmed file characteristics\n", "h2")
        for v in r.video:
            if v.attached_pic:
                continue
            text.insert(END, f"  Video #{v.index}: {v.label} ({v.pix_fmt or '?'})\n")
        for a in r.audio:
            flags = [n for n, on in (("default", a.default), ("forced", a.forced), ("commentary", a.commentary)) if on]
            line = (f"  Audio #{a.index}: {a.format_name} {a.channel_layout or ''} {a.channels or '?'}ch "
                    f"{a.sample_rate or '?'} Hz" + (f" {a.bit_depth}-bit" if a.bit_depth else "")
                    + f" • {language_label(a.language)}" + (f' • "{a.title}"' if a.title else "")
                    + (" {" + ", ".join(flags) + "}" if flags else ""))
            text.insert(END, line + "\n")
            if a.atmos:
                text.insert(END, f"      Atmos: {a.atmos.status} (confidence {a.atmos.confidence})\n", "muted")
        text.insert(END, f"  Subtitles: {len(r.subtitles)}\n")
        for s in r.subtitles:
            text.insert(END, f"    {sub_description(s)}\n", "muted")

        file_findings = [f for f in item.findings if f.device is None]
        device_findings = [f for f in item.findings if f.device is not None]
        for heading, findings in (("Findings about the file", file_findings),
                                  ("Device-specific potential problems", device_findings)):
            text.insert(END, heading + "\n", "h2")
            if not findings:
                text.insert(END, "  none\n", "muted")
            for f in findings:
                device = f" [{DEVICE_PROFILES[f.device].name}]" if f.device in DEVICE_PROFILES else ""
                text.insert(END, f"  {f.severity}", f.severity)
                text.insert(END, f"{device}: {f.title}\n")
                if f.detail:
                    text.insert(END, f"      {f.detail}\n")
                for e in f.evidence:
                    text.insert(END, f"      • {e}\n", "muted")
                if f.actions:
                    text.insert(END, "      Recommended: " + ", ".join(ACTION_LABELS[a] for a in f.actions) + "\n")
        if item.repair_status:
            text.insert(END, "Repair status\n", "h2")
            text.insert(END, f"  {item.repair_status}\n")
        if r.notes:
            text.insert(END, "Notes\n", "h2")
            for n in r.notes:
                text.insert(END, f"  {n}\n", "muted")
        text.insert(END, "Original metadata (ffprobe)\n", "h2")
        for s in r.raw_streams:
            summary = {k: v for k, v in s.items() if k not in ("side_data_list",)}
            text.insert(END, f"  {summary}\n", "muted")
            for sd in s.get("side_data_list") or []:
                text.insert(END, f"      side data: {sd}\n", "muted")
        for a in r.audio:
            if a.mediainfo:
                text.insert(END, f"  MediaInfo audio #{a.index}: {a.mediainfo}\n", "muted")
            if a.bitstream:
                text.insert(END, f"  Bitstream audio #{a.index}: {a.bitstream}\n", "muted")
        for v in r.video:
            if v.mediainfo_hdr:
                text.insert(END, f"  MediaInfo video #{v.index}: {v.mediainfo_hdr}\n", "muted")

    # ------------------------------------------------------------------
    # Report export
    # ------------------------------------------------------------------

    def _save_report(self):
        items = [self.items[i] for i in self.tree.get_children() if i in self.items]
        if not items:
            messagebox.showinfo("Save Report", "Nothing to save - scan a folder first.")
            return
        path = filedialog.asksaveasfilename(title="Save scan report", defaultextension=".txt",
                                            initialfile="DTS2DD library report.txt",
                                            filetypes=[("Text report", "*.txt"), ("CSV", "*.csv")])
        if not path:
            return
        try:
            if path.lower().endswith(".csv"):
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["file", "severity", "category", "device", "finding", "detail", "recommended"])
                    for item in items:
                        for finding in item.findings or [None]:
                            if finding is None:
                                w.writerow([item.path, "Passed", "", "", "", "", ""])
                                continue
                            w.writerow([item.path, finding.severity, finding.category,
                                        DEVICE_PROFILES[finding.device].name if finding.device else "",
                                        finding.title, finding.detail,
                                        ", ".join(ACTION_LABELS[a] for a in finding.actions)])
            else:
                text = Text(self.frame)       # reuse the details renderer
                for item in items:
                    self._write_details(text, item)
                    text.insert(END, "\n" + "-" * 70 + "\n")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text.get("1.0", END))
                text.destroy()
            self.log(f"Report saved: {path}")
        except OSError as e:
            messagebox.showerror("Save Report", str(e))

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _open_settings(self):
        SettingsDialog(self)

    def apply_config(self, config: ScannerConfig):
        self.config = config
        config.save()
        for item in self.items.values():
            item.refresh(config)
        self._update_devices_label()
        self._refill()
        self._show_details()

    # ------------------------------------------------------------------
    # Repairs
    # ------------------------------------------------------------------

    def _open_repair(self, preselect: Optional[List[str]]):
        items = self._selected_items()
        if not items:
            messagebox.showinfo("Repair", "Select one or more files in the list first "
                                "(Ctrl/Cmd-click or Shift-click for several, or 'Select all shown').")
            return
        if self.repairing:
            messagebox.showinfo("Repair", "Wait for the running repairs to finish.")
            return
        RepairDialog(self, items, preselect)

    def other_work_running(self) -> Optional[str]:
        """Why repairs cannot start now (they can always be queued)."""
        if self.repairing:
            return "Repairs are already running."
        if self.scanning:
            return "A scan is running."
        if self.app.busy:
            return "A conversion is running in the Convert File tab."
        if self.app.queue_tab and self.app.queue_tab.running:
            return "The queue is running."
        return None

    def start_repairs(self, plans, options: RepairOptions):
        tools = self._tools()
        self.repair_handle = ProcessHandle()
        ctx = RepairContext(ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe, encoder=self.app.encoder_report,
                            options=options, handle=self.repair_handle, log=self.log,
                            progress=lambda phase, fraction, detail: self._ui(self._repair_progress, phase,
                                                                              fraction, detail))
        self.repairing = True
        self._set_busy()
        self.lower.select(1)
        total = len(plans)
        state = {"n": 0, "name": ""}

        def on_start(plan):
            state["n"] += 1
            state["name"] = Path(plan.path).name
            self._ui(self._set_status, plan.path, "Repairing…")
            self._ui(self._set_progress, None, f"Repairing {state['n']} of {total}: {state['name']}")

        def on_outcome(outcome):
            self._ui(self._set_status, outcome.path, outcome.status)
            if outcome.status in (STATUS_REPAIRED, STATUS_PARTIAL):
                self._rescan(outcome.final_path or outcome.path, outcome.status, tools)

        def worker():
            batch = None
            try:
                batch = run_batch(plans, ctx, on_outcome=on_outcome, on_start=on_start)
            except Exception as e:
                self.log(f"Repair batch stopped: {e}")
            finally:
                self._ui(self._repairs_finished, batch)

        self._progress_prefix = lambda: f"{state['n']}/{total} {state['name']}"
        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _repair_progress(self, phase, fraction, detail):
        prefix = self._progress_prefix() if hasattr(self, "_progress_prefix") else ""
        text = f"{prefix} • {phase}" + (f": {fraction * 100:.1f}%" if fraction is not None else "")
        if detail:
            text += f" • {detail}"
        self._set_progress(fraction, text)

    def _set_status(self, path: str, status: str):
        item = self.items.get(path)
        if item:
            item.repair_status = status
            self._render_item(item)

    def _rescan(self, path: str, status: str, tools):
        """Refresh a repaired file's row (runs on the repair worker thread)."""
        if self.cache is None:
            self.cache = ScanCache()
        self.cache.forget(path)
        try:
            item = LibraryScanner(tools, self.config, self.cache).inspect_one(path)
            self.cache.save()
        except Exception as e:
            self.log(f"Could not re-scan {Path(path).name}: {e}")
            return
        item.repair_status = status
        self._queue_item(item)

    def _repairs_finished(self, batch):
        self.repairing = False
        self.repair_handle = None
        self._set_busy()
        if batch is None:
            self._set_progress(0, "Repairs stopped - see Activity")
            return
        summary = (f"Repairs finished: {batch.count(STATUS_REPAIRED, STATUS_PARTIAL)} repaired, "
                   f"{batch.count(STATUS_SKIPPED)} skipped, {batch.count(STATUS_FAILED)} failed, "
                   f"{batch.count(STATUS_CANCELLED)} cancelled")
        self._set_progress(1.0, summary)
        self.log(summary)
        ReportDialog(self.root, "Repair report", batch.text())


# ----------------------------------------------------------------------
# Dialogs
# ----------------------------------------------------------------------

class ReportDialog(Toplevel):
    def __init__(self, parent, title: str, body: str):
        super().__init__(parent)
        self.title(title)
        self.geometry("720x480")
        text = Text(self, wrap="word")
        sb = Scrollbar(self, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        buttons = Frame(self, pady=6)
        buttons.pack(side="bottom", fill="x")
        sb.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", body)
        text.config(state="disabled")
        self.body = body
        Button(buttons, text="Save…", command=self._save).pack(side="left", padx=8)
        Button(buttons, text="Close", command=self.destroy).pack(side="right", padx=8)

    def _save(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".txt",
                                            initialfile="DTS2DD repair report.txt")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.body)


class RepairDialog(Toplevel):
    """Choose repairs + options, preview the exact per-file plan, then start."""

    def __init__(self, tab: LibraryScannerTab, items: List[ScanItem], preselect: Optional[List[str]]):
        super().__init__(tab.root)
        self.tab = tab
        self.items = items
        self.title(f"Repair {len(items)} file(s)")
        self.geometry("860x760")
        self.transient(tab.root)
        config = tab.config
        counts = {a: sum(1 for i in items if a in available_actions(i.findings)) for a in ACTION_LABELS}
        if preselect is None:
            preselect = sorted({a for i in items for a in i.recommended})

        acts = LabelFrame(self, text="Repairs (only the ticked repairs run; scanning never changed anything)",
                          padx=10, pady=6)
        acts.pack(fill="x", padx=10, pady=(10, 4))
        self.action_vars = {}
        for action in (ACTION_CLEAN_SUBS, ACTION_FLAC_TRUEHD, ACTION_DTS_CONVERT, ACTION_FIX_DEFAULT):
            var = BooleanVar(value=action in preselect)
            self.action_vars[action] = var
            Checkbutton(acts, text=f"{ACTION_LABELS[action]}  ({counts[action]} of {len(items)} files)",
                        variable=var, command=self._preview).pack(anchor="w")

        opts = LabelFrame(self, text="Options", padx=10, pady=6)
        opts.pack(fill="x", padx=10, pady=4)
        row = Frame(opts)
        row.pack(fill="x")
        Label(row, text="Subtitles - keep languages:").pack(side="left")
        self.langs_var = StringVar(value=", ".join(config.keep_languages))
        e = Entry(row, textvariable=self.langs_var, width=16)
        e.pack(side="left", padx=4)
        e.bind("<KeyRelease>", lambda ev: self._preview())
        self.forced_var = BooleanVar(value=config.keep_forced)
        self.sdh_var = BooleanVar(value=config.keep_sdh)
        self.dup_var = BooleanVar(value=config.remove_duplicate_subs)
        self.unlabelled_var = BooleanVar(value=False)
        for text, var in (("keep forced", self.forced_var), ("keep SDH", self.sdh_var),
                          ("remove duplicates", self.dup_var), ("remove tracks without a language tag", self.unlabelled_var)):
            Checkbutton(row, text=text, variable=var, command=self._preview).pack(side="left", padx=4)

        row2 = Frame(opts)
        row2.pack(fill="x", pady=(4, 0))
        self.keep_audio_var = BooleanVar(value=True)
        self.default_var = BooleanVar(value=True)
        self.strict_var = BooleanVar(value=True)
        for text, var in (("Keep the original FLAC/DTS track", self.keep_audio_var),
                          ("Make the new track the default", self.default_var),
                          ("Strict lossless verification (TrueHD)", self.strict_var)):
            Checkbutton(row2, text=text, variable=var, command=self._preview).pack(side="left", padx=(0, 8))

        row3 = Frame(opts)
        row3.pack(fill="x", pady=(4, 0))
        Label(row3, text="TrueHD output:").pack(side="left")
        self.replace_var = StringVar(value="replace")
        Radiobutton(row3, text="replace original after verification", variable=self.replace_var,
                    value="replace", command=self._preview).pack(side="left")
        Radiobutton(row3, text="save as new …_Sound_TrueHD.mkv", variable=self.replace_var,
                    value="new", command=self._preview).pack(side="left")

        row4 = Frame(opts)
        row4.pack(fill="x", pady=(4, 0))
        Label(row4, text="DTS conversion:").pack(side="left")
        self.dts_var = StringVar(value=list(DTS_TARGETS)[0])
        c = ttk.Combobox(row4, textvariable=self.dts_var, values=list(DTS_TARGETS), state="readonly", width=36)
        c.pack(side="left", padx=4)
        c.bind("<<ComboboxSelected>>", lambda ev: self._preview())
        Label(row4, text="lossy bitrate:").pack(side="left", padx=(8, 2))
        self.bitrate_var = StringVar(value="640k")
        b = ttk.Combobox(row4, textvariable=self.bitrate_var, state="readonly", width=6,
                         values=["384k", "448k", "640k", "768k"])
        b.pack(side="left")
        b.bind("<<ComboboxSelected>>", lambda ev: self._preview())

        report = tab.app.encoder_report
        enc = ("TrueHD encoder: checking…" if tab.app.encoder_checking else
               f"TrueHD encoder: {report.status} - {report.message}" if report else "TrueHD encoder: not configured")
        Label(opts, text=enc + "\nConverting FLAC to TrueHD does not create Dolby Atmos. Atmos tracks are never "
                               "re-encoded.", fg="#555555", justify="left", wraplength=800).pack(anchor="w", pady=(6, 0))

        prev = LabelFrame(self, text="Plan - tracks kept and removed, per file", padx=6, pady=4)
        prev.pack(fill="both", expand=True, padx=10, pady=4)
        sb = Scrollbar(prev)
        sb.pack(side="right", fill="y")
        self.preview = Text(prev, wrap="none", yscrollcommand=sb.set, height=16)
        self.preview.pack(fill="both", expand=True)
        sb.config(command=self.preview.yview)

        buttons = Frame(self, pady=8)
        buttons.pack(fill="x", padx=10)
        self.start_button = Button(buttons, text="Start Repairs Now", command=self._start)
        self.start_button.pack(side="right")
        self.queue_button = Button(buttons, text="Add to Queue", command=self._queue)
        self.queue_button.pack(side="right", padx=8)
        Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.summary_label = Label(buttons, text="", fg="#555555")
        self.summary_label.pack(side="left")
        self.plans = []
        self._preview()
        self.grab_set()

    def _scanner_config(self) -> ScannerConfig:
        config = ScannerConfig(**{k: getattr(self.tab.config, k) for k in ScannerConfig.__dataclass_fields__})
        langs = [normalize_language(x) for x in self.langs_var.get().replace(";", ",").split(",")]
        config.keep_languages = [l for l in langs if l] or ["eng"]
        config.keep_forced = self.forced_var.get()
        config.keep_sdh = self.sdh_var.get()
        config.remove_duplicate_subs = self.dup_var.get()
        return config

    def _repair_options(self) -> RepairOptions:
        return RepairOptions(
            remove_unlabelled_subs=self.unlabelled_var.get(),
            remove_duplicate_subs=self.dup_var.get(),
            keep_original_audio=self.keep_audio_var.get(),
            make_new_default=self.default_var.get(),
            strict_verify=self.strict_var.get(),
            replace_original=self.replace_var.get() == "replace",
            dts_target=DTS_TARGETS[self.dts_var.get()],
            lossy_bitrate=self.bitrate_var.get(),
        )

    def _preview(self):
        actions = [a for a, v in self.action_vars.items() if v.get()]
        config, options = self._scanner_config(), self._repair_options()
        encoder = self.tab.app.encoder_report
        self.plans = [plan_repair(i, actions, options, config, encoder) for i in self.items]
        self.preview.config(state="normal")
        self.preview.delete("1.0", END)
        for plan in self.plans:
            self.preview.insert(END, "\n".join(plan.describe()) + "\n\n")
        self.preview.config(state="disabled")
        n = sum(1 for p in self.plans if p.method)
        self.summary_label.config(text=f"{n} of {len(self.plans)} file(s) will be changed")
        self.start_button.config(state="normal" if n else "disabled")
        self.queue_button.config(state="normal" if n else "disabled")

    def _confirm_unlabelled(self, plans) -> bool:
        unlabelled = [(p, s) for p in plans if p.subtitle_plan for s in p.subtitle_plan.unlabelled_removed]
        if not unlabelled:
            return True
        listing = "\n".join(f"  {Path(p.path).name}: {sub_description(s)}" for p, s in unlabelled[:12])
        more = f"\n  … and {len(unlabelled) - 12} more" if len(unlabelled) > 12 else ""
        return messagebox.askyesno(
            "Remove unlabelled subtitles?",
            f"{len(unlabelled)} subtitle track(s) have NO language tag and will be removed:\n\n"
            f"{listing}{more}\n\nThey might be English. Remove them?", icon="warning", parent=self)

    def _remember_prefs(self):
        config = self._scanner_config()
        self.tab.config.keep_languages = config.keep_languages
        self.tab.config.keep_forced = config.keep_forced
        self.tab.config.keep_sdh = config.keep_sdh
        self.tab.config.remove_duplicate_subs = config.remove_duplicate_subs
        self.tab.config.save()
        return config

    def _queue(self):
        from queue_jobs import repair_job
        self._preview()
        plans = [p for p in self.plans if p.method]
        if not plans or not self._confirm_unlabelled(plans):
            return
        options = self._repair_options()
        config = self._remember_prefs()
        jobs = []
        for plan in plans:
            try:
                jobs.append(repair_job(plan, options, config))
            except OSError as e:
                self.tab.log(f"Not queued {Path(plan.path).name}: {e}")
        self.tab.app.queue_tab.add_jobs(jobs)
        self.tab.log(f"Added {len(jobs)} repair job(s) to the queue")
        for plan in plans:
            self.tab._set_status(plan.path, "Queued")
        self.destroy()

    def _start(self):
        self._preview()
        plans = [p for p in self.plans if p.method]
        if not plans:
            return
        reason = self.tab.other_work_running()
        if reason:
            messagebox.showinfo("Repair", reason + " Use 'Add to Queue' instead.", parent=self)
            return
        if not self._confirm_unlabelled(plans):
            return
        if not messagebox.askyesno(
                "Start repairs",
                f"{len(plans)} file(s) will be changed as shown in the plan.\n\n"
                "Each file is written to a temporary copy, validated, and only then replaces the original "
                "(TrueHD: as chosen). Video is never re-encoded. This cannot be undone.\n\nContinue?",
                icon="warning", parent=self):
            return
        self._remember_prefs()
        options = self._repair_options()
        self.destroy()
        self.tab.start_repairs(plans, options)


class SettingsDialog(Toplevel):
    """Device profiles, thresholds, subtitle preferences, tools and per-rule switches."""

    def __init__(self, tab: LibraryScannerTab):
        super().__init__(tab.root)
        self.tab = tab
        self.title("Library Scanner - Rules & Devices")
        self.geometry("820x720")
        self.transient(tab.root)
        c = tab.config
        self.disabled = set(c.disabled_rules)

        dev = LabelFrame(self, text="Device profiles (device findings are potential problems, not file faults)",
                         padx=10, pady=6)
        dev.pack(fill="x", padx=10, pady=(10, 4))
        self.device_vars = {}
        for key, profile in DEVICE_PROFILES.items():
            var = BooleanVar(value=key in c.devices)
            self.device_vars[key] = var
            Checkbutton(dev, text=f"{profile.name} - {profile.description}", variable=var).pack(anchor="w")

        gen = LabelFrame(self, text="Checks and subtitle preferences", padx=10, pady=6)
        gen.pack(fill="x", padx=10, pady=4)
        row = Frame(gen)
        row.pack(fill="x")
        Label(row, text="Warn when a file has more than").pack(side="left")
        self.threshold_var = IntVar(value=c.subtitle_threshold)
        Spinbox(row, from_=0, to=200, width=5, textvariable=self.threshold_var).pack(side="left", padx=4)
        Label(row, text="subtitle tracks.   Keep languages:").pack(side="left")
        self.langs_var = StringVar(value=", ".join(c.keep_languages))
        Entry(row, textvariable=self.langs_var, width=14).pack(side="left", padx=4)
        row2 = Frame(gen)
        row2.pack(fill="x", pady=(4, 0))
        self.forced_var = BooleanVar(value=c.keep_forced)
        self.sdh_var = BooleanVar(value=c.keep_sdh)
        self.dup_var = BooleanVar(value=c.remove_duplicate_subs)
        self.bitstream_var = BooleanVar(value=c.bitstream_analysis)
        self.frames_var = BooleanVar(value=c.video_frame_check)
        for text, var in (("keep forced", self.forced_var), ("keep SDH", self.sdh_var),
                          ("remove duplicate tracks", self.dup_var),
                          ("Atmos bitstream analysis", self.bitstream_var),
                          ("HDR10+ frame check", self.frames_var)):
            Checkbutton(row2, text=text, variable=var).pack(side="left", padx=(0, 8))
        Label(row2, text="Files in parallel:").pack(side="left")
        self.workers_var = IntVar(value=c.workers)
        Spinbox(row2, from_=1, to=8, width=3, textvariable=self.workers_var).pack(side="left")

        tools = LabelFrame(self, text="Optional tools (used when found; ffprobe is always used)", padx=10, pady=6)
        tools.pack(fill="x", padx=10, pady=4)
        detected = discover_tools(tab.app.ffmpeg_path, tab.app.ffprobe_path)
        self.tool_vars = {}
        for key, label, found in (("mediainfo_path", "MediaInfo CLI", detected.mediainfo),
                                  ("mkvmerge_path", "MKVToolNix mkvmerge", detected.mkvmerge)):
            r = Frame(tools)
            r.pack(fill="x", pady=1)
            Label(r, text=f"{label}:", width=20, anchor="w").pack(side="left")
            var = StringVar(value=get_setting(key) or found or "")
            self.tool_vars[key] = var
            Entry(r, textvariable=var, width=60).pack(side="left", fill="x", expand=True)
            Button(r, text="Browse…", command=lambda v=var: self._browse_tool(v)).pack(side="left", padx=4)

        rules = LabelFrame(self, text="Rules (click a row to switch it on or off)", padx=6, pady=4)
        rules.pack(fill="both", expand=True, padx=10, pady=4)
        sb = Scrollbar(rules)
        sb.pack(side="right", fill="y")
        self.rules_tree = ttk.Treeview(rules, columns=("on", "rule", "category", "device", "description"),
                                       show="headings", yscrollcommand=sb.set, selectmode="none")
        for key, title, width in (("on", "On", 36), ("rule", "Rule", 160), ("category", "Category", 90),
                                  ("device", "Device", 150), ("description", "Description", 330)):
            self.rules_tree.heading(key, text=title)
            self.rules_tree.column(key, width=width, stretch=key == "description")
        self.rules_tree.pack(fill="both", expand=True)
        sb.config(command=self.rules_tree.yview)
        for rule_id, rule in RULES.items():
            device = DEVICE_PROFILES[rule.device].name if rule.device else "all"
            self.rules_tree.insert("", END, iid=rule_id, values=(
                "☐" if rule_id in self.disabled else "☑", rule_id, rule.category, device, rule.description))
        self.rules_tree.bind("<Button-1>", self._toggle_rule)

        buttons = Frame(self, pady=8)
        buttons.pack(fill="x", padx=10)
        Button(buttons, text="Save", command=self._save).pack(side="right")
        Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        Button(buttons, text="Restore defaults", command=self._defaults).pack(side="left")
        self.grab_set()

    def _browse_tool(self, var):
        path = filedialog.askopenfilename(parent=self, title="Select executable")
        if path:
            var.set(path)

    def _toggle_rule(self, event):
        row = self.rules_tree.identify_row(event.y)
        if not row:
            return "break"
        if row in self.disabled:
            self.disabled.discard(row)
        else:
            self.disabled.add(row)
        self.rules_tree.set(row, "on", "☐" if row in self.disabled else "☑")
        return "break"

    def _defaults(self):
        if messagebox.askyesno("Restore defaults", "Reset all scanner rules and preferences?", parent=self):
            self.tab.apply_config(ScannerConfig())
            self.destroy()

    def _save(self):
        try:
            threshold = int(self.threshold_var.get())
            workers = int(self.workers_var.get())
        except (ValueError, TypeError):
            messagebox.showerror("Settings", "Threshold and parallel files must be numbers.", parent=self)
            return
        langs = [normalize_language(x) for x in self.langs_var.get().replace(";", ",").split(",")]
        config = ScannerConfig(
            subtitle_threshold=max(0, threshold),
            keep_languages=[l for l in langs if l] or ["eng"],
            keep_forced=self.forced_var.get(), keep_sdh=self.sdh_var.get(),
            remove_duplicate_subs=self.dup_var.get(),
            devices=[k for k, v in self.device_vars.items() if v.get()],
            disabled_rules=sorted(self.disabled),
            bitstream_analysis=self.bitstream_var.get(), video_frame_check=self.frames_var.get(),
            workers=min(8, max(1, workers)),
        )
        for key, var in self.tool_vars.items():
            value = var.get().strip()
            set_setting(key, value or None)
        self.tab.apply_config(config)
        self.destroy()
