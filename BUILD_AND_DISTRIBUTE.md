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

The Windows deliverable is an installer, `DTS2DD-Setup-<version>.exe`
(Inno Setup), containing a PyInstaller **one-folder** build plus FFmpeg and
LibreMPEG. One-folder starts quickly (a single-file exe unpacks ~200 MB to
%TEMP% on every launch) and is less likely to upset antivirus software.

```
%LOCALAPPDATA%\Programs\DTS2DD\
  DTS to AC-3 Converter.exe     PyInstaller launcher
  _internal\                    Python runtime and app modules
  ffmpeg\ffmpeg.exe, ffprobe.exe        standard FFmpeg (gyan.dev, pinned)
  librempeg\ffmpeg.exe, ffprobe.exe     LibreMPEG (TrueHD), pinned commit
  licenses\                     third-party licences and source pointers
  unins000.exe                  uninstaller
```

The app looks for these folders beside its exe first, writes nothing into its
program folder, keeps settings in `%APPDATA%\DTS2DD`, and only downloads FFmpeg
(into `%LOCALAPPDATA%\DTS2DD\ffmpeg`) if the bundled copy is missing.

### Recommended: GitHub Actions

`.github/workflows/windows-installer.yml` runs on every push to `master` or
`feature/**`, on pull requests, on `v*` tags, and manually ("Run workflow"):

1. Builds LibreMPEG from the pinned commit with MSYS2 (statically linked;
   cached after the first run, which takes ~20-30 min) and fails if it depends
   on MinGW DLLs or lacks the TrueHD 7.1 encoder / `side_data` filter.
2. Downloads the pinned FFmpeg build and checks its SHA-256.
3. Runs the full test suite (unit + integration) **on Windows** with those
   binaries; fails if integration tests were skipped.
4. Builds the app folder, adds the tools and licences, and runs
   `"DTS to AC-3 Converter.exe" --self-test` on it.
5. Compiles the installer, installs it silently, checks the Start menu
   shortcut, self-tests the **installed** app (it must use its bundled
   FFmpeg and LibreMPEG), then uninstalls it.
6. Uploads the installer, self-test reports and test results as the
   workflow artifact. On a `v1.2.3` tag (must match `version.py`) it also
   attaches the installer to a GitHub release.

To release: bump `version.py`, commit, `git tag v1.1.0 && git push --tags`.

To update FFmpeg: change `FFMPEG_VERSION`, `FFMPEG_URL` and `FFMPEG_SHA256` in
the workflow (`https://www.gyan.dev/ffmpeg/builds/release-version` and the
`.sha256` file next to the zip give the values). To update LibreMPEG: change
`LIBREMPEG_COMMIT` and re-verify (the self-test and tests must pass).

### Local build (`build_windows.bat`)

Needs Python 3.11+, and for the installer Inno Setup 6.3+. Put the tools in
`vendor\` first (or copy them from a workflow run):

```
vendor\ffmpeg\ffmpeg.exe, ffprobe.exe, LICENSE
vendor\librempeg\ffmpeg.exe, ffprobe.exe, COPYING.AGPLv3, LICENSE.md
```

```bat
cd path\to\DTS2DD
build_windows.bat
```

It builds `dist\DTS2DD\`, adds the tools and licences, runs the self-test,
and (if Inno Setup is installed) writes `dist\installer\DTS2DD-Setup-<version>.exe`.
Missing tools only produce warnings: the app then downloads FFmpeg on first
run and/or TrueHD users select LibreMPEG themselves.

### Code signing

The installer and app are not signed, so SmartScreen shows "Windows protected
your PC" (More info -> Run anyway) until the download builds reputation. Only
an Authenticode certificate (e.g. Azure Trusted Signing, or an OV/EV
certificate) removes this; sign `dist\DTS2DD\*.exe` before compiling the
installer and the installer afterwards with `signtool`.

### Manual Build Steps

If the script fails, build manually:

```bash
pip install -r requirements.txt pyinstaller pillow
python create_icon.py
python -m PyInstaller --noconfirm --clean converter.spec      # -> dist\DTS2DD\
"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" /DAppVersion=1.1.0 installer\DTS2DD.iss
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

## LibreMPEG for TrueHD

Dolby TrueHD conversion needs a **LibreMPEG** `ffmpeg` (plus `ffprobe`) with
the TrueHD 7.1 encoder. The app does not download it and never substitutes the
standard FFmpeg.

### What was verified (November 2026)

