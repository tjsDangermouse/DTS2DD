# Quick Start Guide

## Installation (First Time Only)

### Step 1: Install Python Dependencies
```bash
pip install -r requirements.txt
```

This installs `py7zr` which is needed for extracting FFmpeg on macOS.

### Step 2: Run the Application
```bash
python3 converter.py
```

**First run only**: The application will automatically download and extract FFmpeg (~70-100MB depending on platform). This may take 1-5 minutes. A progress dialog will appear on macOS.

## Using the Application

### Basic Workflow

1. **Launch**: `python3 converter.py`

2. **Select File**:
   - Click "Browse" button in "Input File" section
   - Select an MKV file containing DTS audio
   - Output filename is auto-generated with "_AC3" suffix

3. **Detect Audio**:
   - Click "Detect Audio" button
   - Application scans the file
   - Audio streams display in the text area
   - Shows codec, channels, bitrate, language

4. **Configure Output** (Optional):
   - Change output path if desired (use "Browse" button)
   - Select AC-3 bitrate from dropdown (default: 384k)
   - Common bitrates:
     - 384k: Good quality, standard for 5.1
     - 448k: Higher quality
     - 640k: Highest quality

5. **Convert**:
   - Click "Convert" button
   - Progress bar shows activity
   - Takes several minutes depending on file size
   - Success message appears when done

6. **Verify**:
   - Open output file in media player
   - Confirm audio plays correctly
   - Optional: Check file with FFmpeg:
     ```bash
     ffmpeg -i output.mkv
     ```

## Common Tasks

### Convert a Single File
```
1. Launch app
2. Click Browse, select file.mkv
3. Click Detect Audio (optional, just to verify)
4. Click Convert
5. Done!
```

### Convert Multiple Files (Manual)
```
1. Launch app
2. For each file:
   - Browse and select file
   - Click Convert
   - Wait for completion
   - Repeat
```

### Use Custom Output Bitrate
```
1. Select input file
2. In "Output Settings", click dropdown next to "AC-3 Bitrate"
3. Select desired bitrate (192k - 640k)
4. Click Convert
```

### Save to Different Directory
```
1. Select input file
2. In "Output Settings", click Browse button next to "Output File"
3. Navigate to desired directory
4. Enter filename or accept default
5. Click Save
6. Click Convert
```

## Troubleshooting

### "FFmpeg not found" Error

**On First Run**:
- Check internet connection
- Click OK to dismiss error
- Application will try to download FFmpeg again

**Solution**:
1. Ensure internet connection is active
2. Try running again: `python3 converter.py`
3. If still fails, install manually:
   - macOS: `brew install ffmpeg`
   - Windows: Download from https://www.gyan.dev/ffmpeg/builds/

### "No audio streams found" Error

**Cause**: File doesn't contain audio or is not a valid MKV file

**Solution**:
1. Verify file is a valid MKV
2. Try opening in media player (VLC, etc.)
3. Use FFmpeg to check: `ffmpeg -i yourfile.mkv`

### Conversion Fails

**Cause**: Various possible issues

**Solutions**:
1. Verify input file exists and is readable
2. Verify output directory exists and is writable
3. Check available disk space
4. Try a smaller file first to test
5. Check error message for details

### Application Freezes During Audio Detection

**Cause**: Scanning large file or FFmpeg taking time

**Solution**:
1. Wait - large files can take 1-2 minutes
2. If frozen for >5 minutes, close and restart
3. Try with smaller file first

## File Requirements

### Input Format
- **File Type**: Matroska (MKV)
- **Audio**: Any codec (will be converted to AC-3)
- **Size**: No limit (but large files take longer)
- **Codec Support**: All codecs supported by FFmpeg work

### Output Format
- **File Type**: Matroska (MKV)
- **Audio**: AC-3 (Dolby Digital)
- **Video**: Unchanged from input (not re-encoded)
- **Bitrate**: Configurable (192k-640k)

## Performance Expectations

- **Audio Detection**: 1-2 seconds per file
- **Conversion Speed**: Depends on file size
  - 1GB file: ~1-2 minutes
  - 5GB file: ~5-10 minutes
  - 10GB file: ~10-20 minutes

Note: These are typical times on modern hardware. Older systems may be slower.

## Command Line Verification

After conversion, verify the output:

```bash
# Check if file has audio
ffmpeg -i output.mkv

# Look for AC-3 audio line, should show:
# Stream #0:1: Audio: ac3, 48000 Hz, 5.1(side), fltp, 384 kb/s
```

## Advanced: Running Without GUI Download

If FFmpeg is already installed, you can:

1. Install manually:
   - macOS: `brew install ffmpeg`
   - Windows: Add ffmpeg.exe to PATH

2. Application will detect and use existing installation
3. No auto-download will occur

## Advanced: Using Specific FFmpeg Build

To use a specific FFmpeg installation:

1. Place `ffmpeg` (or `ffmpeg.exe`) in `./ffmpeg/` directory
2. Application will use local version automatically
3. Or ensure ffmpeg is in system PATH

## Tips for Best Results

1. **Choose Right Bitrate**:
   - 384k: Good for 5.1 surround, typical choice
   - 448k: Better quality, slightly larger file
   - 640k: Best quality, larger file size
   - 192k-256k: Lower quality, smaller file

2. **Check Output**:
   - Always verify audio works in media player
   - Use VLC or other player that supports AC-3

3. **Disk Space**:
   - Output file usually slightly smaller than input
   - Ensure at least 50% free space of input file size

4. **CPU Usage**:
   - Conversion uses 1-2 CPU cores
   - Computer remains usable during conversion
   - Close other applications for faster conversion

## Getting Help

1. **Check File Validity**:
   ```bash
   ffmpeg -i yourfile.mkv
   ```

2. **Check FFmpeg Installation**:
   ```bash
   ffmpeg -version
   ```

3. **Check FFmpeg AC-3 Support**:
   ```bash
   ffmpeg -codecs | grep ac3
   ```
   Should show: `DEA` (Decode, Encode, Audio)

4. **See README.md** for more detailed documentation

## Next Steps

1. Install dependencies: `pip install -r requirements.txt`
2. Run application: `python3 converter.py`
3. Select your first MKV file and convert!

Enjoy your DTS to AC-3 conversions!
