"""Read-only Dolby Atmos signalling analysis for E-AC-3 and TrueHD tracks.

Nothing here modifies a file. The scanner extracts a few seconds of a track
by stream copy (see media_inspector.read_stream_sample) and these functions
parse the raw bitstream:

E-AC-3 (ETSI TS 102 366 Annex E)
    Every syncframe's bit stream information (bsi) is parsed field by field,
    up to the additional bit stream information (addbsi). Dolby Digital Plus
    with Atmos (JOC, ETSI TS 103 420) is signalled there by
    ``flag_ec3_extension_type_a`` (the LSB of the first addbsi byte),
    followed by an 8-bit ``complexity_index_type_a`` (the object count).
    This is the same field FFmpeg's decoder and MediaInfo use. The JOC
    payload itself (in EMDF) is NOT decoded, so "confirmed" means "JOC is
    signalled consistently in the bitstream", not "the object audio decodes".

Dolby TrueHD
    The major sync header's ``substream_info`` bit 7 signals the presence
    of the 16-channel presentation that carries Atmos; the substream count
    (4 for Atmos) is reported alongside.

These parsers were validated against FFmpeg-encoded streams and synthetic
signalling fixtures, NOT against genuine Atmos masters. A disagreement
between this parser, ffprobe and MediaInfo is reported as "manual
inspection recommended" rather than resolved by guessing.
"""
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple


class BitstreamError(ValueError):
    """Raised when a frame is truncated or malformed."""


class BitReader:
    """MSB-first bit reader over a bytes object."""

    def __init__(self, data: bytes):
        self.value = int.from_bytes(data, "big")
        self.total = len(data) * 8
        self.pos = 0

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        if self.pos + n > self.total:
            raise BitstreamError("frame truncated")
        shift = self.total - self.pos - n
        self.pos += n
        return (self.value >> shift) & ((1 << n) - 1)

    def flag(self) -> bool:
        return bool(self.read(1))

    def skip(self, n: int) -> None:
        if self.pos + n > self.total:
            raise BitstreamError("frame truncated")
        self.pos += n


# --------------------------------------------------------------------------
# AC-3 / E-AC-3 syncframes
# --------------------------------------------------------------------------

AC3_SYNC = b"\x0b\x77"
_AC3_BITRATES = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320,
                 384, 448, 512, 576, 640)
_SAMPLE_RATES = (48000, 44100, 32000)
_ACMOD_NAMES = ("1+1 (dual mono)", "1/0 (mono)", "2/0 (stereo)", "3/0", "2/1", "3/1", "2/2", "3/2")
_ACMOD_CHANNELS = (2, 1, 2, 3, 3, 4, 4, 5)


@dataclass
class Eac3Frame:
    """Parsed bsi of one AC-3 or E-AC-3 syncframe."""
    offset: int
    size: int                      # bytes
    bsid: int
    is_eac3: bool
    strmtyp: int = 0               # 0 independent, 1 dependent, 2 AC-3 converted
    substreamid: int = 0
    sample_rate: int = 0
    blocks: int = 6
    acmod: int = 0
    lfeon: bool = False
    addbsi_present: bool = False
    joc: bool = False              # flag_ec3_extension_type_a
    complexity_index: Optional[int] = None
    addbsie_bit: Optional[int] = None   # bit offset of addbsie within the frame

    @property
    def channels(self) -> int:
        return _ACMOD_CHANNELS[self.acmod] + (1 if self.lfeon else 0)

    @property
    def channel_mode(self) -> str:
        return _ACMOD_NAMES[self.acmod] + (" + LFE" if self.lfeon else "")


