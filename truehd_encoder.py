"""Dolby TrueHD encoding backend (LibreMPEG).

Standard FFmpeg's TrueHD encoder is experimental and only accepts layouts up
to 5.1(side). LibreMPEG (https://github.com/librempeg/librempeg) extends the
same encoder with more layouts including 7.1. This module never assumes an
executable works: every candidate is

1. asked which channel layouts / sample formats its TrueHD encoder
   advertises, and then
2. functionally self-tested: synthetic, per-channel-labelled PCM is encoded
   to TrueHD in Matroska, decoded again (by the encoder's own build *and* by
   the independent standard FFmpeg build when available), and compared
   bit-for-bit channel-by-channel. The TrueHD major-sync channel assignment
   bits are also parsed directly from the bitstream, independently of FFmpeg.

Only layouts that pass the self-test can be converted.
"""
import array
import hashlib
import math
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from media_probe import subprocess_flags

# --------------------------------------------------------------------------
# Supported formats
# --------------------------------------------------------------------------

# FFmpeg channel order for each supported layout.
LAYOUT_CHANNELS: Dict[str, List[str]] = {
    "5.1(side)": ["FL", "FR", "FC", "LFE", "SL", "SR"],
    "7.1": ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
}

# Layouts this app will convert. Everything else is rejected even if the
# encoder accepts it, because it has not been verified:
#   * 6.1            -> LibreMPEG encodes it, but the result is NOT bit-exact.
#   * 5.1 (BL/BR)    -> the 6-channel presentation is signalled without
#                       surround channels.
#   * 7.1(wide)      -> not supported by the encoder; FFmpeg would silently
#                       downmix it to 5.1 if the layout were not forced.
SUPPORTED_LAYOUTS = ("5.1(side)", "7.1")

LAYOUT_LABELS = {"5.1(side)": "5.1", "7.1": "7.1"}

CHANNEL_NAMES = {
    "FL": "Front Left", "FR": "Front Right", "FC": "Centre",
    "LFE": "LFE (subwoofer)", "SL": "Surround Left (side)",
    "SR": "Surround Right (side)", "BL": "Surround Back Left (rear)",
    "BR": "Surround Back Right (rear)",
}

# Dolby TrueHD allows 8 channels up to 96 kHz and 6 channels up to 192 kHz.
SAMPLE_RATES_BY_CHANNELS = {
    8: (44100, 48000, 88200, 96000),
    6: (44100, 48000, 88200, 96000, 176400, 192000),
}

# Decoder output formats that represent integer (lossless) PCM. The DTS
# decoder outputs float (fltp) when it only decodes the lossy core, and
# integer s16p/s32p when it decodes the lossless XLL extension.
LOSSLESS_SAMPLE_FORMATS = {"s16p": 16, "s32p": 24}

# TrueHD major-sync channel assignment bits (Dolby's definitions).
TRUEHD_CHANNEL_BITS = [
    "L/R", "C", "LFE", "Ls/Rs", "Lvh/Rvh", "Lc/Rc", "Lrs/Rrs",
    "Cs", "Ts", "Lsd/Rsd", "Lw/Rw", "Cvh", "LFE2",
]

# Expected (6-channel presentation, 8-channel presentation) assignments.
EXPECTED_CHANNEL_ASSIGNMENT = {
    "5.1(side)": (0x0F, 0x0F),   # L/R C LFE Ls/Rs
    "7.1": (0x0F, 0x4F),         # 8ch: L/R C LFE Ls/Rs Lrs/Rrs
}

# LibreMPEG's TrueHD encoder codes a short final access unit correctly but
# also tags that packet with "discard padding" for the missing samples, so
# Matroska players would drop up to 39 real samples at the very end. Removing
# that packet side data keeps every sample (verified by the self-test and the
# strict comparison).
END_PADDING_FIX = "side_data=remove=skip_samples"

# Peak data rate written into every TrueHD header. LibreMPEG gives every
# access unit the same input timing, so a hardware decoder expects each one to
# arrive within its own 1/1200 s at no more than this rate. With LibreMPEG's
# default of 9.6 Mb/s, loud passages (e.g. Spectre at 0:34.5) produce access
# units just over 1000 bytes and real Dolby decoders glitch. 18 Mb/s is the
# TrueHD/Blu-ray maximum and allows 1875-byte access units. Requires the
# peak_bitrate option added by installer/patches/librempeg-truehd-peak-bitrate.patch.
TRUEHD_PEAK_BITRATE = 18_000_000


def max_access_unit_bytes(peak_bitrate: int, sample_rate: int) -> float:
    """Largest access unit that fits the declared peak rate.

    An access unit is 40 samples at 44.1/48 kHz (80 at 88.2/96, 160 at
    176.4/192), i.e. always 40/44100 or 40/48000 seconds.
    """
    base = 44100 if sample_rate % 44100 == 0 else 48000
    return peak_bitrate * 40 / base / 8


# On incompressible audio (full-scale noise) LibreMPEG's access units were
# measured at up to ~1.3x the size of the raw PCM they carry: 1248 B for
# 7.1/48k (960 B raw), 1648 B for 5.1/96k, 2192 B for 7.1/96k, 3094 B for
# 5.1/192k.
WORST_CASE_EXPANSION = 1.3


