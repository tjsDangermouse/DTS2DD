"""Main GUI application for DTS to AC-3 converter."""
import os
import sys
import threading
from pathlib import Path
from tkinter import (
    Tk, Frame, Label, Entry, Button, Text, messagebox, Radiobutton,
    filedialog, Scrollbar, StringVar, BooleanVar, Checkbutton, ttk, LabelFrame, END, BOTH
)
from tkinter import font as tkfont

from app_settings import get_setting, set_setting
from ffmpeg_downloader import ensure_ffmpeg_available, get_ffmpeg_path
from ffmpeg_helper import (
    ConversionCancelled, ProcessHandle, convert_and_append_audio, get_tracks, remove_streams_in_place,
)
from media_probe import find_ffprobe, format_seconds, parse_timecode
from truehd_encoder import (
    LAYOUT_LABELS, STATUS_MISSING, STATUS_NO_71, STATUS_NOT_VERIFIED, STATUS_READY,
    TrueHDError, check_encoder, create_channel_test_file,
)
from version import __version__
from scanner_ui import LibraryScannerTab
from m2ts_ui import M2TSDialog, check_tsmuxer_async
from queue_ui import QueueTab
from queue_jobs import convert_job, removal_job, truehd_job
from job_queue import same_path
from truehd_workflow import (
    TrueHDConverter, TrueHDOptions, find_stale_partials, output_name, source_warnings,
    truehd_target_layout, validate_source,
)

CODEC_AC3 = "AC-3 (Dolby Digital)"
CODEC_EAC3 = "E-AC-3 (Dolby Digital Plus)"
CODEC_TRUEHD = "Dolby TrueHD (Lossless)"

STATUS_COLOURS = {
    STATUS_READY: "#1b7f2a",
    STATUS_NO_71: "#b36b00",
    STATUS_NOT_VERIFIED: "#b36b00",
    STATUS_MISSING: "#b00020",
}


