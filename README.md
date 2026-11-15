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

## Notes: TrueHD glitch and damaged subtitles (fixed in 1.1.3)

### Summary

Spectre (2015), converted to Dolby TrueHD 7.1 and played through Plex on a
Fire TV Stick 4K Plus to a Denon AVR-X1800H, had a short audible glitch about
34 seconds in. The same file played cleanly in VLC.

The cause was a header value in the TrueHD stream. LibreMPEG's encoder
declared a maximum data rate of 9.6 Mb/s, and the loudest passage so far in
the film briefly needed slightly more. The Denon's hardware decoder enforces
that declared limit; software decoders such as VLC ignore it.

Version 1.1.3 declares 18 Mb/s (the TrueHD maximum) and checks every
converted file against the declared limit. A 1.1.3 test clip played on the
Fire TV and Denon without the glitch.

The investigation also found a second, unrelated bug: subtitles, and one video
packet, were damaged in files converted with 1.1.0 and 1.1.1. That is also
fixed in 1.1.3. **Files converted with 1.1.0 or 1.1.1 should be converted
again.**

### Symptom

| Observation | Meaning |
|---|---|
| Glitch at about 0:34-0:35 via Fire TV -> Denon | The problem shows up on the hardware path |
| No glitch when the converted TrueHD track plays in VLC | The decoded audio is perfect |
| No glitch in the original DTS-HD track (another device, and VLC) | The source is fine |
| Strict lossless verification passed | The TrueHD audio exactly matched the source |

Every sample was correct, so the problem had to be in how the stream
*describes* itself to a hardware decoder.

### How TrueHD audio is delivered

- **Access units:** TrueHD audio is split into small chunks called *access
  units*. At 48 kHz each holds 40 samples (0.83 ms), so there are 1,200 per
  second.
- **Headers:** every 16 access units the stream repeats a header. One of its
  fields is the **peak data rate**, the fastest the stream promises to need
  data.
- **Input timing:** each access unit also carries an *input timing* value,
  which tells the decoder when its data should arrive.
- **What Dolby's encoder does:** it varies the input timing, so a large access
  unit can start arriving early.
- **What LibreMPEG does:** it always writes the same input timing. Each access
  unit therefore has to arrive within its own 0.83 ms, at no more than the
  declared peak rate.
- **The result:** with a declared 9.6 Mb/s, no access unit may exceed
  **1,000 bytes** (9.6 Mb/s x 0.83 ms / 8).

A hardware decoder that trusts the header briefly runs short of data when an
access unit is larger, which is heard as a dropout or click. FFmpeg's decoder,
used by VLC and by the app's checks, ignores the header value.

### Evidence

Real Dolby streams versus LibreMPEG's (from genuine Dolby TrueHD samples in
FFmpeg's official test files):

| | Real Dolby 7.1 | Real Dolby 5.1 | LibreMPEG (before fix) |
|---|---|---|---|
| Declared peak rate | 7.66 Mb/s (set from the content) | 3.15 Mb/s (set from the content) | **Always 9.60 Mb/s** |
| Input timing between access units | Varies (39, 40, 57) | Varies (35, 40, 45) | **Always 40** |
| Extra header fields (dialogue level etc.) | Filled in | Filled in | All zero |

Spectre's first minute, encoded exactly as the app did:

| Time | Average rate | Loudest front channel | Largest access unit |
|---|---|---|---|
| 0:28-0:32 | 4.6-5.2 Mb/s | -14 to -21 dBFS | 7.8-8.2 Mb/s |
| **0:34** | **6.75 Mb/s** | **-4 dBFS** | **9.8 Mb/s** |
| 0:35-0:37 | 6.3-6.7 Mb/s | -3 to -6 dBFS | 9.5-9.6 Mb/s |

At 0:34 the soundtrack becomes louder than anything before it. Only 9 of the
72,000 access units in the first minute reached the 1,000-byte limit: seven
between **0:34.467 and 0:34.653** (largest 1,018 bytes), where the glitch was
heard, and two at about 0:44.8.

Ruled out:
- **DTS decoding:** no decoder errors or fallback to the lossy core, and the
  audio fingerprints matched.
- **Channels:** all eight carry sound from the start.
- **Stream integrity:** TrueHD's own integrity and lossless checks pass.
- **Overall data rate:** the busiest 13 ms window (7.15 Mb/s) is under
  9.6 Mb/s. Only individual access units exceed the limit, which is why the
  constant input timing matters.

### Why the app's checks didn't catch it

- Strict verification compares decoded audio, which was perfect.
- FFmpeg's TrueHD decoder ignores the declared peak rate.
- The self-test used half a second of moderate-volume test audio.
- The trigger depends on content: only loud, dense passages hit it.

### The fix

1. A LibreMPEG patch
   ([installer/patches/librempeg-truehd-peak-bitrate.patch](installer/patches/librempeg-truehd-peak-bitrate.patch))
   adds a `peak_bitrate` option. Without the option, output is byte-identical
   to before.
