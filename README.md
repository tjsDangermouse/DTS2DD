# DTS2DD - DTS to Dolby MKV Converter

A Windows and macOS desktop app that adds a Dolby audio track to MKV files
that only have DTS audio:

- **Dolby Digital (AC-3)** or **Dolby Digital Plus (E-AC-3)** from any DTS track
- **Dolby TrueHD (lossless)** from DTS-HD Master Audio 5.1 or 7.1, bit-for-bit

Video, subtitles, chapters and other tracks are copied untouched; only the
new audio track is encoded.

## Features

- **Three output formats**: AC-3 (192k-640k), E-AC-3 (192k-768k), and
  lossless Dolby TrueHD 5.1/7.1.
- **Accurate track detection**: reads every audio track with ffprobe and
  identifies DTS, DTS-HD High Resolution, DTS-HD Master Audio, DTS:X,
  Dolby TrueHD (and Atmos), Dolby Digital and Dolby Digital Plus, plus
  channel layout, sample rate, bit depth, language, title and default/forced
  flags. Unrecognised formats are shown as "Unknown" rather than guessed.
- **Safe file handling**: everything is written to a temporary file first;
  the original is only changed after a successful, checked conversion.
- **Progress and control**: percentage, speed and time remaining for TrueHD
  conversions, a Cancel button, and an activity log with the real FFmpeg
  error when something fails.
- **Self-contained Windows installer**: FFmpeg and the TrueHD encoder are
  included, so there is nothing else to install or configure.

## Installation

### Windows (recommended)