def may_exceed_peak_rate(channels: int, sample_rate: int, bit_depth: int,
                         peak_bitrate: int = TRUEHD_PEAK_BITRATE) -> bool:
    """True when a loud passage could produce an access unit larger than the
    declared peak rate allows (so the conversion might fail at the end)."""
    base = 44100 if sample_rate % 44100 == 0 else 48000
    samples_per_au = 40 * sample_rate // base
    raw_bytes = channels * samples_per_au * max(bit_depth, 16) / 8
    return raw_bytes * WORST_CASE_EXPANSION > max_access_unit_bytes(peak_bitrate, sample_rate)


STATUS_READY = "TrueHD encoder ready"
STATUS_MISSING = "TrueHD encoder missing"
STATUS_NO_71 = "TrueHD 7.1 unsupported"
STATUS_NOT_VERIFIED = "TrueHD encoder not verified"


class TrueHDError(RuntimeError):
    """A TrueHD-specific failure with a user-facing explanation."""


# --------------------------------------------------------------------------
# Executable discovery
# --------------------------------------------------------------------------

def _exe(name: str) -> str:
    return f"{name}.exe" if platform.system() == "Windows" else name


def app_base_dirs() -> List[Path]:
    dirs = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        dirs.append(exe_dir)
        # macOS .app bundle: also look beside the bundle
        if exe_dir.name == "MacOS":
            dirs.append(exe_dir.parent.parent.parent)
    dirs.append(Path(__file__).resolve().parent)
    dirs.append(Path.cwd())
    unique = []
    for d in dirs:
        if d not in unique:
            unique.append(d)
    return unique


def bundled_candidates() -> List[Path]:
    """Application-local LibreMPEG locations (``librempeg/`` folder)."""
    found = []
    for base in app_base_dirs():
        folder = base / "librempeg"
        for rel in (_exe("ffmpeg"), _exe("librempeg"),
                    Path("bin") / _exe("ffmpeg"), Path("bin") / _exe("librempeg")):
            path = folder / rel
            if path.is_file() and path not in found:
                found.append(path)
    return found


def discover_encoder(configured: Optional[str],
                     exclude: Iterable[Optional[str]] = ()) -> Tuple[Optional[str], str]:
    """Return (path, origin) for the TrueHD encoder to check.

    Order: configured path -> bundled ``librempeg/`` folder -> ``librempeg``
    on PATH -> an ``ffmpeg`` on PATH that advertises TrueHD 7.1. The app's
    standard FFmpeg is never chosen automatically unless it advertises 7.1.
    """
    if configured and os.path.isfile(configured):
        return configured, "configured"
    for path in bundled_candidates():
        return str(path), "bundled"
    on_path = shutil.which("librempeg")
    if on_path:
        return on_path, "PATH"
    excluded = {os.path.abspath(p) for p in exclude if p}
    ffmpeg_on_path = shutil.which("ffmpeg")
    if ffmpeg_on_path and os.path.abspath(ffmpeg_on_path) not in excluded:
        caps = query_capabilities(ffmpeg_on_path)
        if caps.has_encoder and "7.1" in caps.layouts:
            return ffmpeg_on_path, "PATH"
    return None, "none"


# --------------------------------------------------------------------------
# Advertised capabilities
# --------------------------------------------------------------------------

@dataclass
class AdvertisedCapabilities:
    has_encoder: bool = False
    layouts: List[str] = field(default_factory=list)
    sample_formats: List[str] = field(default_factory=list)
    sample_rates: List[int] = field(default_factory=list)
    experimental: bool = False
    has_peak_option: bool = False   # patched LibreMPEG: -peak_bitrate
    has_zlib: bool = False          # needed to copy zlib-compressed MKV tracks (PGS subtitles)
    version: str = ""
    error: str = ""

    def missing_features(self) -> List[str]:
        missing = []
        if not self.has_peak_option:
            missing.append("it lacks the TrueHD peak data rate fix (peak_bitrate option), so loud "
                           "passages can glitch on AV receivers")
        if not self.has_zlib:
            missing.append("it was built without zlib, so compressed subtitle tracks would be "
                           "corrupted when copied")
        return missing


def parse_encoder_help(text: str) -> AdvertisedCapabilities:
    """Parse the output of ``ffmpeg -h encoder=truehd``."""
    caps = AdvertisedCapabilities()
    if not re.search(r"^Encoder truehd", text, re.MULTILINE):
        return caps
    caps.has_encoder = True
    caps.has_peak_option = bool(re.search(r"^\s*-peak_bitrate\b", text, re.MULTILINE))
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("General capabilities:"):
            caps.experimental = " exp" in f" {line.split(':', 1)[1]} "
        elif line.startswith("Supported channel layouts:"):
            caps.layouts = line.split(":", 1)[1].split()
        elif line.startswith("Supported sample formats:"):
            caps.sample_formats = line.split(":", 1)[1].split()
        elif line.startswith("Supported sample rates:"):
            caps.sample_rates = [int(v) for v in line.split(":", 1)[1].split() if v.isdigit()]
    return caps