2. The app declares **18 Mb/s**, the TrueHD and Blu-ray maximum, allowing
   access units of up to 1,875 bytes. Spectre's largest seen is 1,032.
3. Every conversion records the size of every access unit during encoding, at
   no extra cost, and fails if any exceeds the header's limit.
4. The encoder self-test refuses any LibreMPEG without the patch.

Access unit sizes depend on where encoding starts: a clip starting at 0:20.85
put its oversized access units at different times than the full film. To
reproduce an issue, a test clip must start at the same point as the full
conversion.

Verified on a Mac with two clips of Spectre's first minute with identical
audio. The unfixed clip had the same nine over-limit access units; the fixed
clip had none. Both decode bit-identical to the source. On the Fire TV and
Denon, a 1.1.3 test clip played without the glitch.

### Second issue: damaged subtitles and video

- **Symptom:** reading Spectre, the Windows LibreMPEG build printed
  "Unsupported encoding type" 46 times, once for each subtitle track.
- **Cause:** Blu-ray PGS subtitles in MKV are usually zlib-compressed. The
  LibreMPEG build lacked the zlib library, so it copied the subtitles *still
  compressed* into a file that marks them as uncompressed (about a third of
  the expected data, and unreadable). The same build also inserted a spurious
  4,305-byte video packet at 0:25.1.
- **Why it was missed:** the subtitle check compared only names, languages and
  flags; the video check read both files with the same faulty build.
- **Fix:** LibreMPEG is built with zlib. The self-test creates a tiny
  zlib-compressed MKV and refuses any build that can't read it back. Every
  copied track (video, audio and all subtitles) is now compared packet by
  packet with the original, both read by the independent standard FFmpeg. On
  Spectre all 48 tracks match, and a copy of the old faulty build is refused.

### Release problems on the way

| Problem | Fix |
|---|---|
| Build failed setting up MSYS2's deprecated MINGW64 environment | Switched to UCRT64 |
| The patch was converted to Windows line endings on the build machine and wouldn't apply | `.gitattributes` keeps patch files byte-exact |
| MSYS2 setup fails intermittently | Retried up to three times |
| The `v1.1.2` tag pointed at a failed build | Released as 1.1.3; 1.1.2 was never published |

### Remaining risks

**Input timing is still constant.** Dolby's encoder schedules each access
unit's arrival using a buffer model: data flows in at the declared peak rate,
and big access units start arriving earlier so the buffer absorbs bursts. That
lets Dolby declare a low, realistic peak (7.66 Mb/s in the sample above) even
though single access units briefly exceed it. LibreMPEG has no buffer model.
It gives every access unit the same timing, so each must fit in its own
0.83 ms slot. Declaring 18 Mb/s makes that true for every access unit, which
means the stream never asks the decoder to buffer anything. That is the most
conservative possible schedule, and any decoder that buffers is also
satisfied. What it is *not* is verified against Dolby's specification, which
is only available to Dolby licensees together with Dolby's validation tools.
Full certification would need those tools; reproducing Dolby's buffer
scheduling in LibreMPEG would need the same specification. In practice the
remaining risk is a receiver that dislikes the 18 Mb/s declaration itself.
The Denon accepts it, and testing on more devices is the practical check.

**A film could exceed the limit at high sample rates.** The 18 Mb/s TrueHD
limit applies to Dolby's streams *with* buffering. Here each access unit must
fit 1,875 bytes on its own, and LibreMPEG compresses less efficiently than
Dolby (about 1.3x the size of the DTS-HD MA track on Spectre, and around 30%
*larger* than raw PCM on incompressible audio). Worst case, measured with
full-volume random noise on every channel:

| Source | Raw audio per access unit | TrueHD worst case | Fits 1,875 bytes? |
|---|---|---|---|
| 7.1, 48 kHz | 960 B | 1,248 B (12.0 Mb/s) | Always |
| 5.1, 48 kHz | 720 B | 934 B (9.0 Mb/s) | Always |
| 5.1, 96 kHz | 1,440 B | 1,648 B (15.8 Mb/s) | Always |
| 7.1, 96 kHz | 1,920 B | 2,192 B (21.0 Mb/s) | Can exceed |
| 5.1, 192 kHz | 2,880 B | 3,094 B (29.7 Mb/s) | Can exceed |

At 44.1/48 kHz, which covers almost all Blu-ray soundtracks including
Spectre, exceeding the limit is impossible: even pure noise stays a third
below it. For 7.1 at 96 kHz and 5.1 at 192 kHz it can happen in loud,
complex passages. The app warns before converting such a source (and
suggests a test clip of a loud scene first). If a passage doesn't fit, the
conversion stops with a clear error rather than producing a file that
glitches.

### Other known differences from Dolby streams

The dialogue-level and similar header fields are zero, and the built-in
2-channel and 6-channel versions are simple channel subsets rather than proper
downmixes. Neither has caused a problem on the Denon.

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