1. Download `DTS2DD-Setup-<version>.exe` from the
   [Releases](https://github.com/tjsDangermouse/DTS2DD/releases) page, or
   from the **Artifacts** of the latest successful
   [Windows installer](https://github.com/tjsDangermouse/DTS2DD/actions)
   build.
2. Run it. The installer is not code-signed, so Windows may show "Windows
   protected your PC": click **More info**, then **Run anyway**.
3. Click **Install**, then **Finish**. The app opens; afterwards start it from
   the Start menu or desktop shortcut.

No administrator rights are needed. The app installs to
`%LOCALAPPDATA%\Programs\DTS2DD` and can be removed from Settings → Apps.

### macOS / from source

Requires Python 3.11+ (with tkinter).

```bash
git clone https://github.com/tjsDangermouse/DTS2DD.git
cd DTS2DD
pip install -r requirements.txt
python3 converter.py
```

To build a macOS app bundle instead, run `./build_macos.sh` (output:
`dist/DTS to AC-3 Converter.app`).

When run this way the app uses FFmpeg from your PATH (e.g.
`brew install ffmpeg`) or downloads it on first run into your user folder
(`~/Library/Application Support/DTS2DD/ffmpeg`). TrueHD additionally needs a
LibreMPEG build you provide - see [Dolby TrueHD](#dolby-truehd-lossless).

## Usage

1. Click **Browse** and choose an MKV file. Its audio tracks are detected
   automatically (use **Detect Audio** to re-scan).
2. Choose the track to convert. The first DTS-HD MA track is pre-selected.
3. Choose the **Audio Codec**:
   - **AC-3** or **E-AC-3**: pick a bitrate. The converted track is appended
     and the original MKV is **updated in place** after you confirm.
   - **Dolby TrueHD (Lossless)**: no bitrate to choose. A new file is saved
     beside the original by default - see below.
4. Optionally tick **Remove the original selected audio track** (off by
   default).
5. Click **Convert**.

## Dolby TrueHD (Lossless)

Adds a Dolby TrueHD copy of a DTS-HD MA 5.1 or 7.1 track. This is useful when
a player (e.g. a Fire TV Stick 4K Plus) passes TrueHD to the receiver but only
the lossy core of DTS-HD MA.

- **Lossless and verified**: DTS-HD MA (lossless decode) -> integer PCM ->
  TrueHD, with no filters. Strict verification (on by default) decodes the
  original and the new track and confirms every sample is identical.
- **Channels preserved**: 7.1 stays 7.1 and 5.1 stays 5.1. Unsupported
  layouts (7.1 "wide", 6.1, 5.1 with rear surrounds) and lossy DTS formats
  are refused rather than converted approximately.
- **Not Atmos**: TrueHD 7.1 is channel-based. No height or object metadata is
  added, and the track is titled e.g. "Dolby TrueHD 7.1".
- **Safe output**: saves `Film_Sound_TrueHD.mkv` by default and never
  overwrites an existing file. Replacing the original is optional and only
  offered after every check has passed. The original DTS-HD MA track is kept
  unless you choose to remove it.
- **Video untouched**: stream copy only. Video format, HDR10 and Dolby Vision
  (profile 7/8) settings and video packet data are checked after conversion.
- **Test clip mode**: make a 30/60/120-second clip to check playback on your
  TV and receiver before converting a whole film.
- **Speaker test file**: a TrueHD 7.1 file that plays each speaker in turn,
  to confirm no channels are swapped on your system.
- **Encoder**: [LibreMPEG](https://github.com/librempeg/librempeg), because
  standard FFmpeg cannot encode TrueHD 7.1. The Windows installer includes a
  build made from a verified source commit. The app self-tests the encoder on
  every start and only allows TrueHD conversion once it passes.

Playback on Plex, Fire TV and AV receivers has **not** been verified by
automated tests. See "Testing on Fire TV / Plex / Denon" in
[USER_GUIDE.md](USER_GUIDE.md) for the manual check.

## How It Works

### Finding FFmpeg

The app looks for FFmpeg in this order:

1. The `ffmpeg` folder installed beside the app (Windows installer)
2. Your system PATH
3. A previous download in your user folder
   (`%LOCALAPPDATA%\DTS2DD\ffmpeg` on Windows,
   `~/Library/Application Support/DTS2DD/ffmpeg` on macOS)

If none is found it downloads FFmpeg into that user folder from
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/) (Windows) or
[evermeet.cx](https://evermeet.cx/ffmpeg/) (macOS). The app never writes into
its own program folder. Settings are stored in `%APPDATA%\DTS2DD` (Windows)
or `~/Library/Application Support/DTS2DD` (macOS).

### AC-3 / E-AC-3 conversion

```
ffmpeg -i input.mkv -map 0 -map -0:t? -map 0:[selected-stream] -map 0:t? -c copy \
  -c:a:[new-track] ac3 -b:a:[new-track] [bitrate] temporary.mkv -y
```

All streams are copied and the selected track is appended a second time as
AC-3 or E-AC-3. Attachments (such as subtitle fonts) are mapped last, because
FFmpeg's Matroska writer otherwise fails on files that contain them. The
temporary file replaces the original only after FFmpeg succeeds.

### TrueHD conversion

The exact LibreMPEG command, encoder findings and verification steps are
documented in [BUILD_AND_DISTRIBUTE.md](BUILD_AND_DISTRIBUTE.md#librempeg-for-truehd).

## Troubleshooting

### Windows says "Windows protected your PC"

The installer is not code-signed. Click **More info**, then **Run anyway**.

### TrueHD encoder status is not "ready"

- **TrueHD encoder missing**: no LibreMPEG was found. On Windows, reinstall
  with the installer; from source, use **Browse...** to select a LibreMPEG
  `ffmpeg`.
- **TrueHD 7.1 unsupported**: the selected executable is standard FFmpeg,
  which cannot encode TrueHD 7.1.
- **TrueHD encoder not verified**: the self-test failed; the line underneath
  says why.

### TrueHD option says the track cannot be converted

Only DTS-HD Master Audio 5.1 (side surrounds) and 7.1 tracks can be converted
losslessly. The message under the encoder status explains the specific reason.

### FFmpeg download fails (macOS / from source)

Check your internet connection, or install FFmpeg yourself:

- **macOS**: `brew install ffmpeg`
- **Windows**: download from https://www.gyan.dev/ffmpeg/builds/ and add its
  `bin` folder to PATH

### No audio streams detected

Check that the file is a valid MKV with audio by opening it in a media player.

### Permission or disk-space errors

The app writes a temporary file next to the original, so that folder must be
writable and have enough free space. A TrueHD conversion checks the space
first and stops with a message if there is not enough. Close any program that
has the file open, such as a player or a Plex library scan.

## Development

```bash
pip install pytest
python tests/fetch_samples.py      # FFmpeg FATE DTS-HD MA + Dolby Vision samples (~7 MB)
DTS2DD_LIBREMPEG=/path/to/librempeg/ffmpeg python -m pytest tests -v
```

Unit tests need nothing else. Integration tests skip unless LibreMPEG,
FFmpeg, ffprobe and the samples are available. The Windows installer is built
and tested on Windows by GitHub Actions on every push
(`.github/workflows/windows-installer.yml`). See
[BUILD_AND_DISTRIBUTE.md](BUILD_AND_DISTRIBUTE.md) for building locally and
releasing.

## File Structure

```
DTS2DD/
├── converter.py              # GUI (tkinter) and --self-test entry point
├── ffmpeg_helper.py          # FFmpeg commands, process runner, AC-3/E-AC-3 workflow
├── ffmpeg_downloader.py      # FFmpeg discovery and download
├── media_probe.py            # ffprobe JSON probing, format identification, video checks
├── truehd_encoder.py         # LibreMPEG discovery, self-test, TrueHD commands
├── truehd_workflow.py        # TrueHD conversion, verification, safe output
├── audio_stream_info.py      # Audio stream data model
├── app_paths.py              # Program and per-user folders
├── app_settings.py           # Persistent settings
├── version.py                # App version
├── converter.spec            # PyInstaller configuration
├── build_windows.bat         # Local Windows build (app folder + installer)
├── build_macos.sh            # macOS app bundle build
├── installer/                # Inno Setup script and third-party notices
├── .github/workflows/        # Windows installer build and tests
├── tests/                    # Unit and integration tests
├── icons/                    # App icons
└── requirements.txt          # Python dependencies
```

## Platform Notes

- **Windows 10/11 (64-bit)**: use the installer. It is built and tested by
  GitHub Actions on Windows Server; testing on a home PC is still to be done.
- **macOS**: run from source or build the app bundle. TrueHD needs a
  LibreMPEG build you provide.

## Performance

- Track detection takes a second or two.
- AC-3/E-AC-3 and TrueHD conversions read and write the whole MKV, so large
  films are mostly limited by disk speed.
- In testing on an Apple-silicon Mac, TrueHD encoding ran at about 44x real
  time. Strict verification then reads the new file once more; its speed is
  mostly limited by the drive.

## Future Enhancements

- Batch processing of multiple files
- Drag-and-drop file selection
- Code-signed installer (removes the SmartScreen warning)

## License

This project does not yet have a licence file, so the code is published but
all rights are reserved.

The Windows installer includes third-party programs under their own licences,
listed in its `licenses` folder:

- **FFmpeg** (gyan.dev build): GNU GPL v3
- **LibreMPEG**: GNU AGPL v3 or later

See [installer/THIRD_PARTY_NOTICES.txt](installer/THIRD_PARTY_NOTICES.txt).
Dolby, Dolby TrueHD and Dolby Atmos are trademarks of Dolby Laboratories; DTS
and DTS-HD are trademarks of DTS, Inc. This project is not affiliated with
either.