def ac3_frame_size(header: bytes) -> Optional[int]:
    """Size in bytes of the AC-3/E-AC-3 frame starting with ``header`` (>= 6 bytes)."""
    if len(header) < 6 or header[:2] != AC3_SYNC:
        return None
    bsid = header[5] >> 3
    if bsid <= 10:
        fscod = header[4] >> 6
        frmsizecod = header[4] & 0x3F
        if fscod == 3 or frmsizecod >= 38:
            return None
        kbps = _AC3_BITRATES[frmsizecod >> 1]
        if fscod == 0:
            words = 2 * kbps
        elif fscod == 1:
            words = kbps * 1000 * 1536 // 44100 // 16 + (frmsizecod & 1)
        else:
            words = 3 * kbps
        return words * 2
    if 11 <= bsid <= 16:
        frmsiz = ((header[2] & 0x07) << 8) | header[3]
        return (frmsiz + 1) * 2
    return None


def parse_ac3_frame(frame: bytes, offset: int = 0) -> Eac3Frame:
    """Parse the bsi of one syncframe (AC-3 basic fields, E-AC-3 in full)."""
    r = BitReader(frame)
    if r.read(16) != 0x0B77:
        raise BitstreamError("no sync word")
    # bsid is at bit 40 in both AC-3 and E-AC-3 syncframes
    bsid = (frame[5] >> 3) if len(frame) > 5 else -1
    if bsid <= 10:
        r.skip(16)                 # crc1
        fscod = r.read(2)
        r.skip(6)                  # frmsizecod
        r.skip(5)                  # bsid
        r.skip(3)                  # bsmod
        acmod = r.read(3)
        if (acmod & 1) and acmod != 1:
            r.skip(2)              # cmixlev
        if acmod & 4:
            r.skip(2)              # surmixlev
        if acmod == 2:
            r.skip(2)              # dsurmod
        lfeon = r.flag()
        return Eac3Frame(offset, len(frame), bsid, False, sample_rate=_SAMPLE_RATES[fscod] if fscod < 3 else 0,
                         acmod=acmod, lfeon=lfeon)
    if not 11 <= bsid <= 16:
        raise BitstreamError(f"unsupported bsid {bsid}")

    f = Eac3Frame(offset, len(frame), bsid, True)
    f.strmtyp = r.read(2)
    f.substreamid = r.read(3)
    r.skip(11)                     # frmsiz
    fscod = r.read(2)
    if fscod == 3:
        fscod2 = r.read(2)
        if fscod2 == 3:
            raise BitstreamError("reserved sample rate code")
        f.sample_rate = _SAMPLE_RATES[fscod2] // 2
        numblkscod = 3
    else:
        f.sample_rate = _SAMPLE_RATES[fscod]
        numblkscod = r.read(2)
    f.blocks = (1, 2, 3, 6)[numblkscod]
    f.acmod = acmod = r.read(3)
    f.lfeon = lfeon = r.flag()
    r.skip(5)                      # bsid
    for _ in range(1 if acmod else 2):
        r.skip(5)                  # dialnorm(2)
        if r.flag():               # compre(2)
            r.skip(8)
    if f.strmtyp == 1 and r.flag():    # chanmape
        r.skip(16)                 # chanmap
    if r.flag():                   # mixmdate
        if acmod > 2:
            r.skip(2)              # dmixmod
        if (acmod & 1) and acmod > 2:
            r.skip(6)              # ltrtcmixlev, lorocmixlev
        if acmod & 4:
            r.skip(6)              # ltrtsurmixlev, lorosurmixlev
        if lfeon and r.flag():     # lfemixlevcode
            r.skip(5)
        if f.strmtyp == 0:
            for _ in range(1 if acmod else 2):
                if r.flag():       # pgmscle(2)
                    r.skip(6)
            if r.flag():           # extpgmscle
                r.skip(6)
            mixdef = r.read(2)
            if mixdef == 1:
                r.skip(5)          # premixcmpsel, drcsrc, premixcmpscl
            elif mixdef == 2:
                r.skip(12)
            elif mixdef == 3:
                r.skip((r.read(5) + 2) * 8)
            if acmod < 2:
                for _ in range(1 if acmod else 2):
                    if r.flag():   # paninfoe(2)
                        r.skip(14)
            if r.flag():           # frmmixcfginfoe
                if numblkscod == 0:
                    r.skip(5)
                else:
                    for _ in range(f.blocks):
                        if r.flag():
                            r.skip(5)
    if r.flag():                   # infomdate
        r.skip(5)                  # bsmod, copyrightb, origbs
        if acmod == 2:
            r.skip(4)              # dsurmod, dheadphonmod
        if acmod >= 6:
            r.skip(2)              # dsurexmod
        for _ in range(1 if acmod else 2):
            if r.flag():           # audprodie(2)
                r.skip(8)          # mixlevel, roomtyp, adconvtyp
        if fscod < 3:
            r.skip(1)              # sourcefscod
    if f.strmtyp == 0 and numblkscod != 3:
        r.skip(1)                  # convsync
    if f.strmtyp == 2:
        blkid = 1 if numblkscod == 3 else r.read(1)
        if blkid:
            r.skip(6)              # frmsizecod
    f.addbsie_bit = r.pos
    if r.flag():                   # addbsie
        f.addbsi_present = True
        addbsil = r.read(6)
        first = r.read(8)
        f.joc = bool(first & 1)    # flag_ec3_extension_type_a
        if f.joc:
            if addbsil < 1:
                raise BitstreamError("JOC flag set but complexity index missing")
            f.complexity_index = r.read(8)
    return f


