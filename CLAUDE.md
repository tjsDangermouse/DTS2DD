# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DTS to AC-3/DD+ Converter: A cross-platform GUI application that converts DTS audio in MKV files to AC-3 (Dolby Digital) or E-AC-3 (Dolby Digital Plus) format. The app automatically detects and downloads FFmpeg on first run, making it completely self-contained.

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

**ffmpeg_helper.py** - FFmpeg subprocess management
- `get_audio_streams()`: Probes MKV file with FFmpeg, parses stderr using regex to extract codec, channels, bitrate, language
- `convert_to_ac3()`: Executes FFmpeg conversion with codec support (ac3 or eac3)
- Uses subprocess.run() for reliable file I/O and proper buffering

**audio_stream_info.py** - Data model
- Simple `AudioStreamInfo` dataclass holding stream metadata
- Used throughout app for displaying detected audio information

### Threading Model

All long-running operations run in background daemon threads to keep GUI responsive:
- FFmpeg initialization checks on app startup
- Audio stream detection when file selected
- Conversion process during encoding

Main thread uses `root.after()` to safely update GUI from worker threads.

### Key Design Decisions

1. **Automatic FFmpeg Download**: Eliminates user setup friction. Platform-specific builds ensure compatibility.

2. **Recursive FFmpeg Search**: Extracted archives may nest FFmpeg in subdirectories; `rglob()` finds it regardless of nesting depth.

3. **Subprocess.run() for Conversion**: Simpler than Popen with threading. Provides proper file buffering so output file grows during conversion.

4. **Codec Selection**: E-AC-3 (eac3) supports higher bitrates (up to 768k) for better quality preservation when converting from high-bitrate DTS sources.

5. **Activity Log**: Real-time logging in GUI shows all operations and progress, helping users understand what's happening.

## Building for Distribution

### macOS Build
```bash
./build_macos.sh
```
Outputs: `dist/DTS to AC-3 Converter.app`
Install to Applications: `cp -r dist/"DTS to AC-3 Converter.app" /Applications/`

### Windows Build
```bash
build_windows.bat
```
Outputs: `dist/DTS to AC-3 Converter.exe`

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

1. **No Real-time Progress Percentage**: FFmpeg's output capture introduces complexity. Current implementation shows "Encoding in progress..." then "100%" on completion. To add percentage, would need proper Duration parsing and out_time_ms tracking.

2. **Nested FFmpeg Directories**: Windows ZIP extraction creates nested folders. Handled by `rglob()` search in `get_ffmpeg_path()`.

3. **Single Audio Stream**: App converts only one audio stream. To preserve multiple streams, would need to detect all audio streams and handle muxing separately.

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
ffmpeg_helper.py          # FFmpeg integration
audio_stream_info.py      # Data model
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
