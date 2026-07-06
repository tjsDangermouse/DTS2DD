# DTS to Dolby Digital (AC-3) MKV Converter

A cross-platform Python application with a simple GUI for converting DTS audio in MKV files to Dolby Digital (AC-3) format.

## Features

- **Automatic FFmpeg Detection & Download**: The application automatically detects FFmpeg on startup. If not found, it downloads the appropriate version for your platform.
- **Audio Stream Detection**: Displays all audio streams in the selected MKV file with codec, channels, bitrate, and language information.
- **Simple GUI**: Easy-to-use tkinter interface for file selection and conversion settings.
- **Cross-Platform**: Works on macOS and Windows.
- **Flexible Bitrate Selection**: Choose AC-3 bitrate (192k to 640k) for output.
- **Progress Indication**: Visual feedback during conversion process.

## Requirements

- **Python 3.11+** (tkinter included)
- **py7zr**: For extracting FFmpeg on macOS (automatically installed via requirements.txt)

## Installation

1. Clone or download this repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### Running the Application

```bash
python3 converter.py
```

On first run, the application will:
1. Check for FFmpeg in the system PATH
2. If not found, automatically download and extract FFmpeg to the `./ffmpeg/` directory
3. Display a progress dialog during download (macOS only)
4. Launch the main application window

### Converting a File

1. **Select Input File**: Click "Browse" in the "Input File" section to select an MKV file
2. **Detect Audio**: Click "Detect Audio" to scan for audio streams in the file
3. **Configure Output**:
   - Output file path is auto-generated with `_AC3` suffix, but can be customized
   - Select desired AC-3 bitrate (default: 384k)
4. **Convert**: Click "Convert" to start the conversion
5. The progress bar will show activity during conversion
6. A success message will appear when complete

## File Structure

```
dts_to_ac3_converter/
├── converter.py              # Main application GUI
├── ffmpeg_downloader.py      # FFmpeg auto-detection and download
├── ffmpeg_helper.py          # FFmpeg subprocess management
├── audio_stream_info.py      # Audio stream data model
├── requirements.txt          # Python dependencies
└── ffmpeg/                   # Auto-created, contains downloaded FFmpeg
    └── ffmpeg (or ffmpeg.exe on Windows)
```

## How It Works

### FFmpeg Download

The application downloads platform-specific FFmpeg builds from:
- **macOS**: https://evermeet.cx/ffmpeg/getrelease/ffmpeg/7z (static build)
- **Windows**: https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip

Downloaded files are extracted to the `./ffmpeg/` directory locally.

### Audio Detection

The application uses FFmpeg's probe functionality to scan the input file and extract:
- Stream index and type
- Audio codec (DTS, DTS-HD, AC-3, etc.)
- Sample rate
- Number of channels (5.1, 7.1, stereo, etc.)
- Bitrate
- Language (if available)

### Conversion

FFmpeg command used:
```
ffmpeg -i input.mkv -c:v copy -c:a ac3 -b:a [bitrate] output.mkv -y
```

This approach:
- Copies video stream unchanged (no re-encoding)
- Encodes audio to AC-3/Dolby Digital
- Preserves MKV container format
- Automatically overwrites output file if it exists

## Troubleshooting

### FFmpeg Download Fails

**Symptom**: Application shows error about FFmpeg download failing

**Solutions**:
1. Check your internet connection
2. Try manual installation:
   - **macOS**: `brew install ffmpeg`
   - **Windows**: Download from https://www.gyan.dev/ffmpeg/builds/ and add to PATH

### No Audio Streams Detected

**Symptom**: "Detect Audio" shows "No audio streams found"

**Solutions**:
1. Verify the file is a valid MKV container
2. Ensure the file contains audio streams
3. Try opening the file with a media player to confirm it's valid

### AC-3 Codec Not Available

**Symptom**: Conversion fails with error about AC-3 encoding

**Solutions**:
1. Verify FFmpeg installation: `ffmpeg -codecs | grep ac3`
2. If AC-3 is not available, reinstall FFmpeg or check your build includes AC-3 support

### Permission Errors

**Symptom**: "Permission denied" when writing output file

**Solutions**:
1. Verify output directory is writable
2. Check disk space available
3. Close any applications that might have the file locked

## Advanced: Manual FFmpeg Installation

If you prefer not to use auto-download, install FFmpeg manually:

**macOS (using Homebrew)**:
```bash
brew install ffmpeg
```

**Windows**:
1. Download from https://www.gyan.dev/ffmpeg/builds/
2. Extract to a directory
3. Add to PATH environment variable

The application will detect FFmpeg in your system PATH automatically.

## Supported Audio Codecs (Input)

The application can detect and display all audio codecs:
- DTS (DTS, DTS-HD)
- AC-3 (Dolby Digital)
- E-AC-3 (Dolby Digital Plus)
- AAC
- MP3
- FLAC
- OPUS
- And others supported by FFmpeg

## Output Codec

- **AC-3 (Dolby Digital)** - Selectable bitrates: 192k, 224k, 256k, 320k, 384k, 448k, 640k

## Platform Notes

### macOS
- Tested on macOS 12+
- Uses system Python or brew-installed Python
- FFmpeg downloads from evermeet.cx (static builds)

### Windows
- Tested on Windows 10+
- Python 3.11+ from python.org
- FFmpeg downloads from gyan.dev

## Performance

- Audio stream detection: 1-2 seconds for typical MKV files
- Conversion speed depends on:
  - File size
  - CPU performance
  - Output bitrate
  - Input codec complexity

For a 5GB file with DTS 5.1 audio, conversion typically takes 3-10 minutes on modern hardware.

## Future Enhancements

Possible improvements for future versions:
- Batch processing multiple files
- Preview audio player
- Support for other output codecs (E-AC-3, AAC)
- Preserve multiple audio streams
- Advanced FFmpeg options (downmixing, filtering)

## License

This project uses FFmpeg which is licensed under the LGPL license.

## Support

For issues or questions:
1. Check the Troubleshooting section above
2. Verify FFmpeg is working: `ffmpeg -version`
3. Check that your MKV file is valid: `ffmpeg -i yourfile.mkv`