def iter_ac3_frames(data: bytes, max_frames: int = 10_000) -> Iterator[Tuple[int, bytes]]:
    """(offset, frame) for each syncframe confirmed by the next sync word."""
    pos = data.find(AC3_SYNC)
    count = 0
    while 0 <= pos and count < max_frames:
        size = ac3_frame_size(data[pos:pos + 6])
        end = pos + size if size else -1
        confirmed = bool(size) and end <= len(data) and (end == len(data) or data[end:end + 2] == AC3_SYNC)
        if confirmed:
            yield pos, data[pos:end]
            count += 1
            pos = end
        else:
            pos = data.find(AC3_SYNC, pos + 1)


@dataclass
class Eac3Summary:
    """What the parsed frames say about one track."""
    frames: int = 0                 # independent substream 0 frames parsed
    joc_frames: int = 0
    complexity: List[int] = field(default_factory=list)
    errors: int = 0
    first_error: str = ""
    eac3: bool = False              # bsid 11-16 seen
    ac3: bool = False               # bsid <= 10 seen
    dependent_substreams: int = 0
    channel_mode: str = ""
    sample_rate: int = 0

    def to_dict(self) -> Dict:
        return asdict(self)


def summarize_ac3_bitstream(data: bytes, max_frames: int = 4000) -> Eac3Summary:
    summary = Eac3Summary()
    dependent_ids = set()
    for offset, frame in iter_ac3_frames(data, max_frames):
        try:
            f = parse_ac3_frame(frame, offset)
        except BitstreamError as e:
            summary.errors += 1
            summary.first_error = summary.first_error or str(e)
            continue
        if not f.is_eac3:
            summary.ac3 = True
            summary.frames += 1
            summary.channel_mode = summary.channel_mode or f.channel_mode
            summary.sample_rate = summary.sample_rate or f.sample_rate
            continue
        summary.eac3 = True
        if f.strmtyp == 1:
            dependent_ids.add(f.substreamid)
            continue
        if f.substreamid != 0:
            continue
        summary.frames += 1
        summary.channel_mode = summary.channel_mode or f.channel_mode
        summary.sample_rate = summary.sample_rate or f.sample_rate
        if f.joc:
            summary.joc_frames += 1
            if f.complexity_index is not None and f.complexity_index not in summary.complexity:
                summary.complexity.append(f.complexity_index)
    summary.dependent_substreams = len(dependent_ids)
    return summary


# --------------------------------------------------------------------------
# TrueHD major sync
# --------------------------------------------------------------------------

TRUEHD_SYNC = b"\xf8\x72\x6f\xba"
TRUEHD_SIGNATURE = b"\xb7\x52"