class ConverterApp:
    """Main application class."""

    def __init__(self, root):
        self.root = root
        self.root.title(f"DTS to AC-3 Converter {__version__}")
        self.root.geometry("1000x900")
        self.root.minsize(760, 640)
        self.ffmpeg_path = None
        self.ffprobe_path = None
        self.current_file = None
        self.audio_streams = []
        self.subtitle_streams = []
        self.remove_ticks = set()       # absolute stream indices ticked for removal
        self.status_label = None
        self.log_widget = None

        self.encoder_report = None
        self.encoder_checking = False
        self.busy = False
        self.external_busy = False      # a Library Scanner repair is writing files
        self.job_handle = None
        self.job_thread = None
        self.scanner = None
        self.queue_tab = None
        self.tsmuxer_info = None        # tsmuxer_tool.TsMuxerInfo once checked
        self.m2ts_dialogs = set()       # open M2TS remux windows

        self._create_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._initialize_ffmpeg()

    # ------------------------------------------------------------------
    # Thread helpers
    # ------------------------------------------------------------------

    def _ui(self, func, *args):
        """Run ``func`` on the Tk main thread (safe to call from workers)."""
        self.root.after(0, lambda: func(*args))

    def _initialize_ffmpeg(self):
        """Initialize FFmpeg on app startup."""
        def get_ffmpeg():
            try:
                self.ffmpeg_path = ensure_ffmpeg_available(progress_callback=self._log)
                self.ffprobe_path = find_ffprobe(self.ffmpeg_path)
                if not self.ffprobe_path:
                    self._log("Note: ffprobe not found - using basic audio detection")
                self.root.after(2000, lambda: self._update_status(""))
                self._ui(self._check_truehd_encoder)
                self._ui(check_tsmuxer_async, self)

            except RuntimeError as e:
                self._ui(self._show_error, str(e))

        thread = threading.Thread(target=get_ffmpeg, daemon=True)
        thread.start()

    def _log(self, message: str):
        """Add message to log viewer and status label."""
        self.root.after(0, lambda: self._update_log(message))
        self.root.after(0, lambda: self._update_status(message))

    def _update_log(self, message: str):
        """Update log widget (call from main thread via after)."""
        if self.log_widget and message:
            self.log_widget.config(state="normal")
            self.log_widget.insert(END, message + "\n")
            self.log_widget.see(END)  # Auto-scroll to bottom
            self.log_widget.config(state="disabled")

    def _update_status(self, message: str):
        """Update status label (call from main thread via after)."""
        if self.status_label:
            self.status_label.config(text=message.splitlines()[0] if message else "")
            if message:
                self.status_label.pack()
            else:
                self.status_label.pack_forget()

    def _show_error(self, error_msg: str):
        """Show error dialog (call from main thread via after)."""
        messagebox.showerror(
            "FFmpeg Error",
            f"Could not initialize FFmpeg:\n\n{error_msg}"
        )
        self.root.destroy()
        sys.exit(1)

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------

    def _create_widgets(self):
        """Create GUI widgets."""
        # Status label for FFmpeg initialization
        self.status_label = Label(self.root, text="", fg="blue")
        self.status_label.pack(pady=5)

        # Two tabs: the original single-file converter, and the Library Scanner.
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=BOTH, expand=True)
        main_frame = Frame(self.notebook, padx=10, pady=10)
        self.notebook.add(main_frame, text="Convert File")
        self.scanner = LibraryScannerTab(self.notebook, self)
        self.notebook.add(self.scanner.frame, text="Library Scanner")
        self.queue_tab = QueueTab(self.notebook, self)
        self.notebook.add(self.queue_tab.frame, text="Queue")
        self.queue_tab.refresh()

        # File selection frame
        file_frame = LabelFrame(main_frame, text="Input File", padx=10, pady=10)
        file_frame.pack(fill="x", pady=(0, 10))

        Label(file_frame, text="MKV File:").pack(side="left")
        self.file_entry = Entry(file_frame, width=50)
        self.file_entry.pack(side="left", padx=5)

        Button(
            file_frame, text="Browse",
            command=self._select_file
        ).pack(side="left")
        Button(
            file_frame, text="Remux to M2TS (Panasonic)…",
            command=self._open_m2ts
        ).pack(side="left", padx=(12, 0))
        Label(file_frame, text="experimental - new file, MKV unchanged", fg="#555555").pack(side="left", padx=4)

        # Audio and subtitle tracks side by side (same height as the old
        # audio list). Click a row to tick it for removal.
        tracks = ttk.PanedWindow(main_frame, orient="horizontal")
        tracks.pack(fill=BOTH, expand=True, pady=(0, 10))
        audio_frame = LabelFrame(tracks, text="Audio Streams (click a row to tick ☑ for removal)",
                                 padx=6, pady=2)
        # The "tick all" checkbox IS the frame title, so it costs no height.
        self.subs_all_var = BooleanVar(value=False)
        self.subs_all_check = Checkbutton(
            tracks, text="Subtitle Tracks - tick all for removal",
            variable=self.subs_all_var, command=self._toggle_all_subtitles, state="disabled",
            pady=0, bd=0, highlightthickness=0)
        subs_frame = LabelFrame(tracks, labelwidget=self.subs_all_check, padx=6, pady=2)
        tracks.add(audio_frame, weight=3)
        tracks.add(subs_frame, weight=2)
        self.audio_tree = self._make_track_tree(audio_frame)
        self.subs_tree = self._make_track_tree(subs_frame)

        # Conversion settings frame
        output_frame = LabelFrame(main_frame, text="Conversion Settings", padx=10, pady=10)
        output_frame.pack(fill="x", pady=(0, 10))
        output_frame.columnconfigure(1, weight=1)

        Label(output_frame, text="Convert audio track:").grid(row=0, column=0, sticky="w")
        self.track_var = StringVar()
        self.track_combo = ttk.Combobox(
            output_frame,
            textvariable=self.track_var,
            state="disabled",
            width=70,
        )
        self.track_combo.grid(row=0, column=1, sticky="w", padx=5)
        self.track_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_truehd_compat())

        Label(output_frame, text="Audio Codec:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.codec_var = StringVar(value=CODEC_AC3)
        codec_combo = ttk.Combobox(
            output_frame,
            textvariable=self.codec_var,
            values=[CODEC_AC3, CODEC_EAC3, CODEC_TRUEHD],
            state="readonly",
            width=25
        )
        codec_combo.grid(row=1, column=1, sticky="w", padx=5, pady=(10, 0))
        codec_combo.bind("<<ComboboxSelected>>", lambda e: self._on_codec_changed())

        Label(output_frame, text="Bitrate:").grid(row=2, column=0, sticky="w", pady=(10, 0))
        bitrate_cell = Frame(output_frame)
        bitrate_cell.grid(row=2, column=1, sticky="w", padx=5, pady=(10, 0))
        self.bitrate_var = StringVar(value="384k")
        self.bitrate_combo = ttk.Combobox(
            bitrate_cell,
            textvariable=self.bitrate_var,
            state="readonly",
            width=10
        )
        self.bitrate_combo.pack(side="left")
        self.lossless_label = Label(bitrate_cell, text="Lossless — automatic bitrate", fg="#555555")

        self._create_truehd_panel(output_frame)

        self.remove_source_var = BooleanVar(value=False)
        self.remove_source_check = Checkbutton(
            output_frame,
            text="Remove the original selected audio track after conversion",
            variable=self.remove_source_var,
            command=self._warn_about_removal,
        )
        self.remove_source_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.mode_note = Label(
            output_frame,
            text="The original MKV will be updated in place. All other streams are preserved.",
            fg="#555555",
        )
        self.mode_note.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._update_bitrate_options()

        # Progress bar
        self.progress = ttk.Progressbar(main_frame, mode="indeterminate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 2))
        self.progress_label = Label(main_frame, text="", fg="#555555", anchor="w")
        self.progress_label.pack(fill="x", pady=(0, 8))

        # Convert button
        button_frame = Frame(main_frame)
        button_frame.pack(fill="x")

        self.convert_button = Button(
            button_frame, text="Convert",
            command=self._start_conversion,
            state="disabled"
        )
        self.convert_button.pack(side="left", padx=5)

        self.queue_button = Button(
            button_frame, text="Add to Queue",
            command=self._queue_conversion,
            state="disabled"
        )
        self.queue_button.pack(side="left", padx=5)

        Button(
            button_frame, text="Detect Audio",
            command=self._detect_audio
        ).pack(side="left", padx=5)

        self.cancel_button = Button(
            button_frame, text="Cancel",
            command=self._cancel_job,
            state="disabled"
        )
        self.cancel_button.pack(side="left", padx=5)

        self.remove_tracks_button = Button(
            button_frame, text="Remove Ticked Tracks",
            command=self._start_track_removal,
            state="disabled"
        )
        self.remove_tracks_button.pack(side="left", padx=(20, 5))
        self.queue_removal_button = Button(
            button_frame, text="Queue Removal",
            command=self._queue_track_removal,
            state="disabled"
        )
        self.queue_removal_button.pack(side="left", padx=5)
        self.ticked_label = Label(button_frame, text="", fg="#b36b00", anchor="w")
        self.ticked_label.pack(side="left", padx=5, fill="x", expand=True)

        # Log viewer frame
        log_frame = LabelFrame(main_frame, text="Activity Log", padx=10, pady=10)
        log_frame.pack(fill=BOTH, expand=True, pady=(10, 0))

        log_scrollbar = Scrollbar(log_frame)
        log_scrollbar.pack(side="right", fill="y")

        self.log_widget = Text(log_frame, height=6, state="disabled", yscrollcommand=log_scrollbar.set)
        self.log_widget.pack(fill=BOTH, expand=True)
        log_scrollbar.config(command=self.log_widget.yview)

    def _make_track_tree(self, parent):
        """Track list with a tick column; clicking a row toggles removal."""
        scrollbar = Scrollbar(parent)
        scrollbar.pack(side="right", fill="y")
        # No heading row and compact rows, so the lists take no more height
        # than the old 4-line audio text box.
        style = ttk.Style()
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Tracks.Treeview", rowheight=line + 2)
        tree = ttk.Treeview(parent, columns=("remove", "num", "desc"), show="", style="Tracks.Treeview",
                            height=4, selectmode="none", yscrollcommand=scrollbar.set)
        tree.column("remove", width=28, minwidth=28, stretch=False, anchor="center")
        tree.column("num", width=40, minwidth=40, stretch=False, anchor="w")
        tree.column("desc", width=200, stretch=True, anchor="w")
        tree.tag_configure("ticked", foreground="#b00020")
        tree.pack(fill=BOTH, expand=True)
        scrollbar.config(command=tree.yview)
        tree.bind("<Button-1>", lambda e, t=tree: self._toggle_tick(t, e))
        return tree

    def _toggle_tick(self, tree, event):
        row = tree.identify_row(event.y)
        if not row or self.busy or not row.isdigit():
            return "break"
        index = int(row)
        if index in self.remove_ticks:
            self.remove_ticks.discard(index)
        else:
            self.remove_ticks.add(index)
        self._render_track_row(tree, index)
        self._refresh_convert_state()
        return "break"

    def _render_track_row(self, tree, index):
        ticked = index in self.remove_ticks
        tree.set(str(index), "remove", "☑" if ticked else "☐")
        tree.item(str(index), tags=("ticked",) if ticked else ())

    def _fill_track_trees(self):
        for tree, items in ((self.audio_tree, self.audio_streams), (self.subs_tree, self.subtitle_streams)):
            tree.delete(*tree.get_children())
            for item in items:
                text = str(item)
                prefix = f"Stream #{item.stream_index}: "
                if text.startswith(prefix):
                    text = text[len(prefix):]
                tree.insert("", END, iid=str(item.stream_index), values=("☐", f"#{item.stream_index}", text))
                self._render_track_row(tree, item.stream_index)
        if not self.subtitle_streams:
            self.subs_tree.insert("", END, values=("", "", "No subtitle tracks"))

    def _ticked(self):
        """(audio, subtitle) streams currently ticked for removal."""
        return ([s for s in self.audio_streams if s.stream_index in self.remove_ticks],
                [s for s in self.subtitle_streams if s.stream_index in self.remove_ticks])

    def _toggle_all_subtitles(self):
        """Tick or untick every subtitle track."""
        if self.busy:
            return
        indices = {s.stream_index for s in self.subtitle_streams}
        if self.subs_all_var.get():
            self.remove_ticks |= indices
        else:
            self.remove_ticks -= indices
        for index in indices:
            self._render_track_row(self.subs_tree, index)
        self._refresh_convert_state()

    MAX_LISTED_TRACKS = 8

    def _describe_tracks(self, audio, subtitles):
        """Bullet list for confirmation dialogs (films can have 40+ subtitles)."""
        lines = [f"  • Audio #{s.stream_index}: {s}".replace(f"Stream #{s.stream_index}: ", "")
                 for s in audio]
        if subtitles and len(subtitles) == len(self.subtitle_streams) and len(subtitles) > 1:
            lines.append(f"  • All {len(subtitles)} subtitle tracks")
        else:
            lines += [f"  • Subtitle #{s.stream_index}: {s}" for s in subtitles]
        if len(lines) > self.MAX_LISTED_TRACKS:
            hidden = len(lines) - (self.MAX_LISTED_TRACKS - 1)
            lines = lines[:self.MAX_LISTED_TRACKS - 1] + [f"  ... and {hidden} more tracks"]
        return "\n".join(lines)

    def _progress_reporter(self, phase):
        """on_progress callback for run_ffmpeg: % done, speed, time left, disk rate."""
        def report(p):
            detail = f"{format_seconds(p.elapsed)} elapsed"
            if p.speed:
                detail += f" • {p.speed:.1f}x"
            if p.eta is not None:
                detail += f" • ~{format_seconds(p.eta)} remaining"
            if p.write_rate:
                detail += f" • {p.write_rate / 1e6:.0f} MB/s"
            self._ui(self._show_progress, phase, p.fraction, detail)
        return report

    def _create_truehd_panel(self, parent):
        """TrueHD-only settings (shown when Dolby TrueHD is selected)."""
        panel = Frame(parent)
        self.truehd_panel = panel

        Label(
            panel, justify="left", fg="#333333",
            text=("Output format: Dolby TrueHD    Compression: Lossless    Encoder: LibreMPEG\n"
                  "Channels: Preserve original    Sample rate: Preserve original    "
                  "Bit depth: Preserve where supported"),
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(8, 4))

        Label(panel, text="Encoder status:").grid(row=1, column=0, sticky="w")
        self.encoder_status_label = Label(panel, text="Checking...", fg="#555555")
        self.encoder_status_label.grid(row=1, column=1, sticky="w", padx=5)
        buttons = Frame(panel)
        buttons.grid(row=1, column=2, columnspan=2, sticky="w")
        Button(buttons, text="Browse...", command=self._browse_encoder).pack(side="left", padx=2)
        Button(buttons, text="Re-check", command=self._check_truehd_encoder).pack(side="left", padx=2)
        self.speaker_test_button = Button(buttons, text="Speaker test file...",
                                          command=self._create_speaker_test, state="disabled")
        self.speaker_test_button.pack(side="left", padx=2)

        self.encoder_detail_label = Label(panel, text="", fg="#555555", justify="left",
                                          wraplength=760, anchor="w")
        self.encoder_detail_label.grid(row=2, column=0, columnspan=4, sticky="w")
        self.compat_label = Label(panel, text="", justify="left", wraplength=760, anchor="w")
        self.compat_label.grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))

        self.make_default_var = BooleanVar(value=False)
        Checkbutton(panel, text="Make converted audio the default track",
                    variable=self.make_default_var).grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.strict_verify_var = BooleanVar(value=True)
        Checkbutton(panel, text="Strict lossless verification (decode both tracks and compare every sample)",
                    variable=self.strict_verify_var).grid(row=5, column=0, columnspan=4, sticky="w")

        output_row = Frame(panel)
        output_row.grid(row=6, column=0, columnspan=4, sticky="w", pady=(4, 0))
        Label(output_row, text="Output:").pack(side="left")
        self.output_mode_var = StringVar(value="new")
        Radiobutton(output_row, text="Save as new MKV (…_Sound_TrueHD.mkv)",
                    variable=self.output_mode_var, value="new").pack(side="left", padx=4)
        Radiobutton(output_row, text="Replace original after verification (asks first)",
                    variable=self.output_mode_var, value="replace").pack(side="left", padx=4)

        clip_row = Frame(panel)
        clip_row.grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.test_clip_var = BooleanVar(value=False)
        Checkbutton(clip_row, text="Create short test conversion", variable=self.test_clip_var,
                    command=self._update_clip_state).pack(side="left")
        Label(clip_row, text="Length:").pack(side="left", padx=(10, 2))
        self.clip_length_var = StringVar(value="60 s")
        self.clip_length_combo = ttk.Combobox(clip_row, textvariable=self.clip_length_var,
                                              values=["30 s", "60 s", "120 s"], state="disabled", width=6)
        self.clip_length_combo.pack(side="left")
        Label(clip_row, text="Start at (h:mm:ss):").pack(side="left", padx=(10, 2))
        self.clip_start_var = StringVar(value="0:00:00")
        self.clip_start_entry = Entry(clip_row, textvariable=self.clip_start_var, width=9, state="disabled")
        self.clip_start_entry.pack(side="left")

    # ------------------------------------------------------------------
    # File / stream selection
    # ------------------------------------------------------------------

    def _select_file(self):
        """Open file dialog to select MKV file."""
        file_path = filedialog.askopenfilename(
            filetypes=[("MKV files", "*.mkv"), ("All files", "*.*")]
        )
        if file_path:
            self.current_file = file_path
            self.file_entry.delete(0, END)
            self.file_entry.insert(0, file_path)

            # Log and auto-detect audio
            filename = Path(file_path).name
            self._log(f"File selected: {filename}")
            self._offer_partial_cleanup(file_path)
            self._detect_audio()

    def _offer_partial_cleanup(self, file_path):
        """Offer to delete temporary files left by an interrupted conversion."""
        if self.busy:
            return  # never offer to delete the running job's temporary file
        stale = find_stale_partials(file_path)
        if not stale:
            return
        names = "\n".join(Path(p).name for p in stale)
        if messagebox.askyesno(
            "Leftover temporary files",
            "These temporary files were left by an interrupted conversion "
            f"(the original MKV was not modified):\n\n{names}\n\nDelete them now?"
        ):
            for path in stale:
                try:
                    os.unlink(path)
                    self._log(f"Deleted leftover temporary file: {Path(path).name}")
                except OSError as e:
                    self._log(f"Could not delete {Path(path).name}: {e}")

    def _is_truehd(self) -> bool:
        return self.codec_var.get() == CODEC_TRUEHD

    def _on_codec_changed(self):
        self._update_bitrate_options()
        self._refresh_truehd_compat()

    def _update_bitrate_options(self):
        """Update available bitrates based on selected codec."""
        codec = self.codec_var.get()
        if codec == CODEC_TRUEHD:
            self.bitrate_combo.config(state="disabled")
            self.lossless_label.pack(side="left", padx=8)
            self.truehd_panel.grid(row=3, column=0, columnspan=2, sticky="w")
            self.mode_note.config(text="TrueHD saves a new MKV by default. The original is only "
                                       "replaced after verification and your confirmation.")
            return

        self.lossless_label.pack_forget()
        self.truehd_panel.grid_remove()
        self.bitrate_combo.config(state="readonly")
        self.mode_note.config(text="The original MKV will be updated in place. All other streams are preserved.")
        if "E-AC-3" in codec:
            # E-AC-3 supports higher bitrates and is more efficient
            bitrates = ["192k", "224k", "256k", "320k", "384k", "448k", "512k", "640k", "768k"]
            if self.bitrate_var.get() not in bitrates:
                self.bitrate_var.set("384k")
        else:
            # AC-3 standard bitrates
            bitrates = ["192k", "224k", "256k", "320k", "384k", "448k", "640k"]
            if self.bitrate_var.get() not in bitrates:
                self.bitrate_var.set("384k")

        self.bitrate_combo.config(values=bitrates)

    def _update_clip_state(self):
        state = "readonly" if self.test_clip_var.get() else "disabled"
        self.clip_length_combo.config(state=state)
        self.clip_start_entry.config(state="normal" if self.test_clip_var.get() else "disabled")

    def _selected_stream(self):
        index = self.track_combo.current()
        if 0 <= index < len(self.audio_streams):
            return self.audio_streams[index]
        return None

    def _truehd_problems(self):
        stream = self._selected_stream()
        if stream is None:
            return ["Choose an audio track to convert."]
        if self.encoder_checking:
            return ["The TrueHD encoder is still being checked."]
        return validate_source(stream, self.encoder_report)

    def _refresh_truehd_compat(self):
        """Show whether the selected track can be converted to TrueHD."""
        if self._is_truehd():
            stream = self._selected_stream()
            problems = self._truehd_problems()
            if stream is None:
                self.compat_label.config(text="", fg="#333333")
            elif problems:
                self.compat_label.config(text="✗ " + problems[0], fg="#b00020")
            else:
                label = LAYOUT_LABELS[truehd_target_layout(stream)]
                text = (f"✓ {stream.format_name} {label} "
                        f"({stream.channel_count} ch, {stream.sample_rate} Hz, "
                        f"{stream.bit_depth or '?'}-bit) → Dolby TrueHD "
                        f"{label}, lossless")
                for warning in source_warnings(stream):
                    text += f"\n⚠ {warning}"
                self.compat_label.config(text=text, fg="#1b7f2a")
        self._refresh_convert_state()

    def set_external_busy(self, busy: bool = False):
        """Called by the Library Scanner / Queue when their jobs start or stop writing files."""
        self.external_busy = bool((self.scanner and self.scanner.repairing)
                                  or (self.queue_tab and self.queue_tab.running))
        self._refresh_convert_state()

    def job_finished(self, path: str):
        """A queued job changed ``path``: refresh this tab and the scanner row (main thread)."""
        if self.current_file and same_path(self.current_file, path) and not self.busy:
            self._detect_audio()
        if self.scanner and path in self.scanner.items:
            tools = self.scanner._tools()
            threading.Thread(target=self.scanner._rescan, args=(path, "Queued job finished", tools),
                             daemon=True).start()

    def _refresh_convert_state(self):
        ready = not self.busy and not self.external_busy and self._selected_stream() is not None
        if ready and self._is_truehd():
            ready = not self._truehd_problems()
        self.convert_button.config(state="normal" if ready else "disabled")
        # Queuing never touches the file, so it is allowed while other work runs.
        queueable = self._selected_stream() is not None and not (self._is_truehd() and self._truehd_problems())
        self.queue_button.config(state="normal" if queueable else "disabled")
        audio, subtitles = self._ticked()
        count = len(audio) + len(subtitles)
        self.remove_tracks_button.config(state="normal" if count and not self.busy and not self.external_busy
                                         else "disabled")
        self.queue_removal_button.config(state="normal" if count else "disabled")
        subs = self.subtitle_streams
        self.subs_all_var.set(bool(subs) and len(subtitles) == len(subs))
        self.subs_all_check.config(state="normal" if subs and not self.busy else "disabled")
        if count:
            parts = []
            if audio:
                parts.append(f"{len(audio)} audio")
            if subtitles:
                parts.append(f"{len(subtitles)} subtitle")
            self.ticked_label.config(text=f"Ticked for removal: {' + '.join(parts)} "
                                          "(also applied when converting)")
        else:
            self.ticked_label.config(text="")

    def _detect_audio(self):
        """Detect audio streams in the selected file."""
        if not self.current_file:
            messagebox.showwarning("Warning", "Please select an MKV file first")
            return

        if not self.ffmpeg_path:
            messagebox.showerror("Error", "FFmpeg not initialized")
            return

        self.audio_tree.delete(*self.audio_tree.get_children())
        self.subs_tree.delete(*self.subs_tree.get_children())
        self._log("Detecting audio and subtitle tracks...")

        def detect():
            try:
                streams, subtitles = get_tracks(self.current_file, self.ffmpeg_path, self.ffprobe_path)

                # Update audio display
                self.root.after(0, lambda: self._update_audio_display(streams, subtitles))
                self._log(f"Found {len(streams)} audio stream(s), {len(subtitles)} subtitle track(s)")

            except RuntimeError as e:
                self._ui(self._update_audio_display_error, str(e))
                self._log(f"Error: {str(e)}")

        thread = threading.Thread(target=detect, daemon=True)
        thread.start()

    def _update_audio_display(self, streams, subtitles=()):
        """Update audio/subtitle track lists on main thread."""
        self.audio_streams = streams
        self.subtitle_streams = list(subtitles)
        self.remove_ticks = set()
        self._fill_track_trees()
        if streams:
            self.track_combo.config(values=[str(stream) for stream in streams], state="readonly")
            # Pre-select the first DTS-HD MA track when one exists.
            preferred = next((i for i, s in enumerate(streams)
                              if s.format_name and s.format_name.startswith(("DTS-HD Master", "DTS:X"))), 0)
            self.track_combo.current(preferred)
        else:
            self.audio_tree.insert("", END, values=("", "", "No audio streams found"))
            self.track_combo.config(values=(), state="disabled")
            self.track_var.set("")
        self._refresh_truehd_compat()

    def _update_audio_display_error(self, error_msg):
        """Update audio display with error message on main thread."""
        self.audio_streams = []
        self.subtitle_streams = []
        self.remove_ticks = set()
        self._fill_track_trees()
        self.subs_tree.delete(*self.subs_tree.get_children())
        self.audio_tree.insert("", END, values=("", "", f"Error: {error_msg}"))
        self.track_combo.config(values=(), state="disabled")
        self.track_var.set("")
        self._refresh_truehd_compat()

    def _warn_about_removal(self):
        """Explain the consequence of removing the source audio track."""
        if self.remove_source_var.get():
            if self._is_truehd():
                messagebox.showwarning(
                    "Original track will be removed",
                    "The selected original audio track will not be included in the new MKV.\n\n"
                    "Your source MKV is not modified unless you also choose to replace it "
                    "and confirm after verification."
                )
                return
            messagebox.showwarning(
                "Original track will be removed",
                "The selected original audio track will be removed from the updated MKV "
                "after its converted replacement is added.\n\n"
                "The app writes a temporary file first, but once conversion succeeds the "
                "source MKV is replaced. Keep this option off to retain the original track."
            )

    # ------------------------------------------------------------------
    # TrueHD encoder configuration
    # ------------------------------------------------------------------

    def _check_truehd_encoder(self):
        if self.encoder_checking or self.busy:
            return
        self.encoder_checking = True
        self.encoder_status_label.config(text="Checking encoder...", fg="#555555")
        self.speaker_test_button.config(state="disabled")
        self._refresh_truehd_compat()
        configured = get_setting("truehd_encoder_path")

        def worker():
            try:
                report = check_encoder(configured, reference_decoder=self.ffmpeg_path, log=self._log)
            except Exception as e:  # never leave the UI stuck in "checking"
                report = None
                self._log(f"TrueHD encoder check failed: {e}")
            self._ui(self._apply_encoder_report, report)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_encoder_report(self, report):
        self.encoder_checking = False
        self.encoder_report = report
        if report is None:
            self.encoder_status_label.config(text=STATUS_NOT_VERIFIED, fg=STATUS_COLOURS[STATUS_NOT_VERIFIED])
            self.encoder_detail_label.config(text="The encoder check did not complete.")
        else:
            self.encoder_status_label.config(text=report.status, fg=STATUS_COLOURS.get(report.status, "#555555"))
            location = f"{report.path} ({report.origin})" if report.path else "not configured"
            self.encoder_detail_label.config(text=f"{location}\n{report.message}")
            self._log(f"{report.status}: {report.message}")
            if not self.ffprobe_path:
                self.ffprobe_path = find_ffprobe(report.path)
                if self.ffprobe_path and self.current_file:
                    self._detect_audio()   # re-probe with full stream details
            if report.can_encode("7.1"):
                self.speaker_test_button.config(state="normal")
        self._refresh_truehd_compat()

    def _browse_encoder(self):
        filetypes = [("Executables", "*.exe"), ("All files", "*.*")] if os.name == "nt" else [("All files", "*")]
        path = filedialog.askopenfilename(title="Select LibreMPEG ffmpeg executable", filetypes=filetypes)
        if not path:
            return
        set_setting("truehd_encoder_path", path)
        self._log(f"TrueHD encoder set to: {path}")
        self._check_truehd_encoder()

    def _create_speaker_test(self):
        report = self.encoder_report
        if not report or not report.can_encode("7.1"):
            return
        path = filedialog.asksaveasfilename(
            title="Save TrueHD 7.1 speaker test", defaultextension=".mkv",
            initialfile="TrueHD_7.1_Speaker_Test.mkv", filetypes=[("MKV files", "*.mkv")])
        if not path:
            return
        if os.path.exists(path):
            # The save dialog already asked to overwrite; still never overwrite silently.
            if not messagebox.askyesno("Replace file", f"{Path(path).name} exists. Replace it?"):
                return
            os.unlink(path)
        self._set_busy(True, indeterminate=True)
        self._log("Creating TrueHD 7.1 speaker identification test file...")

        def worker():
            try:
                create_channel_test_file(report.path, path, video_ffmpeg=self.ffmpeg_path, log=self._log)
                self._ui(messagebox.showinfo, "Speaker test created",
                         f"Created and verified:\n{path}\n\nEach speaker plays in turn: "
                         "1 pip Front Left, 2 Centre, 3 Front Right, 4 Surround Right, "
                         "5 Surround Back Right, 6 Surround Back Left, 7 Surround Left, "
                         "8 Subwoofer (LFE). Subtitles name each speaker.")
            except (TrueHDError, OSError) as e:
                self._log(f"Error: {e}")
                self._ui(messagebox.showerror, "Speaker test failed", str(e))
            finally:
                self._ui(self._set_busy, False)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def _set_busy(self, busy, indeterminate=True):
        self.busy = busy
        self.cancel_button.config(state="normal" if busy and self.job_handle else "disabled")
        if busy:
            self.progress.config(mode="indeterminate" if indeterminate else "determinate", value=0)
            if indeterminate:
                self.progress.start()
        else:
            self.progress.stop()
            self.progress.config(mode="determinate", value=0)
            self.progress_label.config(text="")
            self.job_handle = None
        self._refresh_convert_state()

    def _show_progress(self, phase, fraction, detail):
        """Progress update from a worker (runs on main thread)."""
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
        if detail:
            text += f" • {detail}"
        self.progress_label.config(text=text)

    def _cancel_job(self):
        if self.job_handle and messagebox.askyesno("Cancel", "Cancel the running conversion?\n\n"
                                                   "The original MKV will not be changed."):
            self._log("Cancelling...")
            threading.Thread(target=self.job_handle.cancel, daemon=True).start()

    def _open_m2ts(self):
        path = self.file_entry.get().strip() or self.current_file
        if not path or not os.path.isfile(path):
            messagebox.showwarning("Remux to M2TS", "Select an MKV file first")
            return
        if not self.ffmpeg_path:
            messagebox.showerror("Remux to M2TS", "FFmpeg is still being set up. Try again in a moment.")
            return
        M2TSDialog(self, path)

    def _on_close(self):
        running = [d for d in self.m2ts_dialogs if d.running]
        if running:
            if not messagebox.askyesno("M2TS remux running",
                                       "An M2TS remux is running. Stop it and quit?\n\n"
                                       "The MKV is not changed; the partial M2TS is deleted."):
                return
            for d in running:
                d.shutdown()
        if self.queue_tab and self.queue_tab.running:
            if not messagebox.askyesno("Queue running",
                                       "The queue is running. Stop it and quit?\n\n"
                                       "The film being processed is left unchanged; the other jobs stay "
                                       "queued for next time."):
                return
            self.queue_tab.shutdown()
        if self.scanner and self.scanner.busy:
            if not messagebox.askyesno("Library Scanner running",
                                       "A library scan or repair is running. Stop it and quit?\n\n"
                                       "A file being repaired is left unchanged."):
                return
            self.scanner.shutdown()
        if self.busy and self.job_handle:
            if not messagebox.askyesno("Conversion running",
                                       "A conversion is running. Cancel it and quit?\n\n"
                                       "The original MKV will not be changed."):
                return
            self.job_handle.cancel()     # stops FFmpeg
            if self.job_thread:
                self.job_thread.join(timeout=15)   # let the worker delete its temp file
        self.root.destroy()

    def _start_conversion(self):
        """Start the conversion process."""
        input_path = self.file_entry.get()

        if not input_path:
            messagebox.showwarning("Warning", "Please select an MKV file")
            return

        if not self.ffmpeg_path:
            messagebox.showerror("Error", "FFmpeg not initialized")
            return

        # Validate paths
        if not os.path.exists(input_path):
            messagebox.showerror("Error", f"Input file not found: {input_path}")
            return

        selected_track = self.track_combo.current()
        if selected_track < 0 or selected_track >= len(self.audio_streams):
            messagebox.showwarning("Warning", "Choose an audio track to convert")
            return

        if self._is_truehd():
            self._start_truehd_conversion(input_path, self.audio_streams[selected_track])
            return

        bitrate = self.bitrate_var.get()
        codec_choice = self.codec_var.get()
        codec = "eac3" if "E-AC-3" in codec_choice else "ac3"
        source_stream = self.audio_streams[selected_track]
        ticked_audio, ticked_subs = self._ticked()
        remove_source = self.remove_source_var.get() or source_stream in ticked_audio
        other_audio = [s for s in ticked_audio if s is not source_stream]
        remove_streams = [s.stream_index for s in other_audio + ticked_subs]

        action = "replace the selected original audio track" if remove_source else "keep the selected original audio track and append a converted copy"
        extra = ""
        if remove_streams:
            extra = ("\n\nThese ticked tracks will also be removed:\n"
                     + self._describe_tracks(other_audio, ticked_subs))
        if not messagebox.askyesno(
            "Update source MKV",
            f"This will update the selected MKV in place. It will {action}.{extra}\n\n"
            "A temporary MKV is created first and replaces the source only after success. "
            "This cannot be undone. Continue?"
        ):
            return

        self.job_handle = ProcessHandle()
        handle = self.job_handle
        self._set_busy(True)

        input_file = Path(input_path).name
        self._log(f"========================================")
        self._log(f"Starting conversion")
        self._log(f"  Input: {input_file}")
        self._log(f"  Selected track: {source_stream}")
        self._log(f"  Codec: {codec_choice}")
        self._log(f"  Bitrate: {bitrate}")
        self._log(f"  Remove original: {'Yes' if remove_source else 'No'}")
        if remove_streams:
            self._log("  Also removing tracks: " + ", ".join(f"#{i}" for i in remove_streams))
        self._log(f"  Status: Processing...")
        self._log(f"========================================")

        def convert():
            try:
                result = convert_and_append_audio(
                    input_path,
                    bitrate,
                    self.ffmpeg_path,
                    source_stream.stream_index,
                    len(self.audio_streams),
                    codec=codec,
                    remove_source_stream=remove_source,
                    progress_callback=self._log,
                    handle=handle,
                    remove_streams=remove_streams,
                    removed_other_audio=len(other_audio),
                    on_progress=self._progress_reporter("Converting"),
                )

                if result:
                    self._log(f"========================================")
                    self._log("✓ Conversion completed successfully!")
                    self._log(f"  Updated: {input_file}")
                    self._log(f"========================================")
                    self._ui(messagebox.showinfo, "Success",
                             f"Conversion completed successfully.\n\nUpdated MKV:\n{input_path}")
                    self._ui(self._detect_audio)
                else:
                    self._log("✗ Conversion failed")
                    self._ui(messagebox.showerror, "Error", "Conversion failed")

            except ConversionCancelled:
                self._log("Conversion cancelled - the original MKV was not changed")
            except RuntimeError as e:
                self._log(f"Error: {str(e)}")
                self._ui(messagebox.showerror, "Conversion Error", str(e))
            finally:
                self._ui(self._set_busy, False)

        self.job_thread = threading.Thread(target=convert, daemon=True)
        self.job_thread.start()

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def _queue_conversion(self):
        """Queue what Convert would do now, including ticked track removal."""
        input_path = self.current_file
        stream = self._selected_stream()
        if not input_path or not os.path.exists(input_path) or stream is None:
            messagebox.showwarning("Add to Queue", "Select an MKV file and an audio track first")
            return
        ticked_audio, ticked_subs = self._ticked()
        removes = ticked_audio + ticked_subs
        remove_source = self.remove_source_var.get() or stream in ticked_audio
        try:
            if self._is_truehd():
                problems = self._truehd_problems()
                if problems:
                    messagebox.showerror("Cannot convert to TrueHD", "\n\n".join(problems))
                    return
                test_clip = self.test_clip_var.get()
                try:
                    clip_start = parse_timecode(self.clip_start_var.get()) if test_clip else 0.0
                except ValueError as e:
                    messagebox.showerror("Invalid start time", str(e))
                    return
                options = TrueHDOptions(
                    remove_source=remove_source, make_default=self.make_default_var.get(),
                    strict_verify=self.strict_verify_var.get(), test_clip=test_clip,
                    clip_seconds=int(self.clip_length_var.get().split()[0]), clip_start=clip_start,
                    replace_original=(self.output_mode_var.get() == "replace" and not test_clip),
                )
                if options.replace_original and not messagebox.askyesno(
                        "Queue TrueHD conversion",
                        "When this job runs, the original MKV will be replaced after verification passes, "
                        "WITHOUT asking again.\n\nChoose No to keep the original and save a new "
                        "…_Sound_TrueHD.mkv instead."):
                    options.replace_original = False
                job = truehd_job(input_path, self.audio_streams, self.subtitle_streams, stream, options, removes,
                                 LAYOUT_LABELS[truehd_target_layout(stream)])
            else:
                codec = "eac3" if "E-AC-3" in self.codec_var.get() else "ac3"
                job = convert_job(input_path, self.audio_streams, self.subtitle_streams, stream, codec,
                                  self.bitrate_var.get(), remove_source, removes)
        except OSError as e:
            messagebox.showerror("Add to Queue", str(e))
            return
        self.queue_tab.add_jobs([job])
        self._log(f"Added to queue: {job.name} - {job.summary}")

    def _queue_track_removal(self):
        input_path = self.current_file
        audio, subtitles = self._ticked()
        if not input_path or not os.path.exists(input_path) or not (audio or subtitles):
            return
        if len(audio) == len(self.audio_streams) and not messagebox.askyesno(
                "Queue Removal", "This removes ALL audio tracks - the film will have no sound. Queue it anyway?",
                icon="warning"):
            return
        job = removal_job(input_path, self.audio_streams, self.subtitle_streams, audio + subtitles)
        self.queue_tab.add_jobs([job])
        self._log(f"Added to queue: {job.name} - {job.summary}")

    def _start_track_removal(self):
        """Remove the ticked audio/subtitle tracks without converting anything."""
        input_path = self.current_file
        if not input_path or not os.path.exists(input_path):
            messagebox.showwarning("Warning", "Please select an MKV file")
            return
        if not self.ffmpeg_path:
            messagebox.showerror("Error", "FFmpeg not initialized")
            return
        audio, subtitles = self._ticked()
        if not audio and not subtitles:
            return
        message = ("Remove these tracks from the MKV?\n\n" + self._describe_tracks(audio, subtitles)
                   + "\n\nNothing is converted; every other track is copied unchanged. "
                   "A temporary MKV is created first and replaces the source only after success. "
                   "This cannot be undone.")
        if len(audio) == len(self.audio_streams):
            message += "\n\nWARNING: this removes ALL audio tracks - the film will have no sound."
        if not messagebox.askyesno("Remove tracks", message, icon="warning"):
            return

        indices = [s.stream_index for s in audio + subtitles]
        self.job_handle = ProcessHandle()
        handle = self.job_handle
        self._set_busy(True)
        name = Path(input_path).name
        self._log("========================================")
        self._log(f"Removing tracks from {name}: " + ", ".join(f"#{i}" for i in indices))

        def worker():
            try:
                remove_streams_in_place(input_path, self.ffmpeg_path, indices,
                                        progress_callback=self._log, handle=handle,
                                        on_progress=self._progress_reporter("Removing tracks"))
                self._log(f"✓ Removed {len(indices)} track(s)")
                self._log("========================================")
                self._ui(messagebox.showinfo, "Tracks removed",
                         f"Removed {len(indices)} track(s).\n\nUpdated MKV:\n{input_path}")
                self._ui(self._detect_audio)
            except ConversionCancelled:
                self._log("Track removal cancelled - the original MKV was not changed")
            except RuntimeError as e:
                self._log(f"Error: {e}")
                self._ui(messagebox.showerror, "Track removal failed", str(e))
            finally:
                self._ui(self._set_busy, False)

        self.job_thread = threading.Thread(target=worker, daemon=True)
        self.job_thread.start()

    def _start_truehd_conversion(self, input_path, stream):
        problems = self._truehd_problems()
        if problems:
            messagebox.showerror("Cannot convert to TrueHD", "\n\n".join(problems))
            return
        if not self.ffprobe_path:
            messagebox.showerror("ffprobe required", "TrueHD conversion needs ffprobe for verification. "
                                 "Place ffprobe next to the LibreMPEG or FFmpeg executable.")
            return

        test_clip = self.test_clip_var.get()
        try:
            clip_start = parse_timecode(self.clip_start_var.get()) if test_clip else 0.0
        except ValueError as e:
            messagebox.showerror("Invalid start time", str(e))
            return
        options = TrueHDOptions(
            remove_source=self.remove_source_var.get(),
            make_default=self.make_default_var.get(),
            strict_verify=self.strict_verify_var.get(),
            test_clip=test_clip,
            clip_seconds=int(self.clip_length_var.get().split()[0]),
            clip_start=clip_start,
            replace_original=(self.output_mode_var.get() == "replace" and not test_clip),
            remove_streams=tuple(sorted(self.remove_ticks)),
        )

        target = output_name(Path(input_path), options.clip_seconds if test_clip else None)
        if test_clip:
            summary = (f"A {options.clip_seconds}-second test clip will be saved as a new file:\n"
                       f"{target.name}\n\nThe original MKV is never modified in test mode.")
        elif options.replace_original:
            summary = ("A verified copy will be created first. You will then be asked whether to "
                       "replace the original MKV with it.")
        else:
            summary = f"A new MKV will be created:\n{target.name}\n\nThe original MKV is not modified."
        ticked_audio, ticked_subs = self._ticked()
        if ticked_audio or ticked_subs:
            summary += ("\n\nThese ticked tracks will not be included in the new MKV:\n"
                        + self._describe_tracks(ticked_audio, ticked_subs))
        if not options.strict_verify:
            summary += "\n\nStrict lossless verification is OFF: the result will not be checked sample-by-sample."
        for warning in source_warnings(stream):
            summary += f"\n\nWarning: {warning}"
        if not messagebox.askyesno("Convert to Dolby TrueHD", summary + "\n\nContinue?"):
            return

        report = self.encoder_report
        self.job_handle = ProcessHandle()
        handle = self.job_handle
        self._set_busy(True, indeterminate=True)

        converter = TrueHDConverter(
            encoder=report.path, ffprobe=self.ffprobe_path, reference_ffmpeg=self.ffmpeg_path,
            report=report, log=self._log,
            progress=lambda phase, fraction, detail: self._ui(self._show_progress, phase, fraction, detail),
            handle=handle,
        )

        def worker():
            try:
                result = converter.convert(input_path, stream.stream_index, options)
                self._ui(self._finish_truehd, converter, result)
            except ConversionCancelled:
                self._log("Conversion cancelled - the original MKV was not changed")
                self._ui(self._set_busy, False)
            except (TrueHDError, RuntimeError, OSError) as e:
                self._log(f"✗ {e}")
                self._ui(messagebox.showerror, "TrueHD conversion failed", str(e))
                self._ui(self._set_busy, False)

        self.job_thread = threading.Thread(target=worker, daemon=True)
        self.job_thread.start()

    def _finish_truehd(self, converter, result):
        """Runs on the main thread once encoding and verification succeeded."""
        try:
            if result.awaiting_replace_confirmation:
                if messagebox.askyesno(
                    "Replace original MKV?",
                    "Conversion and verification passed.\n\n"
                    f"Replace the original file\n{Path(result.source_path).name}\n"
                    "with the verified version? This cannot be undone.\n\n"
                    f"Choose No to keep the original and save as\n{Path(result.final_path).name}",
                    icon="warning",
                ):
                    try:
                        converter.finalize_replace(result)
                        self._log("Original MKV replaced with verified TrueHD version")
                    except OSError as e:
                        # e.g. the file is open in Plex or another player (Windows locks it)
                        converter.finalize_as_new(result)
                        self._log(f"Could not replace the original ({e}); saved as a new file instead")
                        messagebox.showwarning(
                            "Original not replaced",
                            f"The original MKV could not be replaced:\n{e}\n\n"
                            "It may be open in another program. The verified result was saved as:\n"
                            f"{result.final_path}")
                else:
                    converter.finalize_as_new(result)
        except (TrueHDError, OSError) as e:
            converter.discard(result)
            self._set_busy(False)
            self._log(f"✗ {e}")
            messagebox.showerror("Could not save output", f"{e}\n\nThe original MKV was not changed.")
            return

        self._set_busy(False)
        lossless = {True: "Verified bit-identical to the source audio",
                    False: "NOT verified", None: "Not checked (strict verification was off)"}[result.lossless]
        self._log("========================================")
        self._log("✓ TrueHD conversion complete" + (" (test clip)" if result.clip else ""))
        self._log(f"  Output: {result.final_path}")
        self._log(f"  Lossless: {lossless}")
        self._log("========================================")
        messagebox.showinfo(
            "TrueHD conversion complete",
            f"Saved:\n{result.final_path}\n\n"
            f"All {len(result.checks)} output checks passed.\n"
            f"Lossless: {lossless}"
            + ("\n\nThis is a test clip - play it in Plex and check your receiver "
               "shows Dolby TrueHD with 7.1 channels." if result.clip else "")
        )
        if result.final_path == result.source_path:
            self._detect_audio()


