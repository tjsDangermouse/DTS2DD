# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DTS to AC-3/DD+ Converter: A cross-platform GUI application that converts DTS audio in MKV files to AC-3 (Dolby Digital) or E-AC-3 (Dolby Digital Plus) format, and DTS-HD Master Audio 5.1/7.1 to lossless Dolby TrueHD. The app automatically detects and downloads FFmpeg on first run. TrueHD uses a separately configured LibreMPEG executable (never auto-downloaded).

## Architecture

### Core Components

**converter.py** - Main GUI application (tkinter-based)
- `ConverterApp`: Main application class managing the entire UI and workflow
- File selection, codec/bitrate configuration, audio stream display, activity logging
- Manages threading for FFmpeg initialization, audio detection, and conversion
- Key methods: `_select_file()`, `_detect_audio()`, `_start_conversion()`, `_log()`

**ffmpeg_downloader.py** - FFmpeg detection and auto-download
- Platform detection (Windows/macOS) with separate download/extraction paths
- Checks system PATH first, then local `./ffmpeg/` directory recursively
- macOS: Downloads 7z from evermeet.cx, extracts with py7zr
- Windows: Downloads ZIP from gyan.dev, extracts with built-in zipfile
- Key function: `ensure_ffmpeg_available()` - called at app startup

**ffmpeg_helper.py** - FFmpeg subprocess management (shared by both workflows)
- `get_audio_streams()`: ffprobe JSON when ffprobe is found, else the original regex parse of `ffmpeg -i` stderr
- `build_stream_maps()`: maps all streams + the selected audio again; attachments are mapped LAST (Matroska muxer fails otherwise)
- `convert_and_append_audio()`: AC-3/E-AC-3 in-place workflow (temp file -> `os.replace`)
- `run_ffmpeg()`: Popen with `-progress pipe:1`, stderr drained on a thread (last 200 lines kept for errors), cancellation via `ProcessHandle`

**media_probe.py** - ffprobe JSON, `classify_audio()` (DTS / DTS-HD HRA / DTS-HD MA / DTS:X / TrueHD / TrueHD Atmos / DD / DD+, "Unknown" when unsure), video signature + `video_packet_hashes()` (framemd5 of copied packets) for preservation checks

**truehd_encoder.py** - LibreMPEG backend
- `discover_encoder()`: configured path -> `librempeg/` beside the app -> `librempeg` on PATH -> an `ffmpeg` on PATH that advertises TrueHD 7.1
- `check_encoder()`: parses `-h encoder=truehd`, then self-tests 5.1(side)/7.1 at 16/24-bit: labelled synthetic PCM -> TrueHD MKV -> decoded by LibreMPEG AND standard FFmpeg -> per-channel identity + bit-exact + major-sync channel-assignment bits parsed in Python
- `build_truehd_command()`: forces `-ch_layout/-sample_fmt/-ar` (prevents silent downmix), `-strict:a:N experimental`, `-bsf:a:N side_data=remove=skip_samples` (see below)
- `create_channel_test_file()`: speaker-identification MKV for checking on a receiver

**truehd_workflow.py** - `TrueHDConverter.convert()`: validate source -> disk space -> hidden temp in source dir -> (clip: stream-copy cut first) -> encode -> `_verify_output()` checks -> optional `_strict_compare()` (streams both decodes as s32le and compares) -> rename to `_Sound_TrueHD.mkv` or leave for GUI-confirmed `finalize_replace()`

**app_paths.py** - `app_dir()` (exe folder when frozen), `user_data_dir()` (%LOCALAPPDATA%\DTS2DD), `ffmpeg_folders()`: bundled `ffmpeg/` beside the exe -> (PATH, checked in ffmpeg_downloader) -> per-user download folder -> legacy `./ffmpeg`. Never write into the program folder.

**app_settings.py** - JSON settings in %APPDATA%/DTS2DD (or `DTS2DD_SETTINGS_DIR`); stores `truehd_encoder_path`

**audio_stream_info.py** - Data model
- `AudioStreamInfo` dataclass; original 5 fields + optional structured fields (layout, sample_fmt, bit depth, title, dispositions)
- `__str__` keeps the legacy format when only text probing was available

### Threading Model

All long-running operations run in background daemon threads to keep GUI responsive:
- FFmpeg initialization checks on app startup
- Audio stream detection when file selected
- Conversion process during encoding

Main thread uses `root.after()` to safely update GUI from worker threads. Never touch widgets or `messagebox` from a worker - use `self._ui(func, *args)`.