@dataclass
class TrueHDSync:
    offset: int
    sample_rate: int
    substreams: int
    substream_info: int
    extended_substream_info: int

    @property
    def has_16ch_presentation(self) -> bool:
        return bool(self.substream_info & 0x80)


def parse_truehd_syncs(data: bytes, limit: int = 2000) -> List[TrueHDSync]:
    """Every TrueHD major sync header (with a valid signature) in ``data``."""
    syncs = []
    pos = data.find(TRUEHD_SYNC)
    while pos >= 0 and len(syncs) < limit:
        if len(data) >= pos + 18 and data[pos + 8:pos + 10] == TRUEHD_SIGNATURE:
            rate_code = data[pos + 4] >> 4
            if rate_code != 0xF:
                rate = (44100 if rate_code & 8 else 48000) << (rate_code & 7)
                syncs.append(TrueHDSync(
                    offset=pos, sample_rate=rate,
                    substreams=data[pos + 16] >> 4,
                    extended_substream_info=data[pos + 16] & 0x03,
                    substream_info=data[pos + 17],
                ))
        pos = data.find(TRUEHD_SYNC, pos + 4)
    return syncs


@dataclass
class TrueHDSummary:
    syncs: int = 0
    atmos_syncs: int = 0
    substreams: List[int] = field(default_factory=list)
    sample_rate: int = 0

    def to_dict(self) -> Dict:
        return asdict(self)


def summarize_truehd_bitstream(data: bytes) -> TrueHDSummary:
    summary = TrueHDSummary()
    for sync in parse_truehd_syncs(data):
        summary.syncs += 1
        summary.sample_rate = summary.sample_rate or sync.sample_rate
        if sync.has_16ch_presentation:
            summary.atmos_syncs += 1
        if sync.substreams not in summary.substreams:
            summary.substreams.append(sync.substreams)
    return summary


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

STATUS_DDP_ATMOS = "Valid DD+ Atmos (JOC confirmed)"
STATUS_DDP_UNVERIFIED = "DD+ - Atmos unverified"
STATUS_DDP_CONVENTIONAL = "Conventional DD+ (no JOC)"
STATUS_DDP_NOT_DETECTED = "DD+ - Atmos not detected (limited evidence)"
STATUS_AC3_MISLABELLED = "AC-3 labelled Atmos - misleading metadata"
STATUS_TRUEHD_ATMOS = "Valid TrueHD Atmos"
STATUS_TRUEHD_UNVERIFIED = "TrueHD - Atmos unverified"
STATUS_TRUEHD_NO_ATMOS = "TrueHD (no Atmos signalled)"
STATUS_INCONSISTENT = "Manual inspection recommended"
STATUS_NOT_ATMOS = "Not Atmos"

# A few seconds of audio is ~30 E-AC-3 frames per second; fewer than this
# many parsed frames is too little to call Atmos absent.
MIN_FRAMES_FOR_ABSENCE = 30
MIN_SYNCS_FOR_ABSENCE = 3


@dataclass
class AtmosAnalysis:
    status: str
    atmos: Optional[bool]               # True confirmed, False absent, None unknown
    confidence: str = "low"             # high / medium / low
    evidence: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    title_claims_atmos: bool = False
    object_count: Optional[int] = None  # E-AC-3 complexity index

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> Optional["AtmosAnalysis"]:
        if not data:
            return None
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def title_claims_atmos(title: Optional[str]) -> bool:
    text = (title or "").lower()
    return "atmos" in text or " joc" in f" {text}"


def _mediainfo_text(mediainfo: Optional[Dict]) -> str:
    if not mediainfo:
        return ""
    keys = ("Format", "Format_Commercial_IfAny", "Format_Commercial", "Format_AdditionalFeatures",
            "Format_Profile", "Format_Settings")
    return " | ".join(str(mediainfo[k]) for k in keys if mediainfo.get(k))


