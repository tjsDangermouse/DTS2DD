"""Shared helpers for the test suite.

Tool locations (integration tests are skipped when unavailable):
  DTS2DD_LIBREMPEG  LibreMPEG ffmpeg executable with the TrueHD 7.1 encoder
  DTS2DD_FFMPEG     standard FFmpeg (defaults to ffmpeg on PATH)
  DTS2DD_SAMPLES    folder with FATE samples (defaults to tests/samples;
                    populate with: python tests/fetch_samples.py)
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_probe import find_ffprobe  # noqa: E402
from truehd_encoder import bundled_candidates  # noqa: E402

SAMPLES = Path(os.environ.get("DTS2DD_SAMPLES") or ROOT / "tests" / "samples")


def librempeg_path():
    path = os.environ.get("DTS2DD_LIBREMPEG")
    if path and os.path.isfile(path):
        return path
    bundled = bundled_candidates()
    return str(bundled[0]) if bundled else None


def ffmpeg_path():
    path = os.environ.get("DTS2DD_FFMPEG") or shutil.which("ffmpeg")
    return path if path and os.path.isfile(path) else None


def ffprobe_path():
    return find_ffprobe(ffmpeg_path(), librempeg_path())


def sample(name):
    path = SAMPLES / name
    return path if path.is_file() else None


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run(cmd):
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))}\n{result.stderr[-2000:]}")
    return result


def repeat_dts(ffmpeg, source, output, times):
    """Make a longer DTS-HD MA elementary stream by repeating its frames."""
    one = Path(output).with_suffix(".one.dts")
    run([ffmpeg, "-v", "error", "-i", source, "-c", "copy", "-f", "dts", "-y", one])
    data = one.read_bytes()
    Path(output).write_bytes(data * times)
    one.unlink()


def build_fixture(ffmpeg, workdir, dts_sample, name="Movie.mkv", seconds=12.0, video=True):
    """MKV like a Blu-ray remux: HEVC Dolby Vision P8 video, DTS-HD MA,
    a second (French AC-3) audio track, forced English subtitles,
    chapters and a font attachment."""
    workdir = Path(workdir)
    dts = workdir / "audio.dts"
    repeat_dts(ffmpeg, dts_sample, dts, 200)
    srt = workdir / "subs.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nHello\n\n"
                   "2\n00:00:05,000 --> 00:00:06,000\nWorld\n", encoding="utf-8")
    chapters = workdir / "chapters.txt"
    chapters.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=4000\ntitle=Opening\n"
                        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=4000\nEND=12000\ntitle=Main\n",
                        encoding="utf-8")
    font = workdir / "font.ttf"
    font.write_bytes(b"not really a font")
    output = workdir / name

    cmd = [ffmpeg, "-v", "error"]
    if video:
        cmd += ["-stream_loop", "4", "-i", SAMPLES / "dv84.mov"]
    cmd += ["-i", dts, "-i", srt, "-f", "ffmetadata", "-i", chapters]
    offset = 1 if video else 0
    if video:
        cmd += ["-map", "0:v:0"]
    cmd += [
        "-map", f"{offset}:a", "-map", f"{offset}:a", "-map", f"{offset + 1}",
        "-map_chapters", str(offset + 2),
        "-c", "copy", "-c:a:1", "ac3", "-b:a:1", "192k", "-ac:a:1", "2",
        "-t", str(seconds),
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=DTS-HD MA",
        "-metadata:s:a:1", "language=fre", "-metadata:s:a:1", "title=French",
        "-metadata:s:s:0", "language=eng",
        "-disposition:a:0", "default", "-disposition:a:1", "0", "-disposition:s:0", "forced",
        "-attach", font, "-metadata:s:t", "mimetype=font/ttf",
        "-y", output,
    ]
    run(cmd)
    return output
