"""Stand-in for tsMuxeR used by the integration tests (NOT tsMuxeR).

It prints the same text as tsMuxeR 2.7.0 (banner, track detection blocks,
"<n>% complete" progress, "Mux successful complete") and writes the M2TS
with FFmpeg stream copy (-f mpegts -mpegts_m2ts_mode 1). That exercises the
app's discovery, meta file, progress, cancellation, cleanup and verification
code on real files. It says nothing about what the real tsMuxeR does with
Dolby Vision, HDR10+ or Atmos - FFmpeg, for example, does not write a
Dolby Vision descriptor, which the verification then reports.

Environment switches:
  FAKE_TSMUXER_FFMPEG / FAKE_TSMUXER_FFPROBE   tool paths
  FAKE_TSMUXER_VERSION                         banner version (default 2.7.0)
  FAKE_TSMUXER_ATMOS=1                         report TrueHD as "TRUE-HD + ATMOS"
  FAKE_TSMUXER_FAIL=1                          write a partial file, then fail
  FAKE_TSMUXER_SLOW=1                          keep printing progress for 60 s
  FAKE_TSMUXER_ALTER_AUDIO=1                   re-encode the first AC-3 track
  FAKE_TSMUXER_META_COPY=<path>                save the meta file it received
"""
import json
import os
import re
import subprocess
import sys
import time

FFMPEG = os.environ.get("FAKE_TSMUXER_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FAKE_TSMUXER_FFPROBE", "ffprobe")

CODECS = {
    "hevc": ("HEVC", "V_MPEGH/ISO/HEVC"), "h264": ("H.264", "V_MPEG4/ISO/AVC"),
    "truehd": ("TRUE-HD", "A_MLP"), "ac3": ("AC3", "A_AC3"), "eac3": ("E-AC3 (DD+)", "A_AC3"),
    "dts": ("DTS-HD", "A_DTS"), "aac": ("AAC", "A_AAC"), "hdmv_pgs_subtitle": ("PGS", "S_HDMV/PGS"),
    "subrip": ("SRT", "S_TEXT/UTF8"),
}


def out(text=""):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def detect(path):
    probe = json.loads(subprocess.run([FFPROBE, "-v", "error", "-of", "json", "-show_streams", "-show_format", path],
                                      capture_output=True, text=True, encoding="utf-8").stdout or "{}")
    first = True
    for s in probe.get("streams", []):
        if s.get("codec_type") == "attachment" or (s.get("disposition") or {}).get("attached_pic"):
            continue
        if not first:
            out()
        first = False
        out(f"Track ID:    {int(s['index']) + 1}")
        codec = CODECS.get(s.get("codec_name"))
        if codec is None:
            out("Can't detect stream type")
            continue
        out(f"Stream type: {codec[0]}")
        out(f"Stream ID:   {codec[1]}")
        info = ""
        if s.get("codec_name") == "truehd":
            info = "TRUE-HD" + (" + ATMOS" if os.environ.get("FAKE_TSMUXER_ATMOS") else "") + \
                   f". Peak bitrate: 0Kbps Sample Rate: 48KHz Channels: {s.get('channels')}"
        out(f"Stream info: {info}")
        out(f"Stream lang: {(s.get('tags') or {}).get('language', '')}")
    duration = float((probe.get("format") or {}).get("duration") or 0)
    out()
    h, rem = divmod(duration, 3600)
    m, sec = divmod(rem, 60)
    out(f"Duration: {int(h):02d}:{int(m):02d}:{sec:06.3f}")
    return 0


def parse_meta(meta):
    tracks = []
    with open(meta, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("MUXOPT"):
                continue
            parts = [p.strip() for p in re.findall(r'(?:"[^"]*"|[^,])+', line)]
            codec, path = parts[0], parts[1].strip('"')
            opts = {}
            for p in parts[2:]:
                k, _, v = p.partition("=")
                opts[k.strip()] = v.strip().strip('"')
            tracks.append((codec, path, opts))
    return tracks


def mux(meta, output):
    if os.environ.get("FAKE_TSMUXER_META_COPY"):
        with open(meta, encoding="utf-8") as src, open(os.environ["FAKE_TSMUXER_META_COPY"], "w",
                                                       encoding="utf-8") as dst:
            dst.write(src.read())
    tracks = parse_meta(meta)
    if os.environ.get("FAKE_TSMUXER_FAIL"):
        with open(output, "wb") as f:
            f.write(b"\x47" * 188 * 50)
        out("Error: ")
        out("Fake failure for the test suite")
        return 255
    if os.environ.get("FAKE_TSMUXER_SLOW"):
        with open(output, "wb") as f:
            f.write(b"\x47" * 188 * 50)
        for i in range(600):
            out(f"\x1b[A{i / 10:.1f}% complete")
            time.sleep(0.1)
        return 0
    if any(codec == "S_TEXT/UTF8" for codec, _, _ in tracks):
        out("Error: ")
        out("The fake tsMuxeR cannot render SRT to PGS")
        return 255
    source = tracks[0][1]
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", source]
    for i, (codec, path, opts) in enumerate(tracks):
        cmd += ["-map", f"0:{int(opts['track']) - 1}"]
    cmd += ["-c", "copy"]
    alter = os.environ.get("FAKE_TSMUXER_ALTER_AUDIO")
    audio_n = 0
    for i, (codec, path, opts) in enumerate(tracks):
        if "lang" in opts:
            cmd += [f"-metadata:s:{i}", f"language={opts['lang']}"]
        if codec.startswith("A_"):
            if alter and codec == "A_AC3" and audio_n > 0:
                cmd += [f"-c:a:{audio_n}", "ac3", f"-b:a:{audio_n}", "192k"]
                alter = None
            audio_n += 1
    cmd += ["-f", "mpegts", "-mpegts_m2ts_mode", "1", "-progress", "pipe:1", "-nostats", "-y", output]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if line.startswith("progress="):
            out("\x1b[A50.0% complete")
    proc.wait()
    if proc.returncode != 0:
        out("Error: ")
        out(f"ffmpeg failed with {proc.returncode}")
        return 255
    out("100.0% complete")
    out("Flushing write buffer")
    out("Mux successful complete")
    out("Muxing time: 0 min 1 sec")
    return 0


def main(argv):
    out(f"tsMuxeR version {os.environ.get('FAKE_TSMUXER_VERSION', '2.7.0')}. github.com/justdan96/tsMuxer")
    if len(argv) == 2:
        return detect(argv[1])
    if len(argv) == 3:
        return mux(argv[1], argv[2])
    out("tsMuxeR is a simple program to mux video to TS/M2TS files or create BD disks.")
    return 255


if __name__ == "__main__":
    sys.exit(main(sys.argv))