def analyse_eac3(profile: Optional[str], title: Optional[str],
                 bitstream: Optional[Eac3Summary], mediainfo: Optional[Dict] = None) -> AtmosAnalysis:
    """Classify an E-AC-3 track from ffprobe, MediaInfo and the bitstream."""
    claims = title_claims_atmos(title)
    a = AtmosAnalysis(status=STATUS_DDP_NOT_DETECTED, atmos=None, title_claims_atmos=claims)
    yes, no, weak_no = [], [], []

    if bitstream is not None and bitstream.frames:
        n, k = bitstream.frames, bitstream.joc_frames
        if bitstream.ac3 and not bitstream.eac3:
            a.conflicts.append("bitstream contains AC-3 (bsid <= 10) frames, not E-AC-3")
        if k == n:
            objects = ", ".join(str(c) for c in bitstream.complexity) or "?"
            yes.append(f"bitstream: JOC flag (flag_ec3_extension_type_a) set in all {n} parsed "
                       f"frames; complexity index {objects}")
            a.object_count = max(bitstream.complexity) if bitstream.complexity else None
        elif k == 0:
            msg = f"bitstream: JOC flag absent in all {n} parsed frames"
            (no if n >= MIN_FRAMES_FOR_ABSENCE else weak_no).append(msg)
        else:
            a.conflicts.append(f"bitstream: JOC flag set in only {k} of {n} parsed frames")
        if bitstream.errors:
            a.evidence.append(f"bitstream: {bitstream.errors} frame(s) could not be parsed "
                              f"({bitstream.first_error})")
        if bitstream.dependent_substreams:
            a.evidence.append(f"bitstream: {bitstream.dependent_substreams} dependent substream(s) "
                              "(extra channels beyond 5.1)")
    elif bitstream is not None:
        a.evidence.append("bitstream: no E-AC-3 frames could be parsed from the sample")
    else:
        a.evidence.append("bitstream: not analysed")

    prof = profile or ""
    if "atmos" in prof.lower():
        yes.append(f"ffprobe profile: '{prof}'")
    else:
        weak_no.append(f"ffprobe profile: '{prof or 'none'}' (no Atmos profile reported)")

    if mediainfo is not None:
        text = _mediainfo_text(mediainfo)
        if "joc" in text.lower() or "atmos" in text.lower():
            yes.append(f"MediaInfo: {text}")
        elif text:
            # MediaInfo reads the same flag, so it only contradicts a bitstream "yes".
            target = no if bitstream is not None and bitstream.joc_frames else weak_no
            target.append(f"MediaInfo: {text} (no JOC / Atmos reported)")

    if claims:
        a.evidence.append(f"track title: '{title}' claims Atmos")

    bitstream_yes = any(e.startswith("bitstream") for e in yes)
    bitstream_no = any(e.startswith("bitstream") for e in no)
    tool_yes = [e for e in yes if not e.startswith("bitstream")]
    tool_no = [e for e in no if not e.startswith("bitstream")]
    a.evidence = yes + no + weak_no + a.evidence

    if bitstream_no and tool_yes:
        a.conflicts.append("a metadata tool reports Atmos but the bitstream does not signal JOC")
    if bitstream_yes and tool_no:
        a.conflicts.append("the bitstream signals JOC but a metadata tool reports no Atmos")

    if a.conflicts:
        a.status, a.atmos, a.confidence = STATUS_INCONSISTENT, None, "low"
    elif bitstream_yes:
        a.status, a.atmos = STATUS_DDP_ATMOS, True
        a.confidence = "high" if tool_yes else "medium"
    elif bitstream_no:
        a.status, a.atmos, a.confidence = STATUS_DDP_CONVENTIONAL, False, "high"
    elif tool_yes:
        a.status, a.atmos, a.confidence = STATUS_DDP_ATMOS, True, "medium"
    elif claims:
        a.status, a.atmos, a.confidence = STATUS_DDP_UNVERIFIED, None, "low"
    else:
        a.status, a.atmos, a.confidence = STATUS_DDP_NOT_DETECTED, None, "low"
    return a