def _run_text(cmd: Sequence[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=subprocess_flags(),
    )


def query_capabilities(path: str) -> AdvertisedCapabilities:
    try:
        version = _run_text([path, "-hide_banner", "-version"]).stdout.splitlines()
        result = _run_text([path, "-hide_banner", "-h", "encoder=truehd"])
    except (OSError, subprocess.TimeoutExpired) as e:
        caps = AdvertisedCapabilities()
        caps.error = f"Could not run {path}: {e}"
        return caps
    caps = parse_encoder_help(result.stdout + result.stderr)
    caps.has_zlib = reads_zlib_compressed_tracks(path)
    caps.version = version[0] if version else ""
    return caps


# --------------------------------------------------------------------------
# zlib check: can the build read zlib-compressed Matroska tracks?
# --------------------------------------------------------------------------
# Checking for a "zlib" decoder is not enough: LibreMPEG has built-in inflate
# for its own codecs even when built without the zlib library, while its
# Matroska demuxer still cannot unpack compressed tracks. So test it directly.

def _ebml(element_id: bytes, payload: bytes) -> bytes:
    return element_id + b"\x01" + len(payload).to_bytes(7, "big") + payload


def _ebml_uint(element_id: bytes, value: int) -> bytes:
    return _ebml(element_id, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def zlib_test_mkv(text: bytes) -> bytes:
    """A minimal MKV with one zlib-compressed UTF-8 subtitle block."""
    header = _ebml(b"\x1a\x45\xdf\xa3", _ebml_uint(b"\x42\x86", 1) + _ebml_uint(b"\x42\xf7", 1)
                   + _ebml_uint(b"\x42\xf2", 4) + _ebml_uint(b"\x42\xf3", 8)
                   + _ebml(b"\x42\x82", b"matroska") + _ebml_uint(b"\x42\x87", 4)
                   + _ebml_uint(b"\x42\x85", 2))
    info = _ebml(b"\x15\x49\xa9\x66", _ebml_uint(b"\x2a\xd7\xb1", 1000000)
                 + _ebml(b"\x4d\x80", b"DTS2DD") + _ebml(b"\x57\x41", b"DTS2DD"))
    compression = _ebml(b"\x50\x34", _ebml_uint(b"\x42\x54", 0))          # ContentCompAlgo: zlib
    encoding = _ebml(b"\x62\x40", _ebml_uint(b"\x50\x31", 0) + _ebml_uint(b"\x50\x32", 1)
                     + _ebml_uint(b"\x50\x33", 0) + compression)
    track = _ebml(b"\xae", _ebml_uint(b"\xd7", 1) + _ebml_uint(b"\x73\xc5", 1) + _ebml_uint(b"\x83", 0x11)
                  + _ebml(b"\x86", b"S_TEXT/UTF8") + _ebml(b"\x6d\x80", encoding))
    block = b"\x81\x00\x00\x00" + zlib.compress(text)
    cluster = _ebml(b"\x1f\x43\xb6\x75", _ebml_uint(b"\xe7", 0)
                    + _ebml(b"\xa0", _ebml(b"\xa1", block) + _ebml_uint(b"\x9b", 1000)))
    return header + _ebml(b"\x18\x53\x80\x67", info + _ebml(b"\x16\x54\xae\x6b", track) + cluster)


def reads_zlib_compressed_tracks(ffmpeg_path: str) -> bool:
    """True when the build returns the decompressed payload of a zlib track."""
    text = b"DTS2DD zlib check: compressed Matroska tracks must be readable. " * 4
    with tempfile.TemporaryDirectory(prefix="dts2dd-zlib-") as workdir:
        path = os.path.join(workdir, "zlib-test.mkv")
        with open(path, "wb") as f:
            f.write(zlib_test_mkv(text))
        try:
            result = _run_text([ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
                                "-map", "0", "-c", "copy", "-f", "framemd5", "-"])
        except (OSError, subprocess.TimeoutExpired):
            return False
    if result.returncode != 0:
        return False
    return hashlib.md5(text).hexdigest() in result.stdout


# --------------------------------------------------------------------------
# TrueHD bitstream inspection (independent of FFmpeg's decoder)
# --------------------------------------------------------------------------

TRUEHD_SYNC = b"\xf8\x72\x6f\xba"


@dataclass
class MajorSync:
    sample_rate: int
    ch6_assignment: int
    ch8_assignment: int
    peak_bitrate: int = 0      # bits/s declared in the header

    @staticmethod
    def describe(assignment: int) -> str:
        return " ".join(name for bit, name in enumerate(TRUEHD_CHANNEL_BITS)
                        if assignment & (1 << bit)) or "none"


def parse_major_sync(data: bytes) -> Optional[MajorSync]:
    """Parse the first TrueHD major sync header in ``data``.

    Layout after the 32-bit sync word 0xF8726FBA: 4 bits sample-rate code,
    4 bits reserved, 2+2 bits channel modifiers, 5 bits 6-channel
    assignment, 2 bits modifier, 13 bits 8-channel assignment.
    """
    pos = data.find(TRUEHD_SYNC)
    if pos < 0 or len(data) < pos + 16:
        return None
    bits = int.from_bytes(data[pos + 4:pos + 8], "big")
    rate_code = bits >> 28
    ch6 = (bits >> 15) & 0x1F
    ch8 = bits & 0x1FFF
    if rate_code == 0xF:
        return None
    sample_rate = (44100 if rate_code & 8 else 48000) << (rate_code & 7)
    # After signature (16), flags (16), reserved (16): is_vbr (1) + peak_data_rate (15)
    peak_field = int.from_bytes(data[pos + 14:pos + 16], "big") & 0x7FFF
    peak_bitrate = (peak_field * sample_rate + 8) >> 4
    return MajorSync(sample_rate, ch6, ch8, peak_bitrate)


def read_major_sync(ffmpeg_path: str, file_path: str, audio_index: int) -> Optional[MajorSync]:
    """Extract the first TrueHD packets of a stream and parse the header."""
    cmd = [ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-i", file_path,
           "-map", f"0:a:{audio_index}", "-c", "copy", "-frames:a", "4",
           "-f", "truehd", "-"]
    result = subprocess.run(cmd, capture_output=True, timeout=120,
                            creationflags=subprocess_flags())
    if result.returncode != 0:
        return None
    return parse_major_sync(result.stdout)


def check_channel_assignment(layout: str, sync: Optional[MajorSync]) -> Optional[str]:
    """Return an error message, or None when the header matches the layout."""
    if sync is None:
        return "No TrueHD major sync header found in the output"
    expected6, expected8 = EXPECTED_CHANNEL_ASSIGNMENT[layout]
    if (sync.ch6_assignment, sync.ch8_assignment) != (expected6, expected8):
        return (f"TrueHD header signals 6ch [{MajorSync.describe(sync.ch6_assignment)}] / "
                f"8ch [{MajorSync.describe(sync.ch8_assignment)}], expected "
                f"[{MajorSync.describe(expected6)}] / [{MajorSync.describe(expected8)}]")
    return None


# --------------------------------------------------------------------------
# Synthetic labelled PCM and channel comparison
# --------------------------------------------------------------------------

def synth_labelled_pcm(channels: int, frames: int, bits: int, seed: int = 2015) -> array.array:
    """Interleaved test PCM where every channel is unique.

    Channel k is a sine at (k+1)*250 Hz plus channel-specific pseudo-random
    noise, so an exact match identifies the channel unambiguously and the
    noise exercises every bit of the sample word. Values are returned as
    32-bit integers in FFmpeg's convention (24-bit audio is left-justified).
    """
    rng = random.Random(seed)
    full_scale = 1 << (bits - 1)
    shift = 32 - bits
    pcm = array.array("i", bytes(4 * channels * frames))
    for k in range(channels):
        freq = 250.0 * (k + 1)
        step = 2 * math.pi * freq / 48000.0
        amp = 0.35 * full_scale
        noise = max(1, full_scale >> 9)
        for i in range(frames):
            v = int(amp * math.sin(step * i)) + rng.randint(-noise, noise)
            v = max(-full_scale, min(full_scale - 1, v))
            pcm[i * channels + k] = v << shift
    return pcm


def _to_little_endian_bytes(pcm: array.array) -> bytes:
    if sys.byteorder != "little":
        pcm = array.array(pcm.typecode, pcm)
        pcm.byteswap()
    return pcm.tobytes()


def _from_little_endian_bytes(data: bytes) -> array.array:
    pcm = array.array("i")
    pcm.frombytes(data[: len(data) - len(data) % 4])
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm


def identify_channels(source: array.array, decoded: array.array,
                      channels: int) -> List[List[int]]:
    """For each decoded channel, list the source channels it is identical to."""
    result = []
    src = [source[k::channels] for k in range(channels)]
    for k in range(channels):
        out = decoded[k::channels]
        result.append([j for j in range(channels) if src[j] == out])
    return result


def describe_mapping_problem(layout: str, mapping: List[List[int]]) -> Optional[str]:
    """Explain a wrong channel mapping, or return None when it is correct."""
    names = LAYOUT_CHANNELS[layout]
    problems = []
    for k, matches in enumerate(mapping):
        if matches == [k]:
            continue
        if not matches:
            problems.append(f"{names[k]} output does not match any source channel")
        else:
            problems.append(f"{names[k]} output contains {', '.join(names[j] for j in matches)}")
    return "; ".join(problems) or None


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

@dataclass
class LayoutTest:
    layout: str
    bits: int
    passed: bool
    detail: str


@dataclass
class EncoderReport:
    path: Optional[str]
    origin: str = ""
    status: str = STATUS_MISSING
    message: str = ""
    capabilities: Optional[AdvertisedCapabilities] = None
    tests: List[LayoutTest] = field(default_factory=list)

    @property
    def verified_layouts(self) -> List[str]:
        """Layouts where every bit-depth test passed."""
        layouts = []
        for layout in SUPPORTED_LAYOUTS:
            results = [t for t in self.tests if t.layout == layout]
            if results and all(t.passed for t in results):
                layouts.append(layout)
        return layouts

    def can_encode(self, layout: Optional[str]) -> bool:
        return layout in self.verified_layouts

    @property
    def is_usable(self) -> bool:
        return bool(self.verified_layouts)


_DECODED_AUDIO_RE = re.compile(r"Audio: truehd[^,]*, (\d+) Hz, ([^,]+), ([a-z0-9]+)")


def decoded_stream_layout(ffmpeg_path: str, file_path: str) -> Optional[Tuple[int, str]]:
    """(sample_rate, layout) as reported by a decoder for the first TrueHD stream."""
    result = _run_text([ffmpeg_path, "-hide_banner", "-nostdin", "-i", file_path])
    match = _DECODED_AUDIO_RE.search(result.stderr)
    if not match:
        return None
    return int(match.group(1)), match.group(2).strip()


def decode_to_s32(ffmpeg_path: str, file_path: str, audio_index: int = 0,
                  timeout: int = 120) -> bytes:
    cmd = [ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-i", file_path,
           "-map", f"0:a:{audio_index}", "-c:a", "pcm_s32le", "-f", "s32le", "-"]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout,
                            creationflags=subprocess_flags())
    if result.returncode != 0:
        raise TrueHDError(result.stderr.decode("utf-8", "replace").strip()[-400:]
                          or "decode failed")
    return result.stdout


def encode_test_signal(encoder: str, raw_path: str, layout: str, bits: int,
                       out_path: str) -> subprocess.CompletedProcess:
    raw_format = "s16le" if bits == 16 else "s32le"
    sample_fmt = "s16p" if bits == 16 else "s32p"
    cmd = [
        encoder, "-hide_banner", "-nostdin", "-v", "error",
        "-f", raw_format, "-ar", "48000", "-ch_layout", layout, "-i", raw_path,
        "-map", "0:a", "-c:a", "truehd", "-strict:a:0", "experimental",
        "-ch_layout:a:0", layout, "-sample_fmt:a:0", sample_fmt, "-ar:a:0", "48000",
        "-peak_bitrate:a:0", str(TRUEHD_PEAK_BITRATE),
        "-bsf:a:0", END_PADDING_FIX,
        "-y", out_path,
    ]
    return _run_text(cmd, timeout=120)


def run_layout_test(encoder: str, layout: str, bits: int, workdir: str,
                    reference_decoder: Optional[str] = None,
                    seconds: float = 0.5) -> LayoutTest:
    """Encode labelled PCM, decode it and verify every channel bit-for-bit."""
    channels = len(LAYOUT_CHANNELS[layout])
    # Not a multiple of the 40-sample access unit, so a mishandled final
    # (short) access unit is detected.
    frames = int(48000 * seconds) + 24
    pcm = synth_labelled_pcm(channels, frames, bits)
    tag = f"{layout.replace('(', '_').replace(')', '')}_{bits}"
    raw_path = os.path.join(workdir, f"src_{tag}.raw")
    out_path = os.path.join(workdir, f"enc_{tag}.mkv")

    with open(raw_path, "wb") as f:
        if bits == 16:
            pcm16 = array.array("h", (v >> 16 for v in pcm))
            if sys.byteorder != "little":
                pcm16.byteswap()
            f.write(pcm16.tobytes())
        else:
            f.write(_to_little_endian_bytes(pcm))

    try:
        result = encode_test_signal(encoder, raw_path, layout, bits, out_path)
    except (OSError, subprocess.TimeoutExpired) as e:
        return LayoutTest(layout, bits, False, f"encoder could not run: {e}")
    if result.returncode != 0 or not os.path.exists(out_path):
        lines = [l for l in result.stderr.splitlines() if l.strip()]
        return LayoutTest(layout, bits, False,
                          "encoder rejected it: " + (lines[0] if lines else f"exit {result.returncode}"))

    decoders = [encoder]
    if reference_decoder and os.path.abspath(reference_decoder) != os.path.abspath(encoder):
        decoders.append(reference_decoder)

    for decoder in decoders:
        label = "independent FFmpeg" if decoder != encoder else "encoder build"
        info = decoded_stream_layout(decoder, out_path)
        if info is None:
            return LayoutTest(layout, bits, False, f"{label} could not read the TrueHD stream")
        if info != (48000, layout):
            return LayoutTest(layout, bits, False,
                              f"{label} decoded {info[1]} @ {info[0]} Hz, expected {layout} @ 48000 Hz")
        try:
            decoded = _from_little_endian_bytes(decode_to_s32(decoder, out_path))
        except (TrueHDError, OSError, subprocess.TimeoutExpired) as e:
            return LayoutTest(layout, bits, False, f"{label} decode failed: {e}")
        if len(decoded) != len(pcm):
            return LayoutTest(layout, bits, False,
                              f"{label}: sample count {len(decoded) // channels} != {frames}")
        problem = describe_mapping_problem(layout, identify_channels(pcm, decoded, channels))
        if problem:
            return LayoutTest(layout, bits, False, f"{label}: channel mapping wrong: {problem}")
        if decoded != pcm:
            return LayoutTest(layout, bits, False, f"{label}: decoded samples are not identical")

    sync = read_major_sync(encoder, out_path, 0)
    sync_error = check_channel_assignment(layout, sync)
    if sync_error:
        return LayoutTest(layout, bits, False, sync_error)
    # The header stores the rate in steps of sample_rate / 16 (3000 b/s at 48 kHz)
    if abs(sync.peak_bitrate - TRUEHD_PEAK_BITRATE) > sync.sample_rate // 16 + 1:
        return LayoutTest(layout, bits, False,
                          f"header declares {sync.peak_bitrate / 1e6:.2f} Mb/s peak, expected "
                          f"{TRUEHD_PEAK_BITRATE / 1e6:.0f} Mb/s")

    checked = " and ".join("independent FFmpeg" if d != encoder else "encoder build" for d in decoders)
    return LayoutTest(layout, bits, True,
                      f"bit-exact, all {channels} channels in place (decoded by {checked}); "
                      f"header {MajorSync.describe(EXPECTED_CHANNEL_ASSIGNMENT[layout][1])}")


def check_encoder(
    configured: Optional[str],
    reference_decoder: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
) -> EncoderReport:
    """Locate, inspect and self-test a TrueHD encoder."""
    path, origin = discover_encoder(configured, exclude=[reference_decoder])
    report = EncoderReport(path=path, origin=origin)
    if not path:
        report.message = ("No LibreMPEG executable found. Use Browse to select a "
                          "LibreMPEG ffmpeg executable.")
        if configured:
            report.message = f"Configured encoder not found: {configured}. " + report.message
        return report

    caps = query_capabilities(path)
    report.capabilities = caps
    if caps.error:
        report.message = caps.error
        return report
    if not caps.has_encoder:
        report.message = f"{Path(path).name} has no TrueHD encoder"
        return report

    missing = caps.missing_features()

    if log:
        log(f"Checking TrueHD encoder: {path}")
        if caps.version:
            log(f"  {caps.version}")
        log(f"  Advertised layouts: {' '.join(caps.layouts) or 'none'}")

    with tempfile.TemporaryDirectory(prefix="dts2dd-selftest-") as workdir:
        for layout in SUPPORTED_LAYOUTS:
            for bits in (24, 16):
                if layout not in caps.layouts:
                    report.tests.append(LayoutTest(layout, bits, False, "layout not advertised"))
                    continue
                if missing:
                    report.tests.append(LayoutTest(layout, bits, False, "; ".join(missing)))
                    continue
                required_fmt = "s32p" if bits == 24 else "s16p"
                if required_fmt not in caps.sample_formats:
                    report.tests.append(LayoutTest(layout, bits, False,
                                                   f"{required_fmt} not advertised"))
                    continue
                test = run_layout_test(path, layout, bits, workdir, reference_decoder)
                report.tests.append(test)
                if log:
                    mark = "PASS" if test.passed else "FAIL"
                    log(f"  Self-test {LAYOUT_LABELS[layout]} {bits}-bit: {mark} - {test.detail}")

    verified = report.verified_layouts
    first_failure = lambda layout: next(
        (t.detail for t in report.tests if t.layout == layout and not t.passed), "")
    if "7.1" in verified:
        report.status = STATUS_READY
        report.message = ("TrueHD 5.1 and 7.1 verified bit-exact" if "5.1(side)" in verified
                          else "TrueHD 7.1 verified bit-exact (5.1 not verified)")
    elif "7.1" not in caps.layouts:
        report.status = STATUS_NO_71
        report.message = ("This encoder's TrueHD encoder does not support 7.1 "
                          "(standard FFmpeg stops at 5.1). ")
        report.message += ("TrueHD 5.1 verified." if verified
                           else f"5.1 also failed: {first_failure('5.1(side)')}")
    elif missing:
        report.status = STATUS_NOT_VERIFIED
        report.message = ("This LibreMPEG build cannot be used: " + "; ".join(missing)
                          + ". Use the LibreMPEG included with DTS2DD 1.1.3 or later.")
    else:
        report.status = STATUS_NOT_VERIFIED
        report.message = f"7.1 self-test failed: {first_failure('7.1')}"
        if verified:
            report.message += " (TrueHD 5.1 verified)"
    return report


# --------------------------------------------------------------------------
# Conversion command
# --------------------------------------------------------------------------

@dataclass
class TrueHDJob:
    source_path: str
    output_path: str
    source_stream_index: int        # absolute stream index of the source audio
    audio_stream_count: int         # audio streams in the source file
    layout: str                     # FFmpeg layout name of the source
    sample_fmt: str                 # decoder output format (s16p / s32p)
    sample_rate: int
    language: Optional[str] = None
    remove_source: bool = False
    make_default: bool = False

    @property
    def new_audio_index(self) -> int:
        return self.audio_stream_count - (1 if self.remove_source else 0)

    @property
    def title(self) -> str:
        return f"Dolby TrueHD {LAYOUT_LABELS.get(self.layout, self.layout)}"


def build_clip_cut_command(encoder: str, source: str, output: str,
                           start: float, duration: float) -> List[str]:
    """Stream-copy a section of the film (no encoding) for a test clip.

    Test clips are made in two steps - cut by stream copy, then convert the
    cut like a whole film - because seeking and encoding in one FFmpeg pass
    gave the TrueHD track a start timestamp ~0.25 s later than its audio
    (out of sync). Stream-copy cutting keeps every track's relative timing.
    """
    return [encoder, "-hide_banner", "-nostdin", "-ss", f"{start:.6f}", "-t", f"{duration:.6f}",
            "-i", source, "-map", "0", "-c", "copy", "-default_mode", "passthrough",
            "-progress", "pipe:1", "-nostats", "-y", output]


def build_truehd_command(encoder: str, job: TrueHDJob,
                         source_hash_path: Optional[str] = None,
                         packet_sizes_path: Optional[str] = None) -> List[str]:
    """LibreMPEG command that copies every stream and appends a TrueHD track.

    * Video, subtitles, other audio, chapters and attachments are copied
      bit-for-bit (``-c copy``); only the appended stream is encoded.
    * The output layout, sample format and rate are forced to the source's
      values, so FFmpeg can never insert a silent downmix or resample - the
      encoder fails instead.
    * No filters are used: no normalisation, DRC, EQ or remixing.
    * The header declares an 18 Mb/s peak data rate (see TRUEHD_PEAK_BITRATE);
      ``packet_sizes_path`` records every access unit's size so the app can
      prove each one fits.
    * With ``source_hash_path``, a second output writes the SHA-256 of the
      decoded source PCM - the exact samples fed to the encoder - in the same
      pass, so strict verification never has to re-read the source film.
    """
    from ffmpeg_helper import build_stream_maps  # shared with AC-3 workflow

    k = job.new_audio_index
    cmd = [encoder, "-hide_banner", "-nostdin", "-i", job.source_path]
    cmd += build_stream_maps(job.source_stream_index, job.remove_source)
    cmd += [
        "-c", "copy",
        f"-c:a:{k}", "truehd",
        f"-strict:a:{k}", "experimental",
        f"-ch_layout:a:{k}", job.layout,
        f"-sample_fmt:a:{k}", job.sample_fmt,
        f"-ar:a:{k}", str(job.sample_rate),
        f"-peak_bitrate:a:{k}", str(TRUEHD_PEAK_BITRATE),
        f"-bsf:a:{k}", END_PADDING_FIX,
        f"-metadata:s:a:{k}", f"title={job.title}",
    ]
    if packet_sizes_path:
        cmd += [f"-stats_enc_post:a:{k}", packet_sizes_path,
                f"-stats_enc_post_fmt:a:{k}", "{size}"]
    if job.language:
        cmd += [f"-metadata:s:a:{k}", f"language={job.language}"]
    if job.make_default:
        for j in range(k):
            cmd += [f"-disposition:a:{j}", "-default"]
        cmd += [f"-disposition:a:{k}", "default"]
    else:
        cmd += [f"-disposition:a:{k}", "0"]
    cmd += [
        "-default_mode", "passthrough",
        "-progress", "pipe:1", "-nostats",
        "-y", job.output_path,
    ]
    if source_hash_path:
        # A real file, not pipe:N - extra pipe descriptors don't work on Windows.
        cmd += ["-map", f"0:{job.source_stream_index}", "-c:a", "pcm_s32le",
                "-f", "hash", "-hash", "sha256", source_hash_path]
    return cmd


def build_output_hash_command(decoder: str, output_path: str, audio_index: int,
                              hash_path: str) -> List[str]:
    """SHA-256 of the TrueHD track decoded from the finished MKV.

    Decoding from the MKV (not the encoder's raw packets) also covers
    container-level problems such as the end-of-stream padding bug.
    """
    return [decoder, "-hide_banner", "-nostdin", "-v", "error", "-i", output_path,
            "-map", f"0:a:{audio_index}", "-c:a", "pcm_s32le",
            "-f", "hash", "-hash", "sha256",
            "-progress", "pipe:1", "-nostats", "-y", hash_path]


def read_hash_file(path: str) -> Optional[str]:
    """Return the hex digest from an FFmpeg ``-f hash`` file (``SHA256=<hex>``)."""
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            for line in f:
                name, _, value = line.strip().partition("=")
                if name.upper() == "SHA256" and re.fullmatch(r"[0-9a-fA-F]{64}", value):
                    return value.lower()
    except OSError:
        pass
    return None


def check_access_unit_sizes(sizes_path: str, peak_bitrate: int,
                            sample_rate: int) -> Tuple[Optional[str], str]:
    """Check every encoded access unit fits the declared peak data rate.

    Returns (error or None, summary).
    """
    limit = max_access_unit_bytes(peak_bitrate, sample_rate)
    try:
        with open(sizes_path, "r", encoding="ascii", errors="replace") as f:
            sizes = [int(line) for line in f if line.strip().isdigit()]
    except OSError as e:
        return f"access unit sizes were not recorded ({e})", ""
    if not sizes:
        return "access unit sizes were not recorded", ""
    au_seconds = 40 / (44100 if sample_rate % 44100 == 0 else 48000)
    largest = max(sizes)
    over = [i for i, size in enumerate(sizes) if size > limit]
    summary = (f"largest access unit {largest} bytes of {limit:.0f} allowed "
               f"({largest * 8 / au_seconds / 1e6:.1f} of {peak_bitrate / 1e6:.1f} Mb/s), "
               f"{len(sizes):,} checked")
    if over:
        t = over[0] * au_seconds
        return (f"{len(over)} access unit(s) exceed the declared {peak_bitrate / 1e6:.1f} Mb/s peak, "
                f"first at {int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:06.3f} - "
                "AV receivers may glitch there"), summary
    return None, summary


def build_source_decode_command(encoder: str, job: TrueHDJob) -> List[str]:
    """Decode the source audio exactly as the encoder received it."""
    return [encoder, "-hide_banner", "-nostdin", "-v", "error",
            "-i", job.source_path, "-map", f"0:{job.source_stream_index}",
            "-c:a", "pcm_s32le", "-f", "s32le", "-"]


def build_output_decode_command(decoder: str, output_path: str, audio_index: int) -> List[str]:
    return [decoder, "-hide_banner", "-nostdin", "-v", "error", "-i", output_path,
            "-map", f"0:a:{audio_index}", "-c:a", "pcm_s32le", "-f", "s32le", "-"]


# --------------------------------------------------------------------------
# Speaker identification test file (for checking on the receiver)
# --------------------------------------------------------------------------

# Order speakers are played in, like an AVR test-tone sequence.
CHANNEL_TEST_ORDER = ["FL", "FC", "FR", "SR", "BR", "BL", "SL", "LFE"]
_SLOT_SECONDS = 3.0
_LEAD_SECONDS = 2.0


def channel_test_schedule(layout: str = "7.1") -> List[Tuple[float, float, str, int]]:
    """(start, end, channel, pip_count) for each speaker in the test file."""
    order = [c for c in CHANNEL_TEST_ORDER if c in LAYOUT_CHANNELS[layout]]
    schedule = []
    for n, channel in enumerate(order, start=1):
        start = _LEAD_SECONDS + (n - 1) * _SLOT_SECONDS
        schedule.append((start, start + _SLOT_SECONDS - 0.5, channel, n))
    return schedule


def synth_channel_test_pcm(layout: str = "7.1", rounds: int = 2) -> Tuple[array.array, float]:
    """24-bit PCM: each speaker in turn plays N pips (N = its position)."""
    channels = LAYOUT_CHANNELS[layout]
    nch = len(channels)
    schedule = channel_test_schedule(layout)
    round_seconds = _LEAD_SECONDS + len(schedule) * _SLOT_SECONDS
    total_seconds = round_seconds * rounds + 1.0
    frames = int(48000 * total_seconds)
    pcm = array.array("i", bytes(4 * nch * frames))

    pip_frames, gap_frames = int(0.15 * 48000), int(0.12 * 48000)
    fade = 240
    for r in range(rounds):
        for start, _end, channel, count in schedule:
            k = channels.index(channel)
            freq = 60.0 if channel == "LFE" else 1000.0
            amp = 0.25 * (1 << 23)
            pip = []
            for i in range(pip_frames):
                env = min(1.0, i / fade, (pip_frames - i) / fade)
                pip.append(int(amp * env * math.sin(2 * math.pi * freq * i / 48000)) << 8)
            first = int((r * round_seconds + start) * 48000)
            for p in range(count):
                offset = first + p * (pip_frames + gap_frames)
                pcm[offset * nch + k:(offset + pip_frames) * nch + k:nch] = array.array("i", pip)
    return pcm, total_seconds


def channel_test_subtitles(layout: str = "7.1", rounds: int = 2) -> str:
    schedule = channel_test_schedule(layout)
    round_seconds = _LEAD_SECONDS + len(schedule) * _SLOT_SECONDS

    def ts(t: float) -> str:
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    entries = []
    n = 1
    for r in range(rounds):
        base = r * round_seconds
        entries.append(f"{n}\n{ts(base)} --> {ts(base + _LEAD_SECONDS - 0.2)}\n"
                       f"Dolby TrueHD {LAYOUT_LABELS[layout]} speaker test - round {r + 1}\n")
        n += 1
        for start, end, channel, count in schedule:
            pips = "pip" if count == 1 else "pips"
            entries.append(f"{n}\n{ts(base + start)} --> {ts(base + end)}\n"
                           f"{count} {pips}: {CHANNEL_NAMES[channel]}\n")
            n += 1
    return "\n".join(entries)


def create_channel_test_file(
    encoder: str,
    output_path: str,
    video_ffmpeg: Optional[str] = None,
    layout: str = "7.1",
    log: Optional[Callable[[str], None]] = None,
) -> str:
    """Create an MKV with a labelled TrueHD speaker-identification signal.

    The file has a black H.264 (or MPEG-4) video track so Plex treats it as
    a video, and a forced subtitle track naming the speaker that should be
    playing. The TrueHD track is verified bit-exact before returning.
    """
    if os.path.exists(output_path):
        raise TrueHDError(f"{output_path} already exists; choose another name")
    pcm, seconds = synth_channel_test_pcm(layout)
    nch = len(LAYOUT_CHANNELS[layout])
    with tempfile.TemporaryDirectory(prefix="dts2dd-chtest-") as workdir:
        raw = os.path.join(workdir, "test.raw")
        srt = os.path.join(workdir, "test.srt")
        video = os.path.join(workdir, "video.mkv")
        with open(raw, "wb") as f:
            f.write(_to_little_endian_bytes(pcm))
        with open(srt, "w", encoding="utf-8") as f:
            f.write(channel_test_subtitles(layout))

        have_video = False
        video_tool = video_ffmpeg or encoder
        for codec_args in (["-c:v", "libx264", "-pix_fmt", "yuv420p", "-tune", "stillimage"],
                           ["-c:v", "mpeg4", "-q:v", "5"]):
            cmd = [video_tool, "-hide_banner", "-nostdin", "-v", "error",
                   "-f", "lavfi", "-i", f"color=c=black:s=1280x720:r=24:d={seconds:.3f}",
                   *codec_args, "-y", video]
            if _run_text(cmd, timeout=300).returncode == 0:
                have_video = True
                break

        cmd = [encoder, "-hide_banner", "-nostdin", "-v", "error"]
        if have_video:
            cmd += ["-i", video]
        cmd += ["-f", "s32le", "-ar", "48000", "-ch_layout", layout, "-i", raw, "-i", srt]
        a, s = (1, 2) if have_video else (0, 1)
        if have_video:
            cmd += ["-map", "0:v"]
        cmd += [
            "-map", f"{a}:a", "-map", f"{s}:s",
            "-c:v", "copy", "-c:s", "srt",
            "-c:a", "truehd", "-strict:a:0", "experimental",
            "-ch_layout:a:0", layout, "-sample_fmt:a:0", "s32p", "-bsf:a:0", END_PADDING_FIX,
            "-peak_bitrate:a:0", str(TRUEHD_PEAK_BITRATE),
            "-metadata:s:a:0", f"title=Dolby TrueHD {LAYOUT_LABELS[layout]} speaker test",
            "-metadata:s:s:0", "title=Speaker names",
            "-disposition:s:0", "default+forced",
            "-metadata", f"title=Dolby TrueHD {LAYOUT_LABELS[layout]} speaker identification test",
            "-y", output_path,
        ]
        result = _run_text(cmd, timeout=600)
        if result.returncode != 0:
            if os.path.exists(output_path):
                os.unlink(output_path)
            raise TrueHDError("Could not create the test file:\n" + result.stderr.strip()[-600:])

    decoded = _from_little_endian_bytes(decode_to_s32(video_ffmpeg or encoder, output_path))
    mapping = identify_channels(pcm, decoded, nch)
    problem = describe_mapping_problem(layout, mapping)
    if len(decoded) != len(pcm) or problem or decoded != pcm:
        os.unlink(output_path)
        raise TrueHDError(f"Speaker test file failed verification: {problem or 'samples differ'}")
    sync_error = check_channel_assignment(layout, read_major_sync(encoder, output_path, 0))
    if sync_error:
        os.unlink(output_path)
        raise TrueHDError(sync_error)
    if log:
        log(f"Speaker test file verified bit-exact: {output_path}")
    return output_path
