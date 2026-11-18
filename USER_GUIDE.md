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

1. **Run the installer** `DTS2DD-Setup-<version>.exe`.
   - Windows may say "Windows protected your PC" because the installer is not
     code-signed. Click **More info**, then **Run anyway**.
   - No administrator password is needed.
2. Click **Install**, then **Finish** (the app opens straight away).
3. Next time, open **DTS to AC-3 Converter** from the Start menu or the
   desktop shortcut.

That is all: FFmpeg and the LibreMPEG TrueHD encoder are included, so nothing
is downloaded and nothing needs configuring. The app installs to
`%LOCALAPPDATA%\Programs\DTS2DD`; settings are kept in `%APPDATA%\DTS2DD`.
Installing a newer version over the top keeps your settings.

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

## Dolby TrueHD (Lossless) Conversion

Use this to add a **Dolby TrueHD** copy of a **DTS-HD Master Audio** track, for
players that pass TrueHD to your receiver but only send the lossy core of
DTS-HD MA (for example a Fire TV Stick 4K Plus).

TrueHD 7.1 is a lossless, channel-based format. It is **not Dolby Atmos**:
converting does not add height channels or object metadata, and the app never
labels the new track as Atmos.

### What is supported

| Source track | Result |
|---|---|
| DTS-HD MA 7.1 (side + rear surrounds), 16 or 24-bit, 44.1-96 kHz | Dolby TrueHD 7.1, bit-identical |
| DTS-HD MA 5.1 (side surrounds), 16 or 24-bit, 44.1-192 kHz | Dolby TrueHD 5.1, bit-identical |
| DTS:X (DTS-HD MA bed) | The lossless 7.1/5.1 bed is converted; DTS:X objects are not carried (warning shown) |
| DTS-HD MA 7.1 "wide" (front-centre speakers instead of side surrounds) | Refused - the encoder cannot represent it |
| DTS-HD MA 6.1 | Refused - output was not bit-exact in testing |
| DTS-HD MA 5.1 with rear (back) surrounds | Refused - signalled without surrounds for 5.1 decoders |
| DTS, DTS-ES, DTS 96/24, DTS-HD High Resolution, DTS Express | Refused - these are lossy, so TrueHD would not be lossless relative to the master |
| FLAC 2.0 / 5.1 / 7.1, up to 24-bit | Dolby TrueHD 2.0 / 5.1 / 7.1, bit-identical (FLAC 5.1's "back" surround pair becomes the TrueHD surround pair; samples unchanged) |
| FLAC mono, 6.1, 7.1 "wide", >24-bit | Refused |

Nothing is ever downmixed, upmixed, resampled, normalised, compressed or
equalised. If a track cannot be converted exactly, conversion is disabled and
the reason is shown under the encoder status.

### One-time setup: the LibreMPEG encoder

Standard FFmpeg's TrueHD encoder stops at 5.1, so TrueHD uses a separate
encoder, **LibreMPEG** (https://github.com/librempeg/librempeg). It does not
replace the FFmpeg the app uses for AC-3/E-AC-3.

**Windows installer: nothing to do.** The installer includes a LibreMPEG
build made from the verified source commit; the status should read
*TrueHD encoder ready* on first launch.

Otherwise (macOS, or running from source), LibreMPEG publishes no official
download. Build it yourself (see BUILD_AND_DISTRIBUTE.md, "LibreMPEG for
TrueHD") or obtain a build you trust, then **either**:

- put `ffmpeg` and `ffprobe` from LibreMPEG in a folder called `librempeg`
  next to the app, **or**
- choose **Dolby TrueHD (Lossless)**, click **Browse...** next to *Encoder
  status* and select LibreMPEG's `ffmpeg`. The path is remembered and takes
  priority over the bundled copy.

Every time the app starts (or you click **Re-check**) it self-tests the
encoder: it encodes labelled test audio for 5.1 and 7.1 at 16 and 24-bit,
decodes it with both LibreMPEG and the app's standard FFmpeg, checks every
channel is in the right place and every sample is identical, and reads the
speaker layout written in the TrueHD stream itself. The status shows:

- **TrueHD encoder ready** - 5.1 and 7.1 passed.
- **TrueHD encoder missing** - no executable found, or it has no TrueHD encoder.
- **TrueHD 7.1 unsupported** - e.g. a standard FFmpeg build was selected.
- **TrueHD encoder not verified** - the self-test failed; the detail line says why.

Conversion is only possible after the self-test passes for the source's layout.

### Converting

1. Select the MKV. The first DTS-HD MA track is pre-selected.
2. Choose **Dolby TrueHD (Lossless)**. Bitrate is disabled ("Lossless -
   automatic bitrate"). The line below the encoder status confirms what will
   be produced, e.g. *DTS-HD Master Audio 7.1 (8 ch, 48000 Hz, 24-bit) ->
   Dolby TrueHD 7.1, lossless*.
3. Options (defaults are the safe choice):
   - **Remove the original selected audio track** - off. The DTS-HD MA track
     is kept and TrueHD is added after it.
   - **Make converted audio the default track** - off. When on, the TrueHD
     track becomes the only default audio track.
   - **Strict lossless verification** - on. While encoding, the app takes a
     fingerprint (SHA-256) of the original audio exactly as it went into the
     encoder. Afterwards a separate copy of FFmpeg decodes the new TrueHD
     track from the saved MKV and fingerprints it the same way; they must
     match, which proves every sample of every channel is identical. This
     re-reads the new file once (the original is not read again).
   - **Output** - *Save as new MKV* (default) writes `Film_Sound_TrueHD.mkv`
     beside the original. *Replace original after verification* asks for
     confirmation only after every check has passed.
   - **Create short test conversion** - see below.
4. Click **Convert**. The progress bar shows percentage, elapsed time, speed
   and time remaining for encoding, then for verification. **Cancel** stops
   at any point without touching the original.

The result is written to a hidden temporary file (`.Film.truehd-partial-....mkv`)
in the same folder and is only renamed once every check has passed:

- the TrueHD track exists, with the source's channel count, layout and sample rate;
- the speaker assignment in the TrueHD bitstream matches the layout;
- the TrueHD track starts at the same time as the original track (sync);
- every other audio track, subtitle, attachment and chapter is still present
  with the same language, title and default/forced flags;
- video format, HDR and Dolby Vision configuration are unchanged, and the
  first 300 video packets are byte-identical to the original;
- (strict mode) every decoded sample of the TrueHD track equals the original.

If any check fails the temporary file is deleted, the original is untouched,
and the activity log lists the failed checks. An existing file is never
overwritten; a number is added instead (`Film_Sound_TrueHD (2).mkv`). If the
app or PC stops mid-conversion, the next time you select that film the app
offers to delete the leftover temporary file.

### Test clip mode (recommended first)

Tick **Create short test conversion**, choose 30, 60 or 120 seconds and a
start time (h:mm:ss). The app cuts that part of the film by stream copy (the
start moves back to the nearest video keyframe so picture and sound start
together), adds the TrueHD track and runs all of the checks above. The output
is always a new file, e.g. `Spectre_TrueHD_Test_60s.mkv`; test mode never
replaces the original.

### Testing on Fire TV / Plex / Denon (manual)

The app cannot check your playback chain. These steps must be done by you on
the real devices.

**Spectre 60-second test**

1. Select `Spectre (2015).mkv`, choose the DTS-HD MA 7.1 track and **Dolby
   TrueHD (Lossless)**.
2. Tick **Create short test conversion**, Length **60 s**, Start at e.g.
   `0:20:00` (any point with surround activity). Leave strict verification on.
3. Convert. Expect `..._TrueHD_Test_60s.mkv` next to the film and a log
   ending "Lossless: Verified bit-identical to the source audio".
4. Let Plex scan the folder (or put the test clip in a library folder).

**Speaker identification file** - click **Speaker test file...** to create a
53-second MKV where each speaker plays in turn: 1 pip Front Left, 2 Centre,
3 Front Right, 4 Surround Right, 5 Surround Back Right, 6 Surround Back Left,
7 Surround Left, 8 Subwoofer (LFE); the sequence plays twice and subtitles
name each speaker. This is the definitive check that no surround channels are
swapped on your system.

**On the Fire TV**
- Settings > Display & Sounds > Audio > Surround Sound: **Best Available**.
- In Plex, set audio passthrough to HDMI / allow TrueHD passthrough (the
  exact option names depend on the Plex app version).
- Play the clip and select the **Dolby TrueHD 7.1** audio track in Plex
  (it is not the default unless you ticked that option).
- On the Plex server dashboard the stream should show **Direct Play** (or
  Direct Stream) for audio. If audio shows *Transcode*, the receiver is not
  getting TrueHD.

**On the Denon AVR-X1800H**
- Press **INFO** on the remote (or check the front display) while the clip
  plays. The input signal should read **Dolby TrueHD** and the channel
  display should show 7.1 input (front L/R, centre, LFE, surround L/R and
  surround back L/R).
- Use a sound mode that does not upmix (e.g. Dolby TrueHD / Direct), not a
  Neural:X or Dolby Surround upmixer, so you see the native signal.
- With the speaker test file, confirm each pip comes from the named speaker.

Until you have done these steps, compatibility with the Fire TV, Plex and the
Denon is **unverified**.

### Limitations

- LibreMPEG's TrueHD encoder is marked experimental by its developers. The
  app protects against its known issues (see below) and verifies every
  output, but real-device testing is still required.
- The TrueHD stream's built-in 2-channel and 6-channel "presentations" (used
  only by decoders that cannot decode 7.1) are simple channel subsets, not
  proper downmixes: a 5.1-only decoder would not hear the rear surrounds.
  7.1-capable receivers such as the AVR-X1800H decode the full 8-channel
  presentation.
- Known encoder issue handled by the app (fixed in 1.1.3): LibreMPEG declared
  a 9.6 Mb/s peak data rate that loud passages exceed, so AV receivers could
  glitch briefly (Spectre at 0:34). The included LibreMPEG is patched to
  declare 18 Mb/s, and every conversion checks that the stream stays within
  it. Files converted with 1.1.0 / 1.1.1 should be converted again - their
  copied subtitles may also be corrupted (the earlier LibreMPEG build lacked
  zlib); 1.1.3 compares every copied track with the original.
- Known encoder issue handled by the app: LibreMPEG marks the last few
  samples (under 1 ms) of the stream as padding, which would make players drop
  real audio at the very end. The app removes that marker and verifies every
  sample is kept.
- Strict verification reads the whole new MKV once more, so on a slow drive
  or network share it can still take a while - roughly the time it takes to
  read the file, and less than the encode itself.
- Size: in testing, LibreMPEG's TrueHD was about 1.3x the size of the
  DTS-HD MA track it came from. For Spectre (DTS-HD MA ~4.7 Mb/s, 2 h 28 min)
  expect the new file to be roughly 7 GB larger if the original track is kept
  (an estimate, not measured on that film).
- Speed: on an Apple-silicon Mac, encoding ran at about 44x real time; a full
  film is usually limited by disk speed (the whole MKV is read and written).


## Library Scanner

The **Library Scanner** tab checks a whole folder of films. Scanning only
reads files; nothing changes until you start a repair yourself.

1. Choose the folder (sub-folders are included) and press **Scan**. Progress,
   the current file, the number with issues and the estimated time left are
   shown. **Cancel** stops at any time. A second scan only reads new or
   changed films; tick **Rescan all** to re-read everything.
2. Use **Show** to filter (issues only, subtitle warnings, FLAC, Atmos
   warnings, Dolby Vision, passed). Click a column heading to sort.
3. Click a film to see what it contains, each finding with its evidence, and
   the original metadata. "Device-specific potential problems" are about your
   playback equipment, not faults in the file.
4. Select films (Ctrl/Cmd-click, Shift-click or **Select all shown**) and
   choose **Clean Subtitles...**, **Convert FLAC to TrueHD...** or **Repair
   Selected...**. The dialog lists, per film, every track that will be kept
   or removed. Change the options, check the plan, then **Start Repairs**.
5. When the batch finishes a report lists what was repaired, skipped and why,
   and what failed. Repaired films are re-scanned automatically.

Good to know:

- Subtitle tracks without a language tag are never removed unless you tick
  that option *and* confirm the list shown.
- E-AC-3 "DD+ Atmos" that your Fire TV plays as PCM 5.1 is reported as
  **Valid Atmos - potential Fire TV playback compatibility issue**. The file
  is fine; check the Fire TV's audio output setting and Plex passthrough.
  The app never re-encodes Atmos tracks or renames tracks.
- A title saying "Atmos" on a track that does not signal Atmos is reported as
  misleading metadata; the app does not change titles.
- **Rules & Devices...** turns device profiles and individual checks on or
  off and changes the subtitle threshold and languages to keep.
- MediaInfo CLI and MKVToolNix are used automatically when installed and add
  extra evidence; they are optional.

Device behaviour (Fire TV, UB450, LG C5, Denon) cannot be tested
automatically. After a repair, play the film and check the receiver's
display: TrueHD / Dolby Atmos / PCM and the channel count, and how quickly the
UB450 starts playback.

## Queue: many changes on many films

1. In **Convert File**, choose a film, track and format as usual, then press
   **Add to Queue** instead of Convert (or tick tracks and press **Queue
   Removal**). Pick the next film and repeat.
2. In **Library Scanner**, select films, open a repair, check the plan and
   press **Add to Queue**.
3. Open the **Queue** tab. Use **Move Up / Move Down** to change the order,
   **Remove** to drop a job, then press **Start Queue** and confirm.
4. Jobs run one by one. **Stop After Current** pauses cleanly; **Cancel
   Current** stops the running job (its film is not changed). Failed or
   skipped jobs can be selected and **Retry**-ed.

You can queue several changes for the same film (for example: remove a
commentary track, then convert DTS to E-AC-3, then clean subtitles). Each job
looks at the film again just before it runs. If an earlier job made a track
impossible to identify, the later job is skipped and the report says why.
The queue is kept when you close the app.

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
1. Open Settings → Apps → Installed apps
2. Find "DTS to AC-3 Converter"
3. Click Uninstall (this removes the app, its shortcuts and the bundled tools;
   your settings in `%APPDATA%\DTS2DD` are kept)

## Support

For issues or questions, refer to:
- **README.md** - Technical details
- **QUICKSTART.md** - Quick reference
- **BUILD_AND_DISTRIBUTE.md** - For developers

Enjoy converting your DTS audio to AC-3!