| Item | Result |
|---|---|
| Source | https://github.com/librempeg/librempeg, `master` at commit `2f18174fc205` (2026-11-14). LibreMPEG has no versioned releases; its `v0.x` tags are old FFmpeg tags. The 7.1 TrueHD layouts arrived under the changelog's "version 7.1" section ("more truehd encoder channel-layouts"). |
| Advertised layouts | mono, stereo, 2.1, 3.0, 3.1, 4.0, 4.1, 5.0(side), 5.1(side), 5.0, 5.1, 6.0, 6.1, 7.0, **7.1** (FL FR FC LFE BL BR SL SR) |
| Sample formats / rates | s16p, s32p (coded as 16 or 24-bit); 44.1-192 kHz |
| 24-bit 48 kHz 7.1 | Encodes; decodes bit-identical in LibreMPEG and in independent upstream FFmpeg 9.0.1 |
| Real DTS-HD MA (FATE samples) | 7.1 24/48, 5.1 24/48 and 5.1 16/192 are bit-identical after conversion. 6.1 is NOT bit-identical. 7.1(wide) is not accepted (FFmpeg silently downmixes it to 5.1 unless the layout is forced). |
| Channel mapping | Synthetic labelled audio: all channels in place. TrueHD header channel assignment for 7.1 = L/R, C, LFE, Ls/Rs, Lrs/Rrs. |
| Matroska | Muxes as `A_TRUEHD`; Dolby Vision P8/P7 config and packets preserved |
| Encoder flags needed | `-strict experimental` (encoder is marked experimental), forced `-ch_layout`/`-sample_fmt`/`-ar`, and `-bsf:a side_data=remove=skip_samples` (otherwise the final <1 ms of real audio is flagged as padding in MKV) |
| Plex / Fire TV / AV receiver | **Not tested** - requires the manual hardware test in USER_GUIDE.md |
| Official Windows x64 binary | **None found.** Community builds exist but were not verified, so none is recommended or downloaded by the app. |

Exact conversion command generated by the app (7.1 source at stream 1, two
audio tracks in the file):

```
librempeg\ffmpeg.exe -hide_banner -nostdin -i "Spectre (2015).mkv"
  -map 0 -map -0:t? -map 0:1 -map 0:t?
  -c copy -c:a:2 truehd -strict:a:2 experimental
  -ch_layout:a:2 7.1 -sample_fmt:a:2 s32p -ar:a:2 48000
  -bsf:a:2 side_data=remove=skip_samples
  -metadata:s:a:2 "title=Dolby TrueHD 7.1" -metadata:s:a:2 language=eng
  -disposition:a:2 0 -default_mode passthrough
  -progress pipe:1 -nostats -y ".Spectre (2015).truehd-partial-XXXX.mkv"
```

### Building LibreMPEG for Windows x64 (MSYS2)

Not yet built and tested on Windows by the developer of this change; the same
configuration was built and tested on macOS. After building, the app's
self-test decides whether the binary is usable.

1. Install MSYS2 (https://www.msys2.org/) and open the **MSYS2 MINGW64** shell.
2. Install tools:
   ```bash
   pacman -S --needed git make diffutils mingw-w64-x86_64-gcc mingw-w64-x86_64-nasm mingw-w64-x86_64-pkgconf
   ```
3. Build (pinning the verified commit):
   ```bash
   git clone https://github.com/librempeg/librempeg.git
   cd librempeg
   git checkout 2f18174fc205e364026c6fdc69ab5431c86b32ba
   ./configure --enable-agpl --disable-doc --disable-network --disable-autodetect \
               --disable-ffplay --disable-shared --enable-static \
               --extra-ldflags=-static --pkg-config-flags=--static
   make -j$(nproc)
   ```
   `--enable-agpl` is required: LibreMPEG only builds the `ffmpeg` command-line
   tool when AGPL components are enabled.
4. Check: `./ffmpeg.exe -hide_banner -h encoder=truehd` must list `7.1`, and
   `./ffmpeg.exe -hide_banner -h bsf=side_data` must exist.
5. Copy `ffmpeg.exe` and `ffprobe.exe` into a `librempeg\` folder next to
   `DTS to AC-3 Converter.exe` (or select `ffmpeg.exe` with Browse in the app).

macOS (for development): the same `./configure` line without the `--extra-ldflags`
and `--pkg-config-flags` options, with Xcode command-line tools and `nasm`.

### Bundling and licensing

- The Windows installer bundles LibreMPEG (built by the GitHub workflow from
  the pinned commit) and FFmpeg, with their licences in `licenses\` and
  `installer\THIRD_PARTY_NOTICES.txt`.
- The gyan.dev FFmpeg build is **GPL v3**; LibreMPEG built with `--enable-agpl`
  is **AGPL v3+**. For your own use nothing more is needed. If you give the
  installer to others, also make the corresponding source available (the
  notices point to the exact FFmpeg release and LibreMPEG commit).
- A LibreMPEG `ffmpeg` built with `--enable-agpl` is licensed **AGPL v3 or
  later**. If you distribute it with the app you must also provide (or offer)
  the corresponding LibreMPEG source and the license text (`COPYING.AGPLv3`),
  and state the commit you built. The app runs it as a separate program and
  does not link to it.
- Dolby and TrueHD are trademarks of Dolby Laboratories. This app produces a
  TrueHD-format bitstream with an open-source encoder; it is not a
  Dolby-certified encoder.

### Running the tests

```bash
pip install pytest                      # optional; unittest also works
python tests/fetch_samples.py           # ~7 MB of FFmpeg FATE samples
set DTS2DD_LIBREMPEG=C:\path\to\librempeg\ffmpeg.exe   (Windows)
export DTS2DD_LIBREMPEG=/path/to/librempeg/ffmpeg      (macOS)
python -m pytest tests -v
```

Unit tests need nothing else. Integration tests skip unless LibreMPEG, FFmpeg,
ffprobe and the samples are available. `DTS2DD_TEST_PACKAGING=1` also runs a
PyInstaller build.

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
- **LibreMPEG** (only if you bundle it for TrueHD): AGPL v3+ when built with
  `--enable-agpl` - see "LibreMPEG for TrueHD" above

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