def analyse_truehd(profile: Optional[str], title: Optional[str],
                   bitstream: Optional[TrueHDSummary], mediainfo: Optional[Dict] = None) -> AtmosAnalysis:
    claims = title_claims_atmos(title)
    a = AtmosAnalysis(status=STATUS_TRUEHD_UNVERIFIED, atmos=None, title_claims_atmos=claims)
    yes, no, weak_no, notes = [], [], [], []

    if bitstream is not None and bitstream.syncs:
        n, k = bitstream.syncs, bitstream.atmos_syncs
        subs = "/".join(str(s) for s in bitstream.substreams)
        if k == n:
            yes.append(f"bitstream: 16-channel presentation flagged in all {n} major sync headers "
                       f"({subs} substreams)")
            if 4 not in bitstream.substreams:
                notes.append("bitstream: Atmos TrueHD normally has 4 substreams")
        elif k == 0:
            msg = f"bitstream: no 16-channel (Atmos) presentation in {n} major sync headers ({subs} substreams)"
            (no if n >= MIN_SYNCS_FOR_ABSENCE else weak_no).append(msg)
        else:
            a.conflicts.append(f"bitstream: Atmos presentation flagged in only {k} of {n} major sync headers")
    elif bitstream is not None:
        notes.append("bitstream: no TrueHD major sync header found in the sample")
    else:
        notes.append("bitstream: not analysed")

    prof = profile or ""
    if "atmos" in prof.lower():
        yes.append(f"ffprobe profile: '{prof}'")
    else:
        weak_no.append(f"ffprobe profile: '{prof or 'none'}' (no Atmos profile reported)")
    if mediainfo is not None:
        text = _mediainfo_text(mediainfo)
        if "16-ch" in text.lower() or "atmos" in text.lower():
            yes.append(f"MediaInfo: {text}")
        elif text:
            weak_no.append(f"MediaInfo: {text} (no Atmos reported)")
    if claims:
        notes.append(f"track title: '{title}' claims Atmos")

    bitstream_yes = any(e.startswith("bitstream") for e in yes)
    bitstream_no = any(e.startswith("bitstream") for e in no)
    tool_yes = [e for e in yes if not e.startswith("bitstream")]
    a.evidence = yes + no + weak_no + notes
    if bitstream_no and tool_yes:
        a.conflicts.append("a metadata tool reports Atmos but no Atmos presentation is signalled")

    if a.conflicts:
        a.status, a.atmos, a.confidence = STATUS_INCONSISTENT, None, "low"
    elif bitstream_yes:
        a.status, a.atmos = STATUS_TRUEHD_ATMOS, True
        a.confidence = "high" if tool_yes else "medium"
    elif bitstream_no:
        a.status, a.atmos, a.confidence = STATUS_TRUEHD_NO_ATMOS, False, "high"
    elif tool_yes:
        a.status, a.atmos, a.confidence = STATUS_TRUEHD_ATMOS, True, "medium"
    else:
        a.status, a.atmos, a.confidence = STATUS_TRUEHD_UNVERIFIED, None, "low"
    return a


def analyse_ac3(title: Optional[str], bitstream: Optional[Eac3Summary]) -> AtmosAnalysis:
    claims = title_claims_atmos(title)
    a = AtmosAnalysis(status=STATUS_NOT_ATMOS, atmos=False, confidence="high", title_claims_atmos=claims)
    a.evidence.append("codec: AC-3 (Dolby Digital) cannot carry Atmos (JOC needs E-AC-3)")
    if bitstream is not None and bitstream.eac3:
        a.conflicts.append("track is tagged AC-3 but the bitstream contains E-AC-3 frames")
        a.status, a.atmos, a.confidence = STATUS_INCONSISTENT, None, "low"
        if bitstream.joc_frames:
            a.conflicts.append(f"JOC flag set in {bitstream.joc_frames} frame(s)")
        return a
    if claims:
        a.status = STATUS_AC3_MISLABELLED
        a.evidence.append(f"track title: '{title}' claims Atmos")
    return a