def run_self_test(report_path: str) -> int:
    """Headless check used by the Windows build after installing.

    Writes a JSON report and returns 0 only when the bundled FFmpeg is found,
    the TrueHD encoder passes its self-test and tsMuxeR runs. Never downloads anything.
    """
    import json
    from dataclasses import asdict
    from app_paths import app_dir

    from tsmuxer_tool import check_tsmuxer

    ffmpeg = get_ffmpeg_path()
    report = check_encoder(get_setting("truehd_encoder_path"), reference_decoder=ffmpeg)
    tsmuxer = check_tsmuxer(get_setting("tsmuxer_path"))
    data = {
        "version": __version__,
        "app_dir": str(app_dir()),
        "ffmpeg": ffmpeg,
        "ffprobe": find_ffprobe(ffmpeg, report.path),
        "encoder": report.path,
        "encoder_origin": report.origin,
        "status": report.status,
        "message": report.message,
        "tests": [asdict(t) for t in report.tests],
        "tsmuxer": tsmuxer.path,
        "tsmuxer_origin": tsmuxer.origin,
        "tsmuxer_status": tsmuxer.status,
        "tsmuxer_message": tsmuxer.message,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    ok = bool(ffmpeg and data["ffprobe"]) and report.status == STATUS_READY and tsmuxer.ok
    return 0 if ok else 1


def main():
    """Main entry point."""
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        sys.exit(run_self_test(sys.argv[2]))
    root = Tk()
    app = ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
