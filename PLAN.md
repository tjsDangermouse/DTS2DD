# DTS to Dolby Digital MKV Converter - Implementation Plan

## Overview
Create a cross-platform (macOS/Windows) Python application with tkinter GUI that converts DTS audio in MKV files to Dolby Digital (AC-3) format using FFmpeg as the backend.

## Prerequisites Setup Required

### 1. Python
- **macOS**: Python 3.11.9 already installed ✓
- **Windows**: Download Python 3.11+ from python.org (ensure "Add to PATH" is checked during installation)
- tkinter is included with Python on both platforms

### 2. FFmpeg (Automatic)
- Application will automatically check for FFmpeg on startup
- If not found in PATH, will download and extract to local `ffmpeg/` directory
- **macOS**: Download static build from https://evermeet.cx/ffmpeg/
- **Windows**: Download from https://www.gyan.dev/ffmpeg/builds/
- No manual installation required

## Application Architecture

### Project Structure
```
dts_to_ac3_converter/
├── converter.py              # Main application file
├── ffmpeg_helper.py          # FFmpeg process management
├── ffmpeg_downloader.py      # Auto-download FFmpeg on startup
├── audio_stream_info.py      # Data model for audio streams
├── requirements.txt          # Python dependencies (minimal)
└── ffmpeg/                   # Auto-created, contains downloaded FFmpeg binary
    └── ffmpeg(.exe)          # Platform-specific binary
```

### Core Features

#### 0. Startup FFmpeg Check (ffmpeg_downloader.py)
- **On application launch**, check for FFmpeg:
  1. Check if `ffmpeg` is in system PATH using `shutil.which('ffmpeg')`
  2. Check if `./ffmpeg/ffmpeg` (or `ffmpeg.exe` on Windows) exists locally
  3. If neither found, show progress dialog and auto-download:
     - **macOS**: Download from https://evermeet.cx/ffmpeg/ffmpeg-7.0.2.7z (or latest)
     - **Windows**: Download from https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
  4. Extract binary to `./ffmpeg/` directory
  5. Verify FFmpeg works by running `ffmpeg -version`
  6. If download/extraction fails, show error and exit with instructions for manual install

#### 1. File Input (converter.py)
- **Open File Button**: Use `tkinter.filedialog.askopenfilename()` for .mkv files
- **Drag & Drop Zone**: Use `tkinterdnd2` or display drop instructions (native drag-drop requires extra library)
  - Alternative: Simple text label showing current file path after selection

#### 2. Audio Stream Detection (ffmpeg_helper.py)
- Use FFmpeg to probe file: `ffmpeg -i input.mkv`
- Parse stderr output to extract audio stream information:
  - Stream index
  - Codec (DTS, DTS-HD, etc.)
  - Channels (5.1, 7.1, stereo)
  - Bitrate
  - Language (if available)
- Display results in UI (Text widget with scrollbar)

#### 3. Output Configuration
- **Default**: Same directory as source, filename pattern: `[original]_AC3.mkv`
- **Custom**: Browse button using `tkinter.filedialog.asksaveasfilename()`
- **Settings**: OptionMenu/Combobox for AC-3 bitrate (384k, 448k, 640k)

#### 4. Conversion Process
- FFmpeg command: `ffmpeg -i input.mkv -c:v copy -c:a ac3 -b:a 384k output.mkv -y`
- Show progress using `ttk.Progressbar` (indeterminate mode or parse FFmpeg output)
- Run FFmpeg in separate thread to prevent UI freezing
- Show completion messagebox or error details

#### 5. Error Handling
- Validate FFmpeg is installed and accessible (`shutil.which('ffmpeg')`)
- Handle missing/corrupt MKV files
- Detect if AC-3 encoding is available in FFmpeg build
- Show user-friendly error messages via `tkinter.messagebox`

## Implementation Steps

### Step 1: Create Project Directory
```bash
mkdir dts_to_ac3_converter
cd dts_to_ac3_converter
```

### Step 2: Implement ffmpeg_downloader.py
Functions:
- `get_ffmpeg_path() -> Optional[str]`: Check system PATH and local `./ffmpeg/` directory
- `download_ffmpeg() -> bool`: Download platform-specific FFmpeg binary
  - Detect platform using `platform.system()` (Darwin for macOS, Windows for Windows)
  - Download URLs:
    - macOS: https://evermeet.cx/ffmpeg/getrelease/ffmpeg/7z (static build, ~70MB)
    - Windows: https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip (~100MB)
  - Use `urllib.request` to download with progress callback
  - Extract using `py7zr` (macOS 7z) or `zipfile` (Windows zip)
  - Make binary executable on macOS: `os.chmod(path, 0o755)`
  - Return True on success, False on failure
