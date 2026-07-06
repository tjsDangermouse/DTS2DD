# DTS to AC-3 Converter - User Guide

A simple, easy-to-use application to convert DTS audio in MKV files to Dolby Digital (AC-3) format.

## Installation

### macOS

1. **Download the App**
   - Get `DTS to AC-3 Converter.app` (approximately 200-300MB)

2. **Install to Applications**
   - Drag and drop the app to your Applications folder
   - Or double-click the installer (DMG file)

3. **First Launch**
   - Open Spotlight (Cmd + Space)
   - Type "DTS to AC-3 Converter"
   - Press Enter
   - **First time only**: App may take a moment as it prepares FFmpeg

4. **Trust the Application**
   - If you get a security warning: "App is from an unidentified developer"
   - Right-click the app
   - Select "Open"
   - Click "Open" button

### Windows

1. **Download the App**
   - Get `DTS to AC-3 Converter.exe` (approximately 150-250MB)

2. **Run the App**
   - Double-click the executable
   - Windows may show a security prompt
   - Click "Run anyway" or "Yes"

3. **Create Shortcut (Optional)**
   - Right-click `DTS to AC-3 Converter.exe`
   - Select "Send to" → "Desktop (create shortcut)"
   - Now you can launch from desktop

## Using the Application

### Step-by-Step Guide

#### 1. Select Your MKV File

```
┌─────────────────────────────────┐
│ Input File                      │
├─────────────────────────────────┤
│ MKV File: [        ] [Browse]   │
└─────────────────────────────────┘
```

- Click the **Browse** button
- Choose an MKV file containing DTS audio
- The output filename is automatically created

#### 2. Detect Audio Streams (Optional but Recommended)

```
┌─────────────────────────────────┐
│ Audio Streams                   │
├─────────────────────────────────┤
│ (Area shows detected audio)     │
│                                 │
│ [ Detect Audio ]                │
└─────────────────────────────────┘
```

- Click **Detect Audio** to verify audio streams
- You'll see what audio formats are in your file
- This helps confirm the file has audio to convert

#### 3. Set Output Options (Usually Use Defaults)

```
┌─────────────────────────────────┐
│ Output Settings                 │
├─────────────────────────────────┤
│ Output File: [      ] [Browse]  │
│ AC-3 Bitrate: [384k ▼]          │
└─────────────────────────────────┘
```

**Output File**:
- Default: Original location with `_AC3` added to filename
- Example: `movie.mkv` → `movie_AC3.mkv`
- Change only if you want a different location

**AC-3 Bitrate** (Audio Quality):
- **384k** (Default) - Good quality, standard choice for 5.1
- **448k** - Higher quality
- **640k** - Best quality (larger file)
- **192k-256k** - Lower quality (smaller file)

**Recommended**: Leave as 384k unless you have a specific reason to change it.

#### 4. Start Conversion

```
┌─────────────────────────────────┐
│ [████████░░░░░░░░░░░░░░░░░░░░░░]│
│ [ Convert ]                     │
└─────────────────────────────────┘
```

- Click **Convert**
- Progress bar shows activity
- Wait for completion

**Time estimates**:
- 1GB file: 1-2 minutes
- 5GB file: 5-10 minutes
- 10GB file: 10-20 minutes

#### 5. Success!

When done, you'll see a message: **"Conversion completed successfully!"**

Your new AC-3 audio file is ready to use!

## Common Scenarios

### Scenario 1: Simple Conversion

1. Click Browse → Select `movie.mkv`
2. Click Detect Audio (optional, just to verify)
3. Click Convert
4. Wait for completion
5. Done! File `movie_AC3.mkv` is ready

### Scenario 2: Convert Multiple Files

1. Convert first file (see above)
2. Click Browse again
3. Select second file
4. Adjust output settings if needed
5. Click Convert
6. Repeat for each file

### Scenario 3: Custom Output Location

1. Click Browse → Select input file
2. In "Output Settings", click Browse button next to "Output File"
3. Navigate to desired folder
4. Type custom filename (or use default)
5. Click Save
6. Click Convert

### Scenario 4: Higher Quality Output

1. Select your file
2. In "Output Settings", change AC-3 Bitrate to **640k**
3. Click Convert
4. File will be higher quality but slightly larger

## Troubleshooting

### Problem: "File not found" Error

**Cause**: The MKV file is not where the app thinks it is

**Solution**:
1. Move the MKV file to a known location
2. Click Browse again
3. Manually select the file

### Problem: "No audio streams found"

**Cause**: File doesn't contain audio or isn't a valid MKV

