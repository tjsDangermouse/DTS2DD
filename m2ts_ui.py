"""Windows for the experimental MKV -> M2TS remux (Panasonic compatibility test).

M2TSDialog       one film: analysis, track choice, warnings, progress, report
M2TSBatchDialog  several films from the Library Scanner -> queue jobs

Widgets are only touched on the Tk main thread; analysis and remuxing run on
worker threads (results come back through ``root.after``).
"""
import os
import shutil
import threading
import webbrowser
from pathlib import Path
from tkinter import (
    BooleanVar, Button, Checkbutton, END, Entry, Frame, Label, LabelFrame, Radiobutton, Scrollbar, StringVar,
    Text, Toplevel, filedialog, messagebox, ttk,
)
from typing import List, Optional

from app_settings import get_setting, set_setting
from compatibility_rules import ScannerConfig
from ffmpeg_helper import ConversionCancelled, ProcessHandle, format_size
from m2ts_remux import (
    M2TSRemuxer, OutputExists, RemuxError, RemuxOptions, VerificationFailed, describe_plan, find_stale_partials,
)
from media_inspector import discover_tools
from media_probe import audio_streams_from_probe, format_seconds, subtitle_streams_from_probe
from scanner_ui import ReportDialog
from tsmuxer_tool import OFFICIAL_RELEASES_URL, SETTING_KEY, TsMuxerError, bundled_path, check_tsmuxer

EXPERIMENTAL_NOTE = ("Experimental: a container change for Panasonic DLNA playback tests. Video and audio are "
                     "copied, never re-encoded; the MKV is not changed. Playback on the Panasonic is NOT "
                     "confirmed by this app - test it on the player.")
GREEN, AMBER, RED, GREY = "#1b7f2a", "#b36b00", "#b00020", "#555555"


def check_tsmuxer_async(app, callback=None):
    """Re-check tsMuxeR on a worker; stores the result on ``app.tsmuxer_info``."""
    def worker():
        info = check_tsmuxer(get_setting(SETTING_KEY))
        app.tsmuxer_info = info

        def done():
            if callback:
                callback(info)
        app.root.after(0, done)
    threading.Thread(target=worker, daemon=True).start()


def offer_tsmuxer_download(parent) -> None:
    """Open the official release page - only after the user agrees. Nothing is downloaded by the app."""
    if messagebox.askyesno(
            "Get tsMuxeR",
            "The Windows installer includes tsMuxeR; this is only needed when running DTS2DD from source "
            "or on macOS. DTS2DD does not download it itself.\n\n"
            f"Open the official tsMuxer project's release page in your browser?\n{OFFICIAL_RELEASES_URL}\n\n"
            "Download the command-line build for your system (for Windows: tsMuxer-<version>-win64.zip), "
            "extract it, then press Browse… and choose tsMuxeR.exe.", parent=parent):
        webbrowser.open(OFFICIAL_RELEASES_URL)


class TsMuxerRow(Frame):
    """tsMuxeR status line with Browse / Re-check / Get tsMuxeR."""

    def __init__(self, parent, app, on_change):
        super().__init__(parent)
        self.app = app
        self.on_change = on_change
        Label(self, text="tsMuxeR:").pack(side="left")
        self.status = Label(self, text="checking…", fg=GREY, anchor="w", justify="left", wraplength=560)
        self.status.pack(side="left", padx=4, fill="x", expand=True)
        if not bundled_path():          # the Windows installer includes tsMuxeR
            Button(self, text="Get tsMuxeR…", command=lambda: offer_tsmuxer_download(self)).pack(side="right")
        Button(self, text="Re-check", command=self.recheck).pack(side="right", padx=2)
        Button(self, text="Browse…", command=self.browse).pack(side="right", padx=2)
        info = getattr(app, "tsmuxer_info", None)
        if info is None:
            self.recheck()
        else:
            self.show(info)

    def show(self, info):
        colour = GREEN if info.ok else RED
        where = f" - {info.path}" if info.path else ""
        self.status.config(text=f"{info.status}{where}\n{info.message}", fg=colour)

    def recheck(self):
        self.status.config(text="checking…", fg=GREY)

        def done(info):
            if self.winfo_exists():
                self.show(info)
                self.on_change(info)
        check_tsmuxer_async(self.app, done)

    def browse(self):
        types = [("tsMuxeR", "tsMuxeR.exe"), ("Executables", "*.exe")] if os.name == "nt" else [("All files", "*")]
        path = filedialog.askopenfilename(parent=self, title="Select the tsMuxeR executable", filetypes=types)
        if path:
            set_setting(SETTING_KEY, path)
            self.recheck()


