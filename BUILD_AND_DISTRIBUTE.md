# Building and Distributing DTS to AC-3 Converter

This guide covers building standalone applications for macOS and Windows that can be double-clicked to launch.

## Overview

The project includes automated build scripts that create:
- **macOS**: `DTS to AC-3 Converter.app` - A native app bundle
- **Windows**: `DTS to AC-3 Converter.exe` - A standalone executable

Both include the Python runtime and all dependencies, so no installation is required.

## Prerequisites

### macOS
- Python 3.11+ (already installed)
- Build tools: `xcode-select --install` (if not already installed)
- ~500MB free disk space for build

### Windows
- Python 3.11+ from [python.org](https://www.python.org/)
  - **Important**: Check "Add Python to PATH" during installation
- ~500MB free disk space for build

## Building on macOS

### Quick Build (Recommended)

```bash
cd /path/to/DTS2DD
./build_macos.sh
```

The script will:
1. Install build dependencies
2. Generate application icon
3. Build the `.app` bundle
4. Output the app to `dist/DTS to AC-3 Converter.app`

### Manual Build Steps

If the script fails, build manually:

```bash
# Install dependencies
pip install pyinstaller pillow py7zr

# Create icons
python3 create_icon.py

# Build app
pyinstaller --clean converter.spec
```

### Installing to Applications Folder

After successful build:

```bash
# Copy app to Applications
cp -r "dist/DTS to AC-3 Converter.app" /Applications/

# You can then delete the dist folder if desired
rm -rf dist build *.spec
```

### Running the macOS App

Once installed:
1. Open Finder
2. Navigate to Applications
3. Double-click "DTS to AC-3 Converter"
4. App launches automatically

**Alternative**: Use Spotlight (Cmd+Space, type app name)

### Code Signing (Optional)

If distributing to others, sign the app:

```bash
# Self-sign the app (free, but shows warning on first launch)
codesign --deep --force --verify --verbose \
  --sign - "/Applications/DTS to AC-3 Converter.app"

# Users will see: "This app is from an unidentified developer"
# They can override in Security & Privacy settings
```

To distribute without warnings, you would need an Apple Developer certificate ($99/year).

## Building on Windows

### Quick Build (Recommended)

```bash
cd path\to\DTS2DD
build_windows.bat
```

The script will:
1. Install build dependencies
2. Generate application icon
3. Build the `.exe` file
4. Output to `dist\DTS to AC-3 Converter.exe`

### Manual Build Steps

If the script fails, build manually:

```bash
# Install dependencies
pip install pyinstaller pillow py7zr

# Create icons
python create_icon.py

# Build executable
pyinstaller --clean converter.spec
```

### Installing to Start Menu/Desktop

After successful build, you can:

**Option 1: Create Start Menu Shortcut**
```bash
# In dist folder, right-click DTS to AC-3 Converter.exe
# Send to > Desktop (create shortcut)
# Move shortcut to Start Menu folder
```

**Option 2: Move to Program Files**
```bash
# Create folder in Program Files
mkdir "C:\Program Files\DTS to AC-3 Converter"

# Copy executable
copy "dist\DTS to AC-3 Converter.exe" "C:\Program Files\DTS to AC-3 Converter\"

# Create shortcut on Desktop
# Right-click Desktop > New > Shortcut
# Location: C:\Program Files\DTS to AC-3 Converter\DTS to AC-3 Converter.exe
```

### Running the Windows App

Once installed:
1. Double-click the `.exe` file or shortcut
2. App launches automatically
3. No terminal window appears

## Project Structure

After build, you'll have:

```
dts_to_ac3_converter/
├── build/                          # Build artifacts (temporary)
├── dist/                           # Final output
│   ├── DTS to AC-3 Converter.app   # macOS only
│   ├── DTS to AC-3 Converter.exe   # Windows only
│   └── ... (Python runtime)
├── icons/                          # App icons
│   ├── converter_icon.icns        # macOS icon
│   ├── converter_icon.ico         # Windows icon
│   └── converter_icon.png         # Source PNG
├── converter.py                   # Main application
├── converter.spec                 # PyInstaller configuration
├── build_macos.sh                 # macOS build script
├── build_windows.bat              # Windows build script
└── ... (other files)
```

## Clean Build

To perform a clean build (removes previous artifacts):

**macOS:**
```bash
rm -rf build dist *.spec.bak
./build_macos.sh
```

**Windows:**
```bash
rmdir /s /q build dist
build_windows.bat
```

## Troubleshooting Build

### "pyinstaller: command not found"

**Solution**: Install PyInstaller directly
```bash
pip install pyinstaller
```

### "No module named 'converter'"

**Solution**: Run build script from project directory
```bash
cd /path/to/dts_to_ac3_converter
./build_macos.sh  # or build_windows.bat
```

### Icon not appearing

**Solution**: Regenerate icons
```bash
python3 create_icon.py
```

### Build is very large (>200MB)

This is normal. PyInstaller bundles the entire Python runtime.

**Options to reduce size:**
- Delete `dist/` after copying app to Applications
- Distribute only the `.app` or `.exe`, not the whole dist folder

### App won't launch

**macOS**:
1. Right-click app, select "Open"
2. Click "Open" in security dialog
3. App should launch

**Windows**:
1. If UAC prompt appears, click "Yes"
2. If antivirus blocks, add exception

## Creating Installation Packages

### macOS: Create DMG

Create a professional installer:

```bash
# After building the app
brew install create-dmg

create-dmg \
  --volname "DTS to AC-3 Converter" \
  --volicon "icons/converter_icon.icns" \
  --window-pos 200 120 \
  --window-size 600 400 \
  --icon-size 100 \
  --icon "DTS to AC-3 Converter.app" 150 200 \
  --hide-extension "DTS to AC-3 Converter.app" \
  --app-drop-link 450 200 \
  dist/DTS-to-AC3-Converter.dmg \
  dist/DTS\ to\ AC-3\ Converter.app
```

Users can then:
1. Download DMG
2. Double-click to mount
3. Drag app to Applications folder

### Windows: Create Installer

Use NSIS (free and open source):

1. Install NSIS from https://nsis.sourceforge.io/
2. Create `installer.nsi`:

```nsis
; Basic NSIS installer script
!include "MUI2.nsh"

Name "DTS to AC-3 Converter"
OutFile "dist/DTS-to-AC3-Converter-Installer.exe"
InstallDir "$PROGRAMFILES\DTS to AC-3 Converter"

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetOutPath "$INSTDIR"
  File "dist\DTS to AC-3 Converter.exe"
  CreateShortcut "$SMPROGRAMS\DTS to AC-3 Converter.lnk" \
    "$INSTDIR\DTS to AC-3 Converter.exe"
SectionEnd
```

3. Build: `makensis installer.nsi`

## Distribution Checklist

### Before Distributing

- [ ] Tested app thoroughly
- [ ] App launches without errors
- [ ] File dialog works correctly
- [ ] Conversion completes successfully
- [ ] Error messages are user-friendly
- [ ] Icons display correctly
- [ ] App name/version is correct
- [ ] README and documentation included

### Package Contents

Include with distribution:
- [ ] Executable or app bundle
- [ ] README.md (user guide)
- [ ] QUICKSTART.md (for first-time users)
- [ ] License information
- [ ] Any required system libraries (usually not needed)

### Supported Platforms

- **macOS**: 10.13+ (Intel and Apple Silicon)
- **Windows**: Windows 10+ (x86-64)

## Updating the Application

### Modifying Code

1. Edit source files (`.py` files)
2. Test changes: `python3 converter.py`
3. Rebuild: `./build_macos.sh` or `build_windows.bat`
4. Replace old version with new build

### Version Bumping

Update version in code:

**converter.py**:
```python
APP_VERSION = "1.1.0"
```

Update window title to show version:
```python
self.root.title(f"DTS to AC-3 Converter v{APP_VERSION}")
```

Then rebuild the app.

## Performance & Size

### App Size
- **macOS**: 200-300MB (varies by Python version)
- **Windows**: 150-250MB (varies by Python version)

Size includes:
- Complete Python runtime
- All standard library modules
- tkinter
- py7zr and dependencies
- Application code

### Startup Time
- **First launch**: 1-2 seconds
- **Subsequent launches**: 1-2 seconds
- **FFmpeg download** (first run): 2-5 minutes (one-time only)

### Runtime
- **Minimal**: ~50MB RAM (idle)
- **Converting**: ~100-200MB RAM (depends on file size)

## Advanced: Building for Distribution

### Cross-Platform Building

To build for Windows on macOS:
```bash
# Install Windows build tools
pip install pyinstaller[windows]

# Build for Windows
pyinstaller --clean --target-arch x64 converter.spec
```

### Building on CI/CD

Use GitHub Actions or similar to automate builds:

```yaml
name: Build
on: [push, pull_request]

jobs:
  build-macos:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: '3.11'
      - run: ./build_macos.sh
      - uses: actions/upload-artifact@v2
        with:
          name: DTS-to-AC3-macOS
          path: dist/DTS\ to\ AC-3\ Converter.app

  build-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: '3.11'
      - run: build_windows.bat
      - uses: actions/upload-artifact@v2
        with:
          name: DTS-to-AC3-Windows
          path: dist/DTS to AC-3 Converter.exe
```

## Cleanup After Build

To reduce repository size:

```bash
# Remove build artifacts
rm -rf build dist *.spec.bak __pycache__

# Keep only source and scripts
# Before committing to git
```

## Legal Considerations

### Licensing

- **Application**: Use the same license as the original project
- **FFmpeg**: Includes FFmpeg (LGPL licensed)
  - Include FFmpeg license in distribution
  - Provide source code link (FFmpeg source available at https://ffmpeg.org/)

### Distribution Terms

When distributing:
1. Include this LICENSE file from the project
2. Include FFmpeg's license
3. Include source code links for GPL/LGPL compliance

## Next Steps

1. Run build script: `./build_macos.sh` or `build_windows.bat`
2. Test the executable
3. Move to Applications folder
4. Test functionality
5. Distribute to users

## Support

If build fails:
1. Check Python version: `python3 --version` (should be 3.11+)
2. Update pip: `pip install --upgrade pip`
3. Reinstall dependencies: `pip install --force-reinstall pyinstaller pillow py7zr`
4. Try clean build: `rm -rf build dist && ./build_macos.sh`

For more help, see the main README.md and QUICKSTART.md files.