**Solution**:
1. Open the file in a media player (VLC, etc.) to verify it plays
2. Verify the file is actually an MKV (check file name ends in `.mkv`)
3. Try a different file to test

### Problem: Conversion Fails

**Cause**: Various possible reasons

**Solution**:
1. Verify you have free disk space (at least as large as input file)
2. Verify output folder is writable (not read-only)
3. Try a smaller file first to test
4. Restart the application

### Problem: App Won't Start

**macOS**:
1. Open System Preferences → Security & Privacy
2. Find the app in the list
3. Click "Allow" or "Open anyway"

**Windows**:
1. Right-click the `.exe` file
2. Select "Run as administrator"
3. Click "Yes" when prompted

### Problem: App Is Taking a Long Time

**First Run Only**:
- The app may download and prepare FFmpeg the first time
- This is normal and happens only once
- Can take 2-5 minutes

**During Conversion**:
- Large files take proportionally longer
- The app is not frozen; just processing
- Leave it running and be patient

## File Compatibility

### Input Files Supported

- **Container**: Matroska (MKV, MKA)
- **Video Codecs**: Any
- **Audio Codecs**: All (DTS, DTS-HD, AC-3, E-AC-3, AAC, MP3, FLAC, etc.)
- **File Size**: No limit (but very large files take longer)

### Output Format

- **Container**: Matroska (MKV)
- **Audio Format**: AC-3 (Dolby Digital)
- **Video**: Unchanged from input (no re-encoding = fast!)
- **Bitrate**: Your choice (192k to 640k)

## Performance Tips

1. **Disk Space**
   - Keep at least 50% of input file size as free space
   - SSD recommended for faster conversion

2. **Computer Performance**
   - Close other applications for faster conversion
   - The app uses 1-2 CPU cores; computer remains usable
   - Conversion speed depends on CPU

3. **Quality vs Size**
   - 384k: Good balance of quality and file size (recommended)
   - 448k+: Better quality, larger files
   - 192k-256k: Smaller files, lower quality

## After Conversion

### Verify Your File

You can verify the conversion worked:

**Option 1: Play It**
- Open the output file in your media player
- Audio should play correctly
- No errors or glitches should occur

**Option 2: Verify Format (Mac/Linux)**
If you want to verify the audio codec:
1. Open Terminal
2. Run: `ffmpeg -i output_file.mkv`
3. Look for line with "Audio: ac3"

## Common Questions

**Q: Will this damage my original file?**
A: No, the original file is never modified. A new file is created.

**Q: Can I delete the original file?**
A: Yes, after verifying the converted file plays correctly. Always keep a backup of important files.

**Q: What if the output file is larger than input?**
A: This occasionally happens due to MKV container differences. Both files should play fine.

**Q: Can I convert other audio formats (MP3, AAC)?**
A: Yes! The app detects all audio formats and converts any to AC-3.

**Q: Why does it take so long on first run?**
A: The app automatically downloads FFmpeg (the video processing engine). This happens only once.

**Q: Can I move the app after installing?**
A: Yes! You can move it anywhere. It's completely self-contained.

**Q: Do I need internet after installation?**
A: No, internet is only needed on first run for FFmpeg download.

## System Requirements

### macOS
- macOS 10.13 or later
- ~500MB free space for app
- Apple Silicon or Intel Mac

### Windows
- Windows 10 or later
- ~500MB free space for app
- 64-bit system

## Getting Help

1. **Verify your file** is a real MKV: Try opening it in VLC
2. **Check disk space**: Ensure you have free space
3. **Try a different file**: To test if it's file-specific or app-specific
4. **Restart the app**: Close and reopen
5. **Restart your computer**: Sometimes helps with file access issues

## Tips for Best Results

1. **Use standard bitrates**: 384k is usually best (most devices support it)
2. **Keep original file**: Until you verify the output works
3. **Test on different devices**: If you need AC-3 on specific devices
4. **Don't interrupt**: Let conversion complete (don't force-quit)
5. **Give it time**: Large files can take 10+ minutes

## Uninstalling

### macOS
1. Open Applications folder
2. Drag "DTS to AC-3 Converter" to Trash
3. Empty Trash

### Windows
1. Open Programs & Features (Settings → Apps)
2. Find "DTS to AC-3 Converter"
3. Click Uninstall

## Support

For issues or questions, refer to:
- **README.md** - Technical details
- **QUICKSTART.md** - Quick reference
- **BUILD_AND_DISTRIBUTE.md** - For developers

Enjoy converting your DTS audio to AC-3!