### Key Design Decisions

1. **Automatic FFmpeg Download**: Eliminates user setup friction. Platform-specific builds ensure compatibility.

2. **Recursive FFmpeg Search**: Extracted archives may nest FFmpeg in subdirectories; `rglob()` finds it regardless of nesting depth.

3. **Popen + `-progress pipe:1`**: `run_ffmpeg()` parses progress from stdout while a thread drains stderr, so errors can be shown and jobs cancelled.

4. **Codec Selection**: E-AC-3 (eac3) supports higher bitrates (up to 768k) for better quality preservation when converting from high-bitrate DTS sources.

5. **Activity Log**: Real-time logging in GUI shows all operations and progress, helping users understand what's happening.

### TrueHD findings (verified Nov 2026, LibreMPEG master 2f18174)

- Standard FFmpeg's truehd encoder only supports up to 5.1(side). LibreMPEG adds 7.1 etc.
- Only `5.1(side)` and `7.1` are allowed (`SUPPORTED_LAYOUTS`). 6.1 encodes but is NOT bit-exact; 5.1(back) signals no surrounds in the 6ch presentation; `7.1(wide)` is unsupported and FFmpeg silently downmixes it to 5.1 unless the output layout is forced.
- DTS decoder output `s16p/s32p` = lossless XLL decode; `fltp` = lossy core only -> refuse.
- LibreMPEG tags the short final access unit with discard_padding although it already coded only the real samples; in MKV that drops up to 39 real samples. Fixed with the `side_data` bsf (upstream FFmpeg lacks this bsf, so it fails the self-test).
- Seeking (`-ss`) and encoding in one pass gave the TrueHD track a ~250 ms late start. Test clips are therefore cut by stream copy first, then converted like a whole file.
- Strict verification is hash-based: the encode command has a second output (`-map 0:<src> -c:a pcm_s32le -f hash -hash sha256 <file>`) that fingerprints the exact PCM fed to the encoder; afterwards the independent FFmpeg decodes the TrueHD track FROM THE MKV and hashes it. The source film is never re-read (the old two-decoder comparison took ~50 min on a 36 GB film). Decoding from the MKV is deliberate - it is what exposes container bugs like the DiscardPadding one. `_locate_difference` (slow side-by-side compare) only runs after a mismatch, to report where.
- The 2ch/6ch embedded presentations are channel subsets, not downmixes.

### Tests

`python tests/fetch_samples.py` then `DTS2DD_LIBREMPEG=... python -m pytest tests`. Unit tests (`tests/test_unit.py`) need nothing; integration tests skip without tools/samples. Hardware (Plex/Fire TV/AVR) testing is manual - USER_GUIDE.md.

## Building for Distribution

### macOS Build
```bash
./build_macos.sh
```
Outputs: `dist/DTS to AC-3 Converter.app`
Install to Applications: `cp -r dist/"DTS to AC-3 Converter.app" /Applications/`

### Windows Build (installer)
CI: `.github/workflows/windows-installer.yml` builds LibreMPEG (pinned commit, MSYS2, static), downloads pinned FFmpeg (SHA-256 checked), runs all tests on Windows, PyInstaller one-folder build -> `dist/DTS2DD/`, adds `ffmpeg/`, `librempeg/`, `licenses/`, compiles `installer/DTS2DD.iss` (Inno Setup, per-user, no admin), installs silently and runs the installed app's `--self-test`.
Local: `build_windows.bat` with tools in `vendor/ffmpeg` and `vendor/librempeg`.
`converter.spec` is one-folder on Windows (or with `DTS2DD_ONEDIR=1`), single-file `.app` on macOS.
`"DTS to AC-3 Converter.exe" --self-test report.json` checks bundled tools headlessly (exit 0 = TrueHD ready). Version lives in `version.py`.

Both use PyInstaller with `converter.spec` configuration. Icons are embedded (PNG, ICO, ICNS).

### Build Dependencies
- Install with: `pip install -r requirements-build.txt` (PyInstaller, Pillow for icon generation)
- Runtime dependencies: `pip install -r requirements.txt` (py7zr for macOS FFmpeg extraction)

## Running the Application

### Development (CLI)
```bash
python3 converter.py
```

### After Building (Standalone)
- macOS: Double-click the `.app` or search for it in Spotlight
- Windows: Double-click the `.exe`

FFmpeg auto-downloads on first run (2-5 minutes). Subsequent launches are instant.

## Common Development Tasks

