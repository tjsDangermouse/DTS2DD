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
- `build_stream_maps()`: maps all streams + the selected audio again; attachments are mapped LAST (Matroska muxer fails otherwise); `remove_streams` drops ticked audio/subtitle tracks (output audio index then = audio count - removed audio)
- `get_tracks()`: audio + subtitle lists for the GUI tick lists; `remove_streams_in_place()`: removal-only stream copy
- `convert_and_append_audio()`: AC-3/E-AC-3 in-place workflow (temp file -> `os.replace`)
- `run_ffmpeg()`: Popen with `-progress pipe:1`, stderr drained on a thread (last 200 lines kept for errors), cancellation via `ProcessHandle`

**media_probe.py** - ffprobe JSON, `classify_audio()` (DTS / DTS-HD HRA / DTS-HD MA / DTS:X / TrueHD / TrueHD Atmos / DD / DD+, "Unknown" when unsure), video signature + `video_packet_hashes()` (framemd5 of copied packets) for preservation checks

**truehd_encoder.py** - LibreMPEG backend
- `discover_encoder()`: configured path -> `librempeg/` beside the app -> `librempeg` on PATH -> an `ffmpeg` on PATH that advertises TrueHD 7.1
- `check_encoder()`: parses `-h encoder=truehd`, then self-tests 5.1(side)/7.1 at 16/24-bit: labelled synthetic PCM -> TrueHD MKV -> decoded by LibreMPEG AND standard FFmpeg -> per-channel identity + bit-exact + major-sync channel-assignment bits parsed in Python
- `build_truehd_command()`: forces `-ch_layout/-sample_fmt/-ar` (prevents silent downmix), `-strict:a:N experimental`, `-bsf:a:N side_data=remove=skip_samples` (see below)
- `create_channel_test_file()`: speaker-identification MKV for checking on a receiver

**truehd_workflow.py** - `TrueHDConverter.convert()`: validate source -> disk space -> hidden temp in source dir -> (clip: stream-copy cut first) -> encode -> `_verify_output()` checks -> optional `_strict_compare()` (streams both decodes as s32le and compares) -> rename to `_Sound_TrueHD.mkv` or leave for GUI-confirmed `finalize_replace()`

**Library Scanner** (tab 2 of the main window; read-only scan, explicit repairs)
- `media_inspector.py`: `inspect_file()` -> `MediaReport` (ffprobe always; MediaInfo `--Output=JSON` / `mkvmerge -J` when found; 2 decoded frames for HDR10+; stream-copied 4 s samples at start + middle of E-AC-3/AC-3/TrueHD tracks). `INSPECTOR_VERSION` invalidates the cache when the report changes
- `atmos_inspector.py`: full E-AC-3 bsi parser up to addbsi (`flag_ec3_extension_type_a` + complexity index = JOC), TrueHD major sync `substream_info & 0x80` (16-ch presentation). Classification needs >=30 frames to call JOC absent; any tool disagreement -> "Manual inspection recommended". Validated only against FFmpeg-encoded E-AC-3 with JOC signalling inserted (`tests/eac3_fixtures.py`; FFmpeg then reports the Atmos profile) - never against real Atmos masters
- `compatibility_rules.py`: `@rule` registry, `ScannerConfig` (settings key `library_scanner`), `DEVICE_PROFILES`, `subtitle_cleanup_plan()` (unlabelled kept unless confirmed; >=1 regular English always kept). Device rules have `device=` and are potential problems only
- `library_scanner.py`: `find_media_files`, `ScanCache` (user_data_dir()/library_scan_cache.json, size+mtime_ns), thread pool, ETA from uncached files. Findings are recomputed from cached reports
- `library_repair.py`: `plan_repair()` -> one pass per file (TrueHD via `TrueHDConverter` with subtitle removal folded in; AC-3/E-AC-3 via `convert_and_append_audio`; else `remux_in_place`), `validate_output()` before replace, `run_batch()`; skips files changed since the scan; MKV only
- `scanner_ui.py`: tab, `RepairDialog` (plan preview), `SettingsDialog`. Never name dialog methods `_options`/`_config` - they shadow tkinter internals

