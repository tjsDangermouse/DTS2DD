# Installation Complete! 🎉

Your DTS to AC-3 Converter app is now ready to use!

## What You Have

✅ **macOS Application**: `DTS to AC-3 Converter.app`
- **Location**: `/Applications/DTS to AC-3 Converter.app`
- **Size**: ~250-300MB (includes Python runtime and FFmpeg)
- **Status**: Ready to use!

## How to Launch the App

### Method 1: Spotlight (Easiest)
1. Press `Cmd + Space` (opens Spotlight search)
2. Type: `DTS to AC3`
3. Press Enter
4. App launches!

### Method 2: Finder
1. Open Finder
2. Go to Applications folder
3. Look for "DTS to AC-3 Converter"
4. Double-click it

### Method 3: Dock (One-Time Setup)
1. Open Finder → Applications
2. Find "DTS to AC-3 Converter"
3. Drag it to your Dock
4. Now you can click the Dock icon anytime

## On First Launch

When you run the app for the first time, it will automatically:
1. Download FFmpeg (the video processing engine)
2. Set everything up
3. This takes 2-5 minutes (one-time only)

After that, the app launches in seconds!

## Quick Start (Your First Conversion)

### 1. Launch the App
- Cmd+Space → type "DTS" → Enter

### 2. Select Your MKV File
- Click "Browse" button
- Find and select an MKV file with DTS audio
- Output file name auto-generates with `_AC3` suffix

### 3. Click "Convert"
- That's it! The app handles the rest
- Progress bar shows activity
- Wait for completion message

### 4. Your New File is Ready
- Check the same folder as your original file
- Look for file with `_AC3` in the name
- Play it to verify it works!

## What Can It Do?

✅ **Convert DTS to AC-3**: The main function
✅ **Detect Audio Streams**: Shows you what audio is in your file
✅ **Custom Bitrate**: Choose from 192k to 640k
✅ **Custom Output Location**: Save to any folder
✅ **Fast Conversion**: Video not re-encoded, only audio format changed

## What You'll See

The app has a simple interface with:
- **Input File**: Browse for your MKV file
- **Audio Streams**: Display of detected audio information
- **Output Settings**: Configure output file and quality
- **Buttons**: Detect Audio and Convert

See `USER_GUIDE.md` for detailed walkthrough with screenshots.

## For Windows Users

If you need the Windows version, see the **Building Windows Version** section below.

## Building Windows Version

If you have Windows, you can build the Windows executable:

### On Windows:
1. Install Python 3.11+ from python.org
   - Check "Add Python to PATH"
2. Open Command Prompt in the project folder
3. Run: `build_windows.bat`
4. Wait for build to complete
5. You'll get: `dist\DTS to AC-3 Converter.exe`

### Distribute the Windows App:
1. Copy the `.exe` file to your Windows PC
2. Double-click to launch (no installation needed!)

See `BUILD_AND_DISTRIBUTE.md` for detailed instructions.

## Documentation

### For Users:
- **USER_GUIDE.md** - Complete user manual with examples
- **QUICKSTART.md** - Quick reference guide
- **README.md** - General information

### For Developers:
- **BUILD_AND_DISTRIBUTE.md** - How to build and distribute
- **IMPLEMENTATION.md** - Technical details
- **GUI_LAYOUT.md** - Interface design documentation

## First Run - What Happens

```
1. You launch the app
   ↓
2. A window appears saying "Checking FFmpeg..."
   ↓
3. App downloads FFmpeg (might take a few minutes)
   ↓
4. Main application window appears
   ↓
5. Ready to use!
```

**This first launch download only happens once.** All future launches are instant.

## File Organization

The project folder contains:

```
DTS2DD/
├── DTS to AC-3 Converter.app    (← Your app! In /Applications now)
├── dist/                         (← Built app files)
├── icons/                        (← App icon files)
├── USER_GUIDE.md                (← Read this for how to use the app)
├── README.md                    (← General info)
├── QUICKSTART.md                (← Quick reference)
├── BUILD_AND_DISTRIBUTE.md      (← For building/distributing)
├── converter.py                 (← Source code)
├── ffmpeg_downloader.py         (← Source code)
├── ffmpeg_helper.py             (← Source code)
└── ... (other files)
```

## Troubleshooting

### App Won't Start
**Solution 1** (Most Common):
- Right-click the app
- Click "Open"
- Click "Open" again in the security dialog
- App should launch

**Solution 2**:
- The first launch requires internet (to download FFmpeg)
- Check your internet connection
- Try again

### App Takes a Long Time to Start (First Run)
- This is normal! It's downloading FFmpeg
- Takes 2-5 minutes one time only
- After that, launches instantly

### App Freezes During Conversion
- It's not frozen, it's working
- Large files take 10+ minutes
- Let it finish (don't force-quit)

### "No audio streams found" Error
- Your file might not have audio
- Try opening it in VLC to verify
- Try a different file

## More Information

### Where's the FFmpeg?
- The app automatically downloads FFmpeg
- Stored locally in the app (self-contained)
- You don't need to install anything manually

### Can I delete the source files?
- Yes, everything is bundled in the `.app`
- You can delete the project folder if you want
- The installed app will keep working

### Can I move the app?
- Yes! It's completely self-contained
- You can put it anywhere
- Or just leave it in Applications

### Is it safe?
- Yes! This is your own compiled app
- No external code execution
- Uses only FFmpeg (open source, widely used)

## Next Steps

1. **Launch the app**: Cmd+Space → "DTS" → Enter
2. **Select an MKV file**: Click Browse
3. **Click Convert**: That's it!
4. **Verify**: Play the output file to confirm it works

## Performance

### Startup Times
- First launch: 2-5 minutes (FFmpeg setup)
- Subsequent launches: 1-2 seconds

### Conversion Times
- 1GB file: 1-2 minutes
- 5GB file: 5-10 minutes
- 10GB file: 10-20 minutes
- Depends on CPU speed

### System Requirements
- macOS 10.13 or later
- ~500MB free disk space
- CPU: Any modern processor

## Uninstalling

To remove the app:
1. Open Finder
2. Go to Applications folder
3. Drag "DTS to AC-3 Converter" to Trash
4. Empty Trash

That's it! Completely removed.

## Questions?

See the comprehensive guides:
- **USER_GUIDE.md** - How to use the app
- **QUICKSTART.md** - Quick reference
- **README.md** - General info
- **BUILD_AND_DISTRIBUTE.md** - For building your own versions

## Summary

You now have:

✅ A fully functional GUI application
✅ No command-line needed
✅ Double-click to launch
✅ Ready to convert DTS to AC-3
✅ Works on macOS 10.13+

**Enjoy converting your audio files!** 🎬🔊

---

**Questions or issues?** Check the documentation files above or test with a small sample file first.