- `ensure_ffmpeg_available() -> str`: Main function that checks, downloads if needed, returns path

### Step 3: Implement audio_stream_info.py
Simple dataclass to hold audio stream information:
- stream_index
- codec_name
- channels
- bitrate
- language

### Step 4: Implement ffmpeg_helper.py
Functions:
- `get_audio_streams(file_path: str, ffmpeg_path: str) -> List[AudioStreamInfo]`: Run FFmpeg probe and parse audio streams from stderr
- `convert_to_ac3(input_path: str, output_path: str, bitrate: str, ffmpeg_path: str, progress_callback) -> bool`: Execute conversion in subprocess with progress updates

### Step 5: Implement converter.py (Main GUI)
- **On startup**: Call `ensure_ffmpeg_available()` before showing main window
  - Show progress dialog during FFmpeg download if needed
  - Exit gracefully if FFmpeg setup fails
- Create tkinter window with:
  - Frame 1: File selection (Label, Entry, Browse Button)
  - Frame 2: Audio info display (Text widget with scrollbar)
  - Frame 3: Output settings (Entry for output path, Browse button, Bitrate dropdown)
  - Frame 4: Convert button and Progress bar
- Wire up button callbacks
- Use threading.Thread for FFmpeg conversion to prevent UI freeze
- Update progress bar during conversion

### Step 6: Test on macOS
- Delete any existing `./ffmpeg/` directory to test auto-download
- Run: `python3 converter.py`
- Verify FFmpeg downloads automatically on first run
- Test with sample MKV file containing DTS audio
- Verify cross-platform paths work correctly

## Critical Files to Create
- `ffmpeg_downloader.py` - Auto-detect and download FFmpeg on startup
- `audio_stream_info.py` - Data model for audio stream information
- `ffmpeg_helper.py` - FFmpeg subprocess management and parsing
- `converter.py` - Main GUI application (tkinter)
- `requirements.txt` - Dependencies: `py7zr` (for extracting 7z on macOS)

## FFmpeg Integration Details

### Probing Command
```bash
ffmpeg -i "input.mkv" 2>&1
```
Parse stderr output for lines containing "Stream #" and "Audio:"

Example output:
```
Stream #0:1(eng): Audio: dts (DTS), 48000 Hz, 5.1(side), fltp, 1536 kb/s
```

### Conversion Command
```bash
ffmpeg -i "input.mkv" -c:v copy -c:a ac3 -b:a 384k "output.mkv" -y -progress pipe:1
```
- `-c:v copy`: Copy video stream unchanged (no re-encoding)
- `-c:a ac3`: Encode audio to AC-3/Dolby Digital
- `-b:a 384k`: Audio bitrate (standard for 5.1)
- `-y`: Overwrite output if exists
- `-progress pipe:1`: Output progress info for parsing

### Progress Parsing
FFmpeg outputs progress as key=value pairs when using `-progress pipe:1`:
```
frame=1234
fps=50.00
time=00:00:30.00
```

## Verification Plan

### Manual Testing on macOS
1. Install py7zr: `pip3 install py7zr`
2. **Test auto-download**: Ensure FFmpeg is NOT in PATH and `./ffmpeg/` doesn't exist
3. Run the application: `python3 converter.py`
4. Verify FFmpeg downloads automatically (progress dialog should appear)
5. Verify application launches after download completes
6. Click "Browse" and select an MKV file with DTS audio
7. Verify audio stream information displays correctly
8. Click Convert and verify:
   - Progress bar shows activity (indeterminate or percentage)
   - Output file is created in correct location
   - Audio codec is AC-3 (verify with: `ffmpeg -i output.mkv`)
9. Test error cases:
   - Invalid file path
   - Simulate download failure (disconnect network during startup)
   - Output location is read-only

### Manual Testing on Windows
1. Install Python 3.11+ from python.org
2. Install py7zr: `pip install py7zr`
3. Ensure FFmpeg is NOT in PATH
4. Run: `python converter.py`
5. Verify FFmpeg downloads automatically
6. Perform same functional tests as macOS

### FFmpeg Codec Verification
Before running conversion, verify AC-3 encoding support:
```bash
ffmpeg -codecs | grep ac3
```
Should show: `DEA` (Decode, Encode, Audio support)

## Assumptions
- Python 3.11+ installed on both platforms
- tkinter is available (included with Python)
- FFmpeg will be in PATH on both platforms
- User has write permissions to output directories
- Source files are valid Matroska containers

## Future Enhancements (Not in Initial Scope)
- Batch processing multiple files
- Preview audio with embedded player
- Support for other output formats (E-AC3, AAC)
- Preserve multiple audio streams
- Advanced FFmpeg options (custom filters, downmix options)