def _text_box(parent, height=8) -> Text:
    holder = Frame(parent)
    holder.pack(fill="both", expand=True)
    sb = Scrollbar(holder)
    sb.pack(side="right", fill="y")
    text = Text(holder, height=height, wrap="word", state="disabled", yscrollcommand=sb.set)
    text.pack(fill="both", expand=True)
    sb.config(command=text.yview)
    text.tag_configure("warn", foreground=AMBER)
    text.tag_configure("bad", foreground=RED)
    text.tag_configure("ok", foreground=GREEN)
    text.tag_configure("h", font=("TkDefaultFont", 10, "bold"))
    return text


def _set_text(text: Text, chunks):
    text.config(state="normal")
    text.delete("1.0", END)
    for content, tag in chunks:
        text.insert(END, content, tag or ())
    text.config(state="disabled")


class M2TSDialog(Toplevel):
    """Remux one MKV to M2TS."""

    def __init__(self, app, path: str):
        super().__init__(app.root)
        self.app = app
        self.path = os.path.abspath(path)
        self.plan = None
        self.handle: Optional[ProcessHandle] = None
        self.running = False
        self.title(f"Remux to M2TS (Panasonic Compatibility) - {Path(path).name}")
        self.geometry("980x860")
        self.minsize(820, 680)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self._close)
        app.m2ts_dialogs.add(self)

        Label(self, text=EXPERIMENTAL_NOTE, fg=AMBER, justify="left", wraplength=940).pack(
            fill="x", padx=10, pady=(10, 4))
        self.tool_row = TsMuxerRow(self, app, lambda info: self._analyse())
        self.tool_row.pack(fill="x", padx=10, pady=2)

        top = LabelFrame(self, text="Input", padx=8, pady=4)
        top.pack(fill="x", padx=10, pady=4)
        Label(top, text=self.path, anchor="w", justify="left", wraplength=900).pack(fill="x")
        self.video_label = Label(top, text="Analysing…", anchor="w", justify="left", wraplength=900, fg=GREY)
        self.video_label.pack(fill="x")

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=10, pady=4)
        af = LabelFrame(panes, text="Audio tracks (click to keep / leave out)", padx=4, pady=2)
        sf = LabelFrame(panes, text="Subtitle tracks (click to keep / leave out)", padx=4, pady=2)
        panes.add(af, weight=3)
        panes.add(sf, weight=2)
        self.audio_tree = self._tree(af)
        self.subs_tree = self._tree(sf)
        sub_buttons = Frame(sf)
        sub_buttons.pack(fill="x")
        Button(sub_buttons, text="Compatible, kept languages", command=lambda: self._subs("preferred")).pack(side="left")
        Button(sub_buttons, text="All PGS", command=lambda: self._subs("all")).pack(side="left", padx=4)
        Button(sub_buttons, text="None", command=lambda: self._subs("none")).pack(side="left")

        settings = LabelFrame(self, text="Output", padx=8, pady=4)
        settings.pack(fill="x", padx=10, pady=4)
        row = Frame(settings)
        row.pack(fill="x")
        Label(row, text="First audio track:").pack(side="left")
        self.first_var = StringVar()
        self.first_combo = ttk.Combobox(row, textvariable=self.first_var, state="disabled", width=70)
        self.first_combo.pack(side="left", padx=4)
        self.first_combo.bind("<<ComboboxSelected>>", lambda e: self._first_changed())
        row2 = Frame(settings)
        row2.pack(fill="x", pady=(4, 0))
        Label(row2, text="Save as:").pack(side="left")
        self.output_var = StringVar(value=str(Path(self.path).with_suffix(".m2ts")))
        Entry(row2, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=4)
        Button(row2, text="Browse…", command=self._browse_output).pack(side="left")
        row3 = Frame(settings)
        row3.pack(fill="x", pady=(4, 0))
        self.full_var = BooleanVar(value=True)
        Checkbutton(row3, text="Compare the whole TrueHD / DTS-HD audio stream byte for byte "
                               "(reads both files completely; needed to call the audio 'verified')",
                    variable=self.full_var).pack(side="left")
        self.size_label = Label(settings, text="", fg=GREY, anchor="w")
        self.size_label.pack(fill="x")

        nf = LabelFrame(self, text="Compatibility warnings and report", padx=4, pady=2)
        nf.pack(fill="both", expand=True, padx=10, pady=4)
        self.notes = _text_box(nf, 9)

        self.progress = ttk.Progressbar(self, mode="indeterminate", maximum=100)
        self.progress.pack(fill="x", padx=10, pady=(4, 0))
        self.progress_label = Label(self, text="", fg=GREY, anchor="w")
        self.progress_label.pack(fill="x", padx=10)
        buttons = Frame(self, pady=8)
        buttons.pack(fill="x", padx=10)
        self.remux_button = Button(buttons, text="Remux to M2TS", command=self._start, state="disabled")
        self.remux_button.pack(side="right")
        self.queue_button = Button(buttons, text="Add to Queue", command=self._queue, state="disabled")
        self.queue_button.pack(side="right", padx=8)
        self.cancel_button = Button(buttons, text="Cancel remux", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right")
        Button(buttons, text="Close", command=self._close).pack(side="left")
        Button(buttons, text="Re-analyse", command=self._analyse).pack(side="left", padx=8)

        if getattr(app, "tsmuxer_info", None) is not None:
            self._analyse()

    # ---- widgets ---------------------------------------------------------

    def _tree(self, parent):
        holder = Frame(parent)
        holder.pack(fill="both", expand=True)
        sb = Scrollbar(holder)
        sb.pack(side="right", fill="y")
        tree = ttk.Treeview(holder, columns=("keep", "track", "m2ts"), show="headings", height=6,
                            selectmode="none", yscrollcommand=sb.set)
        for key, title, width in (("keep", "Keep", 44), ("track", "Track", 300), ("m2ts", "In M2TS", 260)):
            tree.heading(key, text=title)
            tree.column(key, width=width, stretch=key != "keep", anchor="center" if key == "keep" else "w")
        tree.tag_configure("bad", foreground=RED)
        tree.tag_configure("off", foreground=GREY)
        tree.pack(fill="both", expand=True)
        sb.config(command=tree.yview)
        tree.bind("<Button-1>", lambda e, t=tree: self._toggle(t, e))
        return tree

    def _fill(self):
        plan = self.plan
        for tree, tracks in ((self.audio_tree, plan.audio if plan else []),
                             (self.subs_tree, plan.subtitles if plan else [])):
            tree.delete(*tree.get_children())
            for t in tracks:
                text = t.handling + (f" - {t.atmos}" if t.atmos else "")
                tags = ("bad",) if not t.compatible else (() if t.selected else ("off",))
                tree.insert("", END, iid=str(t.index), tags=tags,
                            values=("☑" if t.selected else ("✗" if not t.compatible else "☐"), t.label, text))
            if not tracks:
                tree.insert("", END, values=("", "none", ""))
        self._refresh()

    def _toggle(self, tree, event):
        row = tree.identify_row(event.y)
        if self.running or not self.plan or not row.isdigit():
            return "break"
        t = next((x for x in self.plan.tracks if x.index == int(row) and x.kind != "video"), None)
        if t is None:
            return "break"
        if not t.compatible:
            messagebox.showinfo("Not supported in M2TS", f"{t.label}\n\n{t.handling}", parent=self)
            return "break"
        t.selected = not t.selected
        self._fill()
        return "break"

    def _subs(self, mode):
        if not self.plan or self.running:
            return
        from compatibility_rules import subtitle_cleanup_plan
        report = self.plan.report
        pgs = [s for s in report.subtitles
               if any(t.index == s.index and t.compatible and t.codec == "hdmv_pgs_subtitle"
                      for t in self.plan.subtitles)]
        keep = {s.index for s, _ in subtitle_cleanup_plan(pgs, self.app.scanner.config).keep} \
            if mode == "preferred" else set()
        for t in self.plan.subtitles:
            t.selected = t.compatible and (t.index in keep if mode == "preferred" else
                                           mode == "all" and t.codec == "hdmv_pgs_subtitle")
        self._fill()

    def _first_changed(self):
        if not self.plan:
            return
        audio = self.plan.selected("audio")
        i = self.first_combo.current()
        if 0 <= i < len(audio):
            self.plan.first_audio = audio[i].index
        self._refresh()

    def _browse_output(self):
        current = Path(self.output_var.get() or self.path)
        path = filedialog.asksaveasfilename(parent=self, title="Save M2TS as", defaultextension=".m2ts",
                                            initialdir=str(current.parent), initialfile=current.name,
                                            filetypes=[("M2TS files", "*.m2ts")], confirmoverwrite=False)
        if path:
            self.output_var.set(path)
            self._refresh()

    def _refresh(self, notes: bool = True):
        plan = self.plan
        if not plan:
            self.remux_button.config(state="disabled")
            self.queue_button.config(state="disabled")
            return
        audio = plan.selected("audio")
        values = [t.label for t in audio]
        self.first_combo.config(values=values, state="readonly" if values and not self.running else "disabled")
        if audio:
            if plan.first_audio not in [t.index for t in audio]:
                plan.first_audio = audio[0].index
            self.first_combo.current([t.index for t in audio].index(plan.first_audio))
        else:
            self.first_var.set("")
        out = self.output_var.get()
        try:
            free = shutil.disk_usage(os.path.dirname(os.path.abspath(out)) or ".").free
            free_text = f", {format_size(free)} free there"
        except OSError:
            free_text = " (output folder not found)"
        exists = " - THIS FILE EXISTS (you will be asked before it is replaced)" if os.path.exists(out) else ""
        self.size_label.config(text=f"Estimated M2TS size: about {format_size(plan.estimated_bytes())}"
                                    f"{free_text}{exists}", fg=AMBER if exists else GREY)
        problems = plan.selection_problems()
        chunks = []
        if problems:
            chunks.append(("Cannot remux:\n", "h"))
            chunks += [(f"  ✗ {p}\n", "bad") for p in problems]
        if plan.metadata_warnings:
            chunks.append(("\nVideo metadata (Dolby Vision / HDR) - you will be asked to confirm:\n", "h"))
            chunks += [(f"  ⚠ {w}\n", "warn") for w in plan.metadata_warnings]
        if plan.warnings:
            chunks.append(("\nWarnings:\n", "h"))
            chunks += [(f"  ⚠ {w}\n", "warn") for w in plan.warnings]
        excluded = [t for t, _ in plan.excluded() if t.compatible]
        if excluded:
            chunks.append(("\nCompatible tracks you left out:\n", "h"))
            chunks += [(f"  - {t.kind} {t.label}\n", None) for t in excluded]
        if plan.info:
            chunks.append(("\nNotes:\n", "h"))
            chunks += [(f"  • {n}\n", None) for n in plan.info]
        if notes:
            _set_text(self.notes, chunks)
        ready = not problems and not self.running and bool(getattr(self.app, "tsmuxer_info", None)
                                                            and self.app.tsmuxer_info.ok)
        self.remux_button.config(state="normal" if ready else "disabled")
        self.queue_button.config(state="normal" if not problems and not self.running else "disabled")

    # ---- analysis ---------------------------------------------------------

    def _remuxer(self, handle):
        tools = discover_tools(self.app.ffmpeg_path, self.app.ffprobe_path)
        return M2TSRemuxer(self.app.tsmuxer_info, self.app.ffmpeg_path, tools.ffprobe, tools,
                           log=self.app._log,
                           progress=lambda phase, f, d: self._ui(self._progress, phase, f, d), handle=handle)

    def _ui(self, func, *args):
        def run():
            if self.winfo_exists():
                func(*args)
        self.app.root.after(0, run)

    def _analyse(self):
        if self.running:
            return
        info = getattr(self.app, "tsmuxer_info", None)
        if not self.app.ffmpeg_path:
            self.video_label.config(text="FFmpeg is still being set up - try Re-analyse in a moment.", fg=RED)
            return
        if info is None or not info.ok:
            self.video_label.config(text="tsMuxeR is needed to read the tracks (see above).", fg=RED)
            return
        self.plan = None
        self._fill()
        self.video_label.config(text="Analysing (ffprobe, tsMuxeR, Atmos bitstream, HDR frames)…", fg=GREY)
        self._busy(True)
        handle = ProcessHandle()
        self.handle = handle
        config = self.app.scanner.config if self.app.scanner else ScannerConfig()

        def worker():
            try:
                plan = self._remuxer(handle).analyse(self.path, self.output_var.get(), config)
                self._ui(self._analysed, plan, None)
            except ConversionCancelled:
                self._ui(self._analysed, None, "Analysis cancelled")
            except (RemuxError, TsMuxerError, RuntimeError, OSError) as e:
                self._ui(self._analysed, None, str(e))
        threading.Thread(target=worker, daemon=True).start()

    def _analysed(self, plan, error):
        self._busy(False)
        self.plan = plan
        if error:
            self.video_label.config(text=f"✗ {error}", fg=RED)
            self._fill()
            return
        video = plan.selected("video")
        lines = [f"Video: {video[0].description}" if video else "Video: none usable"]
        hdr = plan.source_hdr
        if hdr:
            lines.append(f"Dolby Vision: {hdr.dovi_label()}")
            frame = ", ".join(sorted({k for keys in hdr.frames.values() for k in keys})) or "none"
            lines.append(f"HDR metadata in the video stream (sample frames): {frame}"
                         + (f"; in the MKV header: {', '.join(hdr.container)}" if hdr.container else ""))
        dur = plan.report.duration
        lines.append(f"Duration {format_seconds(dur)}; {len(plan.audio)} audio, {len(plan.subtitles)} subtitle "
                     f"track(s); tsMuxeR read {len(plan.detection.tracks)} track(s)")
        self.video_label.config(text="\n".join(lines), fg="#333333")
        stale = find_stale_partials(self.output_var.get())
        if stale and messagebox.askyesno(
                "Leftover temporary files",
                "These temporary files were left by an interrupted M2TS remux:\n\n"
                + "\n".join(Path(p).name for p in stale) + "\n\nDelete them now?", parent=self):
            for p in stale:
                try:
                    os.unlink(p)
                except OSError as e:
                    self.app._log(f"Could not delete {Path(p).name}: {e}")
        self._fill()

    # ---- running ----------------------------------------------------------

    def _busy(self, busy):
        self.running = busy
        self.cancel_button.config(state="normal" if busy else "disabled")
        if busy:
            self.progress.config(mode="indeterminate")
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.config(mode="determinate", value=0)
            self.progress_label.config(text="")
        self._refresh()

    def _progress(self, phase, fraction, detail):
        if fraction is None:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.config(mode="indeterminate")
                self.progress.start()
            text = phase
        else:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.config(mode="determinate")
            self.progress.config(value=fraction * 100)
            text = f"{phase}: {fraction * 100:.1f}%"
        self.progress_label.config(text=text + (f" • {detail}" if detail else ""))

    def _confirm(self) -> Optional[RemuxOptions]:
        plan = self.plan
        plan.output = os.path.abspath(self.output_var.get().strip())
        if not plan.output.lower().endswith(".m2ts"):
            messagebox.showerror("Remux to M2TS", "The output file name must end in .m2ts", parent=self)
            return None
        options = RemuxOptions(full_audio_compare=self.full_var.get())
        lines = describe_plan(plan)
        if plan.needs_confirmation:
            warn = plan.metadata_warnings + [w for w in plan.warnings if "left out" in w]
            if not messagebox.askyesno(
                    "Compatibility test - please confirm",
                    "M2TS remuxing may not preserve everything in this MKV:\n\n"
                    + "\n\n".join(f"• {w}" for w in warn[:8])
                    + "\n\nThe result is verified and every difference is reported. Proceed with the "
                      "compatibility test?", icon="warning", parent=self):
                return None
            options.accept_metadata_risk = True
        if os.path.exists(plan.output):
            if not messagebox.askyesno("Replace existing file?",
                                       f"{Path(plan.output).name} already exists.\n\nReplace it with the new M2TS "
                                       "(only after verification)? This cannot be undone.", icon="warning",
                                       parent=self):
                return None
            options.overwrite = True
        return options if messagebox.askyesno(
            "Remux to M2TS", "\n".join(lines[:30]) + "\n\nThe MKV is not changed. Start?", parent=self) else None

    def _start(self):
        if not self.plan or self.plan.selection_problems():
            return
        if self.app.busy or self.app.external_busy or any(d.running for d in self.app.m2ts_dialogs if d is not self):
            messagebox.showinfo("Remux to M2TS", "Another job is writing files (conversion, repair, queue or "
                                "remux). Use Add to Queue, or wait for it to finish.", parent=self)
            return
        options = self._confirm()
        if options is None:
            return
        handle = ProcessHandle()
        self.handle = handle
        self._busy(True)
        plan = self.plan
        self.app._log("========================================")
        self.app._log(f"M2TS remux: {Path(plan.source).name} -> {Path(plan.output).name}")

        def worker():
            try:
                result = self._remuxer(handle).remux(plan, options)
                self._ui(self._finished, result.report, None, result.output)
            except ConversionCancelled:
                self._ui(self._finished, None, "Remux cancelled. The MKV was not changed and the partial "
                                               "M2TS was deleted.", None)
            except VerificationFailed as e:
                self._ui(self._finished, e.report, str(e), None)
            except OutputExists as e:
                self._ui(self._finished, None, f"{e} Nothing was replaced.", None)
            except (RemuxError, TsMuxerError, RuntimeError, OSError) as e:
                self._ui(self._finished, None, str(e), None)
        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _finished(self, report, error, output):
        self._busy(False)          # refreshes the warnings box; the report replaces it below
        if report:
            self.app._log(report)
            _set_text(self.notes, [(report, "bad" if error else None)])
            ReportDialog(self, "M2TS remux report", report)
        if error:
            self.app._log(f"✗ {error}")
            messagebox.showerror("M2TS remux", error, parent=self)
        elif output:
            self.app._log(f"✓ M2TS saved: {output}")
        self._refresh(notes=not report)     # keep the report visible

    def _queue(self):
        plan = self.plan
        if not plan or plan.selection_problems():
            return
        from queue_jobs import m2ts_job
        options = self._confirm()
        if options is None:
            return
        audio_info = audio_streams_from_probe(plan.probe)
        subs_info = subtitle_streams_from_probe(plan.probe)
        chosen_audio = [a for a in audio_info if a.stream_index in {t.index for t in plan.selected("audio")}]
        chosen_subs = [s for s in subs_info if s.stream_index in {t.index for t in plan.selected("subtitle")}]
        first = next((a for a in audio_info if a.stream_index == plan.first_audio), None)
        try:
            job = m2ts_job(plan.source, output=plan.output, on_exists="overwrite" if options.overwrite else "skip",
                           audio=chosen_audio, subtitles=chosen_subs, all_audio=audio_info,
                           all_subtitles=subs_info, first_audio=first, full_verify=options.full_audio_compare,
                           accept_metadata_risk=options.accept_metadata_risk,
                           description=[Path(plan.source).name, *describe_plan(plan)])
        except OSError as e:
            messagebox.showerror("Add to Queue", str(e), parent=self)
            return
        self.app.queue_tab.add_jobs([job])
        self.app._log(f"Added to queue: {job.name} - {job.summary}")
        self._close()

    def _cancel(self):
        if self.handle and self.running and messagebox.askyesno(
                "Cancel", "Stop now? The MKV is not changed; the partial M2TS is deleted.", parent=self):
            threading.Thread(target=self.handle.cancel, daemon=True).start()

    def shutdown(self, wait: float = 15):
        """Stop tsMuxeR / verification and give the worker time to delete its temporary file."""
        if self.handle:
            self.handle.cancel()
        worker = getattr(self, "worker", None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=wait)

    def _close(self):
        if self.running:
            if not messagebox.askyesno("Remux running", "Stop the remux and close? The MKV is not changed.",
                                       parent=self):
                return
            self.shutdown()
        self.app.m2ts_dialogs.discard(self)
        self.destroy()


class M2TSBatchDialog(Toplevel):
    """Queue M2TS remux tests for several films (planned again when each job runs)."""

    def __init__(self, app, items):
        super().__init__(app.root)
        self.app = app
        self.items = [i for i in items if i.report.is_mkv and not i.report.errors]
        skipped = len(items) - len(self.items)
        self.title(f"Test M2TS Remux - {len(self.items)} film(s)")
        self.geometry("880x700")
        self.transient(app.root)
        Label(self, text=EXPERIMENTAL_NOTE, fg=AMBER, justify="left", wraplength=840).pack(fill="x", padx=10,
                                                                                            pady=(10, 4))
        self.tool_row = TsMuxerRow(self, app, lambda info: self._refresh())
        self.tool_row.pack(fill="x", padx=10)

        lf = LabelFrame(self, text="Films", padx=4, pady=2)
        lf.pack(fill="both", expand=True, padx=10, pady=4)
        self.list = _text_box(lf, 12)

        opts = LabelFrame(self, text="Options (audio: every compatible track; first audio = the MKV's default)",
                          padx=8, pady=4)
        opts.pack(fill="x", padx=10, pady=4)
        r = Frame(opts)
        r.pack(fill="x")
        Label(r, text="Subtitles:").pack(side="left")
        self.subs_var = StringVar(value="preferred")
        for text, value in (("compatible, kept languages", "preferred"), ("all PGS", "all"), ("none", "none")):
            Radiobutton(r, text=text, variable=self.subs_var, value=value).pack(side="left")
        r2 = Frame(opts)
        r2.pack(fill="x", pady=(4, 0))
        Label(r2, text="Save to:").pack(side="left")
        self.where_var = StringVar(value="beside")
        Radiobutton(r2, text="beside each film", variable=self.where_var, value="beside").pack(side="left")
        Radiobutton(r2, text="folder:", variable=self.where_var, value="folder").pack(side="left")
        self.folder_var = StringVar()
        Entry(r2, textvariable=self.folder_var, width=40).pack(side="left", fill="x", expand=True)
        Button(r2, text="Browse…", command=self._browse).pack(side="left", padx=4)
        r3 = Frame(opts)
        r3.pack(fill="x", pady=(4, 0))
        Label(r3, text="If the M2TS exists:").pack(side="left")
        self.exists_var = StringVar(value="skip")
        Radiobutton(r3, text="skip the film", variable=self.exists_var, value="skip").pack(side="left")
        Radiobutton(r3, text="save with a new name", variable=self.exists_var, value="rename").pack(side="left")
        self.full_var = BooleanVar(value=True)
        Checkbutton(r3, text="whole-stream audio comparison", variable=self.full_var).pack(side="left", padx=12)
        self.risk_var = BooleanVar(value=False)
        self.risk_check = Checkbutton(
            opts, variable=self.risk_var, command=self._refresh, justify="left", wraplength=820,
            text="I understand that Dolby Vision / HDR10+ metadata may not be fully preserved in M2TS. "
                 "Remux these films as a compatibility test (differences are verified and reported).")
        self.risk_check.pack(anchor="w", pady=(4, 0))

        buttons = Frame(self, pady=8)
        buttons.pack(fill="x", padx=10)
        self.add_button = Button(buttons, text="Add to Queue", command=self._queue)
        self.add_button.pack(side="right")
        Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.summary = Label(buttons, text="", fg=GREY)
        self.summary.pack(side="left")

        chunks = []
        self.risky = []
        for item in self.items:
            r = item.report
            video = next((v for v in r.video if not v.attached_pic), None)
            flags = []
            if video is not None and video.dv_profile is not None:
                flags.append(f"Dolby Vision P{video.dv_profile}")
            if video is not None and "HDR10+" in video.hdr_formats:
                flags.append("HDR10+")
            if flags:
                self.risky.append(item)
            audio = ", ".join(a.label for a in r.audio)
            chunks.append((f"{r.name}\n", "h"))
            chunks.append((f"   {video.label if video else 'no video'} | {audio}\n", None))
            unsupported = [a.format_name for a in r.audio if a.codec in ("flac", "opus", "vorbis")]
            unsupported += [s.codec for s in r.subtitles if s.codec in ("ass", "ssa", "dvd_subtitle")]
            if flags:
                chunks.append((f"   ⚠ {', '.join(flags)}: video metadata may change - checked after remuxing\n",
                               "warn"))
            if unsupported:
                chunks.append((f"   ⚠ left out (not supported in M2TS): {', '.join(sorted(set(unsupported)))}\n",
                               "warn"))
        if skipped:
            chunks.append((f"\n{skipped} selected file(s) are not readable MKVs and are not listed.\n", "bad"))
        _set_text(self.list, chunks)
        if not self.risky:
            self.risk_check.pack_forget()
        self._refresh()
        self.grab_set()

    def _browse(self):
        folder = filedialog.askdirectory(parent=self, title="Folder for the M2TS files")
        if folder:
            self.folder_var.set(folder)
            self.where_var.set("folder")

    def _refresh(self):
        info = getattr(self.app, "tsmuxer_info", None)
        ok = bool(self.items) and (not self.risky or self.risk_var.get())
        self.add_button.config(state="normal" if ok else "disabled")
        note = f"{len(self.items)} film(s) will be queued"
        if info is not None and not info.ok:
            note += " - tsMuxeR is not ready yet (jobs are skipped until it is)"
        if self.risky and not self.risk_var.get():
            note += f" - tick the confirmation ({len(self.risky)} with Dolby Vision / HDR10+)"
        self.summary.config(text=note)

    def _queue(self):
        from queue_jobs import m2ts_job
        folder = self.folder_var.get().strip() if self.where_var.get() == "folder" else None
        if self.where_var.get() == "folder" and (not folder or not os.path.isdir(folder)):
            messagebox.showerror("Test M2TS Remux", "Choose an existing folder.", parent=self)
            return
        jobs = []
        for item in self.items:
            try:
                jobs.append(m2ts_job(item.report.path, output_dir=folder, on_exists=self.exists_var.get(),
                                     subtitle_mode=self.subs_var.get(), full_verify=self.full_var.get(),
                                     accept_metadata_risk=self.risk_var.get(), config=self.app.scanner.config))
            except OSError as e:
                self.app.scanner.log(f"Not queued {item.report.name}: {e}")
        self.app.queue_tab.add_jobs(jobs)
        self.app.scanner.log(f"Added {len(jobs)} M2TS remux test(s) to the queue")
        for item in self.items:
            self.app.scanner._set_status(item.report.path, "M2TS queued")
        self.destroy()


def open_for_items(app, items: List) -> None:
    """Library Scanner entry point: one film -> full dialog, several -> batch."""
    if len(items) == 1:
        M2TSDialog(app, items[0].report.path)
    else:
        M2TSBatchDialog(app, items)