### Adding a New Audio Codec
1. Update bitrate options in `converter.py` `_update_bitrate_options()` if needed
2. Add codec choice to the dropdown in `_create_widgets()`
3. Pass codec name to `convert_to_ac3()` in `_start_conversion()`
4. Update FFmpeg command in `ffmpeg_helper.py` - just change `-c:a ac3` to `-c:a <codec_name>`

### Modifying FFmpeg Download URLs
- macOS: Edit `_download_ffmpeg_macos()` in `ffmpeg_downloader.py` - change `url` variable
- Windows: Edit `_download_ffmpeg_windows()` in `ffmpeg_downloader.py` - change `url` variable

### Improving Audio Stream Detection
- Regex pattern in `ffmpeg_helper.py` `get_audio_streams()` parses FFmpeg stderr
- Test with: `ffmpeg -i test.mkv 2>&1 | grep "Stream #"`
- Update regex if FFmpeg output format changes

### Updating GUI Layout
- All widgets in `converter.py` `_create_widgets()` method
- Use tkinter's LabelFrame (custom implementation) for grouped sections
- Grid layout for Output Settings, pack layout elsewhere

### Testing Conversion
```bash
# Manually test FFmpeg command (simulates what app does)
ffmpeg -i input.mkv -c:v copy -c:a ac3 -b:a 384k output.mkv -y

# Or with E-AC-3
ffmpeg -i input.mkv -c:v copy -c:a eac3 -b:a 640k output.mkv -y
```

## Known Limitations & Workarounds

1. **Progress Percentage**: TrueHD shows percentage/speed/ETA from `run_ffmpeg()`. The AC-3/E-AC-3 path still shows an indeterminate bar (it does not pass a duration).

2. **Nested FFmpeg Directories**: Windows ZIP extraction creates nested folders. Handled by `rglob()` search in `get_ffmpeg_path()`.

3. **Single Audio Stream**: App converts one audio stream per run; all other streams are copied.

4. **No Drag-and-Drop**: tkinterdnd2 would be required. Current browse button is sufficient.

## Testing Notes

- **macOS First Run**: FFmpeg downloads from evermeet.cx (~70MB)
- **Windows First Run**: FFmpeg downloads from gyan.dev (~100MB)
- **After First Run**: FFmpeg path is cached in `./ffmpeg/` directory
- **Test File**: Use small MKV files (100MB-500MB) for quick testing
- **Verify Output**: Check output file exists and plays correctly with audio

## Platform-Specific Issues

### macOS
- PyInstaller creates `.app` bundle with embedded Python runtime
- Code signing optional (codesign command available in build script)
- Homebrew install: `brew install ffmpeg` (alternative to auto-download)

### Windows
- Console window hidden with `subprocess.CREATE_NO_WINDOW` flag
- File paths use both `/` and `\` - Python normalizes automatically
- ZIP extraction simpler than macOS 7z format

## File Organization

```
converter.py              # GUI application
ffmpeg_downloader.py      # FFmpeg auto-setup
ffmpeg_helper.py          # FFmpeg integration (shared)
media_probe.py            # ffprobe JSON + verification helpers
truehd_encoder.py         # LibreMPEG TrueHD backend + self-test
truehd_workflow.py        # TrueHD conversion/verification workflow
app_settings.py           # Persistent settings
app_paths.py              # Program / per-user folders, bundled tool lookup
version.py                # App version (window title, installer, release tag)
installer/                # Inno Setup script + third-party notices
.github/workflows/        # Windows installer build + tests
audio_stream_info.py      # Data model
tests/                    # unit + integration tests, fetch_samples.py
create_icon.py            # Icon generator
converter.spec            # PyInstaller config
build_macos.sh            # macOS build script
build_windows.bat         # Windows build script
requirements.txt          # Runtime dependencies
requirements-build.txt    # Build dependencies
icons/                    # App icons (PNG, ICO, ICNS)
ffmpeg/                   # Auto-created, contains downloaded FFmpeg
dist/                     # Build output (app/exe)
build/                    # PyInstaller artifacts (temp)
```

## Dependencies

**Runtime** (requirements.txt):
- `py7zr>=0.20.5` - FFmpeg 7z extraction on macOS

**Build** (requirements-build.txt):
- `pyinstaller>=6.0.0` - Create standalone executables
- `pillow>=10.0.0` - Generate app icons

**Built-in**:
- tkinter (included with Python 3.11+)
- subprocess, threading, re, urllib, zipfile, pathlib (all stdlib)
