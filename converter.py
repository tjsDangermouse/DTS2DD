"""Main GUI application for DTS to AC-3 converter."""
import os
import sys
import threading
from pathlib import Path
from tkinter import (
    Tk, Frame, Label, Entry, Button, Text, messagebox,
    filedialog, Scrollbar, StringVar, ttk, LabelFrame, END, BOTH
)

from ffmpeg_downloader import ensure_ffmpeg_available
from ffmpeg_helper import get_audio_streams, convert_to_ac3


class ConverterApp:
    """Main application class."""

    def __init__(self, root):
        self.root = root
        self.root.title("DTS to AC-3 Converter")
        self.root.geometry("800x900")
        self.ffmpeg_path = None
        self.current_file = None
        self.audio_streams = []
        self.status_label = None
        self.log_widget = None

        self._create_widgets()
        self._initialize_ffmpeg()

    def _initialize_ffmpeg(self):
        """Initialize FFmpeg on app startup."""
        def get_ffmpeg():
            try:
                self.ffmpeg_path = ensure_ffmpeg_available(progress_callback=self._log)
                self.root.after(2000, lambda: self._update_status(""))

            except RuntimeError as e:
                self.root.after(0, lambda: self._show_error(str(e)))

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
            self.status_label.config(text=message)
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

    def _create_widgets(self):
        """Create GUI widgets."""
        # Status label for FFmpeg initialization
        self.status_label = Label(self.root, text="", fg="blue")
        self.status_label.pack(pady=5)

        main_frame = Frame(self.root, padx=10, pady=10)
        main_frame.pack(fill=BOTH, expand=True)

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

        # Audio streams display frame
        audio_frame = LabelFrame(main_frame, text="Audio Streams", padx=10, pady=10)
        audio_frame.pack(fill=BOTH, expand=True, pady=(0, 10))

        scrollbar = Scrollbar(audio_frame)
        scrollbar.pack(side="right", fill="y")

        self.audio_text = Text(audio_frame, height=8, yscrollcommand=scrollbar.set)
        self.audio_text.pack(fill=BOTH, expand=True)
        scrollbar.config(command=self.audio_text.yview)

        # Output settings frame
        output_frame = LabelFrame(main_frame, text="Output Settings", padx=10, pady=10)
        output_frame.pack(fill="x", pady=(0, 10))

        Label(output_frame, text="Output File:").grid(row=0, column=0, sticky="w")
        self.output_entry = Entry(output_frame, width=50)
        self.output_entry.grid(row=0, column=1, padx=5)

        Button(
            output_frame, text="Browse",
            command=self._select_output
        ).grid(row=0, column=2)

        Label(output_frame, text="Audio Codec:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.codec_var = StringVar(value="ac3")
        codec_combo = ttk.Combobox(
            output_frame,
            textvariable=self.codec_var,
            values=["AC-3 (Dolby Digital)", "E-AC-3 (Dolby Digital Plus)"],
            state="readonly",
            width=25
        )
        codec_combo.grid(row=1, column=1, sticky="w", padx=5, pady=(10, 0))
        codec_combo.bind("<<ComboboxSelected>>", lambda e: self._update_bitrate_options())

        Label(output_frame, text="Bitrate:").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.bitrate_var = StringVar(value="384k")
        self.bitrate_combo = ttk.Combobox(
            output_frame,
            textvariable=self.bitrate_var,
            state="readonly",
            width=10
        )
        self.bitrate_combo.grid(row=2, column=1, sticky="w", padx=5, pady=(10, 0))
        self._update_bitrate_options()

        # Progress bar
        self.progress = ttk.Progressbar(main_frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 10))

        # Convert button
        button_frame = Frame(main_frame)
        button_frame.pack(fill="x")

        self.convert_button = Button(
            button_frame, text="Convert",
            command=self._start_conversion,
            state="disabled"
        )
        self.convert_button.pack(side="left", padx=5)

        Button(
            button_frame, text="Detect Audio",
            command=self._detect_audio
        ).pack(side="left", padx=5)

        # Log viewer frame
        log_frame = LabelFrame(main_frame, text="Activity Log", padx=10, pady=10)
        log_frame.pack(fill=BOTH, expand=True, pady=(10, 0))

        log_scrollbar = Scrollbar(log_frame)
        log_scrollbar.pack(side="right", fill="y")

        self.log_widget = Text(log_frame, height=6, state="disabled", yscrollcommand=log_scrollbar.set)
        self.log_widget.pack(fill=BOTH, expand=True)
        log_scrollbar.config(command=self.log_widget.yview)

    def _select_file(self):
        """Open file dialog to select MKV file."""
        file_path = filedialog.askopenfilename(
            filetypes=[("MKV files", "*.mkv"), ("All files", "*.*")]
        )
        if file_path:
            self.current_file = file_path
            self.file_entry.delete(0, END)
            self.file_entry.insert(0, file_path)

            # Generate default output path
            output_path = self._generate_output_path(file_path)
            self.output_entry.delete(0, END)
            self.output_entry.insert(0, output_path)

            # Log and auto-detect audio
            filename = Path(file_path).name
            self._log(f"File selected: {filename}")
            self._detect_audio()

    def _select_output(self):
        """Open file dialog to select output file."""
        file_path = filedialog.asksaveasfilename(
            defaultextension=".mkv",
            filetypes=[("MKV files", "*.mkv"), ("All files", "*.*")]
        )
        if file_path:
            self.output_entry.delete(0, END)
            self.output_entry.insert(0, file_path)

    def _generate_output_path(self, input_path: str) -> str:
        """Generate default output path with codec suffix."""
        path = Path(input_path)
        codec = self.codec_var.get()
        suffix = "_AC3" if "AC-3" in codec else "_DDPLUS"
        return str(path.parent / f"{path.stem}{suffix}.mkv")

    def _update_bitrate_options(self):
        """Update available bitrates based on selected codec."""
        codec = self.codec_var.get()
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

    def _detect_audio(self):
        """Detect audio streams in the selected file."""
        if not self.current_file:
            messagebox.showwarning("Warning", "Please select an MKV file first")
            return

        if not self.ffmpeg_path:
            messagebox.showerror("Error", "FFmpeg not initialized")
            return

        self.audio_text.config(state="normal")
        self.audio_text.delete(1.0, END)
        self._log("Detecting audio streams...")

        def detect():
            try:
                streams = get_audio_streams(self.current_file, self.ffmpeg_path)
                self.audio_streams = streams

                # Update audio display
                self.root.after(0, lambda: self._update_audio_display(streams))
                self._log(f"Found {len(streams)} audio stream(s)")

            except RuntimeError as e:
                self.root.after(0, lambda: self._update_audio_display_error(str(e)))
                self._log(f"Error: {str(e)}")

        thread = threading.Thread(target=detect, daemon=True)
        thread.start()

    def _update_audio_display(self, streams):
        """Update audio streams display on main thread."""
        self.audio_text.config(state="normal")
        self.audio_text.delete(1.0, END)
        if streams:
            for stream in streams:
                self.audio_text.insert(END, f"{str(stream)}\n")
            self.convert_button.config(state="normal")
        else:
            self.audio_text.insert(END, "No audio streams found")
        self.audio_text.config(state="disabled")

    def _update_audio_display_error(self, error_msg):
        """Update audio display with error message on main thread."""
        self.audio_text.config(state="normal")
        self.audio_text.delete(1.0, END)
        self.audio_text.insert(END, f"Error: {error_msg}")
        self.audio_text.config(state="disabled")

    def _start_conversion(self):
        """Start the conversion process."""
        input_path = self.file_entry.get()
        output_path = self.output_entry.get()

        if not input_path or not output_path:
            messagebox.showwarning("Warning", "Please select input and output files")
            return

        if not self.ffmpeg_path:
            messagebox.showerror("Error", "FFmpeg not initialized")
            return

        # Validate paths
        if not os.path.exists(input_path):
            messagebox.showerror("Error", f"Input file not found: {input_path}")
            return

        # Check output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            messagebox.showerror("Error", f"Output directory not found: {output_dir}")
            return

        bitrate = self.bitrate_var.get()
        codec_choice = self.codec_var.get()
        codec = "eac3" if "E-AC-3" in codec_choice else "ac3"

        self.convert_button.config(state="disabled")
        self.progress.start()

        input_file = Path(input_path).name
        output_file = Path(output_path).name
        self._log(f"========================================")
        self._log(f"Starting conversion")
        self._log(f"  Input: {input_file}")
        self._log(f"  Output: {output_file}")
        self._log(f"  Codec: {codec_choice}")
        self._log(f"  Bitrate: {bitrate}")
        self._log(f"  Status: Processing...")
        self._log(f"========================================")

        def convert():
            try:
                result = convert_to_ac3(
                    input_path,
                    output_path,
                    bitrate,
                    self.ffmpeg_path,
                    codec=codec,
                    progress_callback=self._log
                )

                self.progress.stop()
                self.convert_button.config(state="normal")

                if result:
                    self._log(f"========================================")
                    self._log("✓ Conversion completed successfully!")
                    self._log(f"  Output: {output_file}")
                    self._log(f"========================================")
                    messagebox.showinfo(
                        "Success",
                        f"Conversion completed successfully!\n\n{output_path}"
                    )
                else:
                    self._log("✗ Conversion failed")
                    messagebox.showerror("Error", "Conversion failed")

            except RuntimeError as e:
                self.progress.stop()
                self.convert_button.config(state="normal")
                self._log(f"Error: {str(e)}")
                messagebox.showerror("Conversion Error", str(e))

        thread = threading.Thread(target=convert, daemon=True)
        thread.start()


def main():
    """Main entry point."""
    root = Tk()
    app = ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