**Queue** (tab 3)
- `job_queue.py`: `JobQueue` (user_data_dir()/job_queue.json, saved on every change; RUNNING -> INTERRUPTED on load), `QueueRunner` (takes the next QUEUED job each time, so edits during a run apply; `stop_after_current`, shared `cancelled` event), `TrackRef` / `resolve_ref` (signature + position among identical tracks; refuses when ambiguous)
- `queue_jobs.py`: job builders (`convert_job`, `removal_job`, `truehd_job` from the Convert tab; `repair_job` from the scanner - stores the requested actions/options and RE-PLANS at run time) and executors calling the existing workflows. `QueueEnv.new_handle()` pre-cancels if the queue was cancelled between jobs
- `queue_ui.py`: tab; `ConverterApp.set_external_busy()` derives busy from scanner repairs + queue; `job_finished()` refreshes the Convert tab and scanner row

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
- FLAC: FFmpeg decodes every 6-ch FLAC as `5.1` (back). It is relabelled by position to `5.1(side)` with `-filter:a:k channelmap=channel_layout=5.1(side)` (`TrueHDJob.source_layout`) - bit-exact. FLAC s16/s32 -> s16p/s32p. Stereo is self-tested (`TESTED_LAYOUTS`) and offered only for FLAC; header assignment 0x01/0x01.
- ffmpeg CLI auto-flags the first track of a type "default" when >=2 remain, none is default and NO `-disposition` was given (e.g. after removing subtitles). `keep_dispositions_args()` passes stream 0's own flags to disable that; used by track removal, legacy AC-3 append and the scanner remux.
- Peak data rate: unpatched mlpenc declares 9.6 Mb/s with constant input timing -> every AU must be <= 1000 B at 48 kHz. Spectre 0:34.5 hit 1018 B and the Denon glitched (VLC clean). We patch LibreMPEG (`installer/patches/`) with a `peak_bitrate` option, declare 18 Mb/s, record AU sizes via `-stats_enc_post ... {size}` during the encode and fail if any exceeds the header's limit (`check_access_unit_sizes`). Note AU sizes depend on where encoding starts, so reproduce issues from the same start time.
- Worst case (incompressible full-scale noise, measured): max AU 1248 B for 7.1/48k, 934 B for 5.1/48k, 1648 B for 5.1/96k - always under the 1875 B limit at 18 Mb/s. 7.1/96k (2192 B) and 5.1/192k (3094 B) CAN exceed; the AU-size check then fails the conversion. LibreMPEG output is ~30% larger than raw PCM on noise (Dolby's encoder is far more efficient).
- zlib: LibreMPEG built with `--disable-autodetect` lacks zlib and copies zlib-compressed MKV tracks (PGS subtitles) still compressed = corrupted. CI builds with `--enable-zlib`; `check_encoder` refuses builds without zlib or the peak option; `_check_copied_packets` compares every copied track (first 5 min / whole clip) against the original, both read by the standard FFmpeg.

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

1. **Progress / speed of in-place jobs**: all jobs show percentage/speed/ETA from `run_ffmpeg()`; `_rewrite_in_place()` (AC-3/E-AC-3 and track removal) probes the duration and also shows MB/s written. Track removal is still a full rewrite (MKV interleaves tracks), so a 40 GB film on a USB/HDD takes many minutes - disk-bound, not CPU-bound.

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
scanner_ui.py             # Library Scanner tab + dialogs
library_scanner.py        # Recursive scan, cache, progress
media_inspector.py        # ffprobe/MediaInfo/mkvmerge/bitstream inspection
atmos_inspector.py        # E-AC-3 JOC / TrueHD Atmos parsing + classification
compatibility_rules.py    # Rules, device profiles, subtitle policy
library_repair.py         # Repair plans, validation, batches
job_queue.py              # Persistent queue + runner + track references
queue_jobs.py             # Queue job builders / executors
queue_ui.py               # Queue tab
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
