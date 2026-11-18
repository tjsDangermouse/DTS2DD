"""Compatibility rules for the Library Scanner.

A rule looks at a MediaReport (confirmed file characteristics) and produces
Findings. General rules describe the file itself; device rules (``device``
set) describe a *potential* problem on one playback device and never claim
the file is defective.

Severity:
  Error        strong evidence of a malformed or unsupported stream
  Warning      likely device compatibility issue or unverified metadata
  Information  valid characteristic that may be relevant

Every rule can be disabled and the thresholds / subtitle preferences are
editable (stored in the app settings under "library_scanner").
"""
import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from app_settings import get_setting, set_setting
from atmos_inspector import (
    STATUS_AC3_MISLABELLED, STATUS_DDP_ATMOS, STATUS_DDP_CONVENTIONAL, STATUS_DDP_NOT_DETECTED,
    STATUS_DDP_UNVERIFIED, STATUS_INCONSISTENT, STATUS_TRUEHD_ATMOS, STATUS_TRUEHD_NO_ATMOS,
    STATUS_TRUEHD_UNVERIFIED,
)
from media_inspector import AudioTrack, MediaReport, SubtitleTrack, VideoTrack
from truehd_workflow import FLAC_LAYOUTS, flac_source_problems

ERROR = "Error"
WARNING = "Warning"
INFO = "Information"
SEVERITY_RANK = {ERROR: 3, WARNING: 2, INFO: 1}

CAT_FILE = "file"
CAT_SUBTITLES = "subtitles"
CAT_FLAC = "flac"
CAT_ATMOS = "atmos"
CAT_DV = "dolby_vision"
CAT_DTS = "dts"
CAT_METADATA = "metadata"

# Repair actions a finding can recommend (implemented in library_repair).
ACTION_CLEAN_SUBS = "clean_subtitles"
ACTION_FLAC_TRUEHD = "flac_to_truehd"
ACTION_DTS_CONVERT = "dts_convert"
ACTION_FIX_DEFAULT = "fix_default_audio"
ACTION_M2TS = "m2ts_remux"          # separate output file (m2ts_remux / m2ts_ui), not an in-place repair
ACTION_LABELS = {
    ACTION_CLEAN_SUBS: "Clean subtitles",
    ACTION_FLAC_TRUEHD: "FLAC → TrueHD",
    ACTION_DTS_CONVERT: "Convert DTS",
    ACTION_FIX_DEFAULT: "Fix default audio",
    ACTION_M2TS: "Test M2TS Remux",
}
UB450_TRUEHD_MESSAGE = ("Potentially unsupported Dolby TrueHD audio over DLNA. M2TS remuxing may improve "
                        "compatibility, but is not guaranteed.")


@dataclass
class Finding:
    rule_id: str
    severity: str
    category: str
    title: str
    detail: str = ""
    evidence: List[str] = field(default_factory=list)
    stream_index: Optional[int] = None
    device: Optional[str] = None          # DEVICE_PROFILES key for device-specific findings
    actions: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Device profiles
# --------------------------------------------------------------------------

@dataclass
class DeviceProfile:
    key: str
    name: str
    description: str


DEVICE_PROFILES: Dict[str, DeviceProfile] = {
    "firetv_plex": DeviceProfile(
        "firetv_plex", "Fire TV Stick 4K Plus - Plex",
        "Plex on Fire TV Stick 4K Plus, HDMI to an AV receiver"),
    "ub450_dlna": DeviceProfile(
        "ub450_dlna", "Panasonic UB450 - DLNA",
        "Panasonic DP-UB450 playing files from a DLNA server"),
    "lgc5_plex": DeviceProfile(
        "lgc5_plex", "LG C5 - Plex",
        "Plex app on an LG C5 TV, audio via (e)ARC"),
}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SETTINGS_KEY = "library_scanner"


@dataclass
class ScannerConfig:
    subtitle_threshold: int = 5                      # warn when MORE than this many
    keep_languages: List[str] = field(default_factory=lambda: ["eng"])
    keep_forced: bool = True
    keep_sdh: bool = True
    remove_duplicate_subs: bool = False
    devices: List[str] = field(default_factory=lambda: list(DEVICE_PROFILES))
    disabled_rules: List[str] = field(default_factory=list)
    bitstream_analysis: bool = True
    video_frame_check: bool = True
    workers: int = 2

    @classmethod
    def load(cls) -> "ScannerConfig":
        data = get_setting(SETTINGS_KEY) or {}
        config = cls()
        for key, value in data.items():
            if key in cls.__dataclass_fields__:
                setattr(config, key, value)
        config.keep_languages = [lang for lang in (normalize_language(l) for l in config.keep_languages) if lang] \
            or ["eng"]
        config.subtitle_threshold = max(0, int(config.subtitle_threshold))
        config.workers = min(8, max(1, int(config.workers)))
        return config

    def save(self) -> None:
        set_setting(SETTINGS_KEY, asdict(self))

    def enabled(self, rule_id: str) -> bool:
        rule = RULES.get(rule_id)
        if rule_id in self.disabled_rules:
            return False
        if rule and rule.device and rule.device not in self.devices:
            return False
        return True


# --------------------------------------------------------------------------
# Languages
# --------------------------------------------------------------------------

UNLABELLED_CODES = {"", "und", "unk", "mis", "mul", "zxx", "none", "xx", "qaa"}
_TWO_LETTER = {
    "en": "eng", "fr": "fre", "de": "ger", "es": "spa", "it": "ita", "pt": "por", "nl": "dut",
    "sv": "swe", "no": "nor", "nb": "nor", "nn": "nor", "da": "dan", "fi": "fin", "pl": "pol",
    "cs": "cze", "hu": "hun", "ru": "rus", "uk": "ukr", "el": "gre", "tr": "tur", "ar": "ara",
    "he": "heb", "hi": "hin", "ja": "jpn", "ko": "kor", "zh": "chi", "th": "tha", "vi": "vie",
    "id": "ind", "ms": "may", "ro": "rum", "bg": "bul", "hr": "hrv", "sr": "srp", "sl": "slv",
    "sk": "slo", "is": "ice", "et": "est", "lv": "lav", "lt": "lit", "ca": "cat", "eu": "baq",
    "gl": "glg", "ta": "tam", "te": "tel", "fa": "per", "sq": "alb", "hy": "arm", "ka": "geo",
    "mk": "mac", "my": "bur", "cy": "wel", "bo": "tib", "fil": "fil", "tl": "tgl",
}
_TERMINOLOGY_TO_BIBLIOGRAPHIC = {
    "fra": "fre", "deu": "ger", "nld": "dut", "ces": "cze", "ell": "gre", "zho": "chi", "ron": "rum",
    "slk": "slo", "isl": "ice", "eus": "baq", "msa": "may", "fas": "per", "sqi": "alb", "hye": "arm",
    "kat": "geo", "mkd": "mac", "mya": "bur", "cym": "wel", "bod": "tib", "nob": "nor", "nno": "nor",
    "scc": "srp", "scr": "hrv",
}
LANGUAGE_NAMES = {
    "english": "eng", "french": "fre", "français": "fre", "francais": "fre", "german": "ger",
    "deutsch": "ger", "spanish": "spa", "español": "spa", "espanol": "spa", "castellano": "spa",
    "italian": "ita", "italiano": "ita", "portuguese": "por", "português": "por", "dutch": "dut",
    "nederlands": "dut", "swedish": "swe", "svenska": "swe", "norwegian": "nor", "norsk": "nor",
    "danish": "dan", "dansk": "dan", "finnish": "fin", "suomi": "fin", "polish": "pol",
    "polski": "pol", "czech": "cze", "hungarian": "hun", "magyar": "hun", "russian": "rus",
    "ukrainian": "ukr", "greek": "gre", "turkish": "tur", "arabic": "ara", "hebrew": "heb",
    "hindi": "hin", "japanese": "jpn", "korean": "kor", "chinese": "chi", "mandarin": "chi",
    "cantonese": "chi", "thai": "tha", "vietnamese": "vie", "indonesian": "ind", "malay": "may",
    "romanian": "rum", "bulgarian": "bul", "croatian": "hrv", "serbian": "srp", "slovenian": "slv",
    "slovak": "slo", "icelandic": "ice", "estonian": "est", "latvian": "lav", "lithuanian": "lit",
    "catalan": "cat", "basque": "baq", "galician": "glg", "tamil": "tam", "telugu": "tel",
    "persian": "per", "farsi": "per",
}
LANGUAGE_DISPLAY = {code: name.title() for name, code in reversed(list(LANGUAGE_NAMES.items()))
                    if name.isascii()}


def normalize_language(code: Optional[str]) -> Optional[str]:
    """ISO 639-2/B code (e.g. 'eng'), or None when the track is unlabelled."""
    text = (code or "").strip().lower().replace("_", "-")
    if text in UNLABELLED_CODES:
        return None
    if text in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[text]
    base = text.split("-")[0]          # BCP 47: en-US -> en
    if base in UNLABELLED_CODES:
        return None
    if len(base) == 2 or base in ("fil",):
        return _TWO_LETTER.get(base, base)
    return _TERMINOLOGY_TO_BIBLIOGRAPHIC.get(base, base)


def language_label(code: Optional[str]) -> str:
    norm = normalize_language(code)
    if norm is None:
        return "no language" if not code else f"'{code}' (undetermined)"
    return LANGUAGE_DISPLAY.get(norm, norm)


def languages_in_title(title: Optional[str]) -> List[str]:
    words = re.findall(r"[a-zà-ÿ]+", (title or "").lower())
    found = []
    for word in words:
        code = LANGUAGE_NAMES.get(word)
        if code and code not in found:
            found.append(code)
    return found


def title_language_conflict(language: Optional[str], title: Optional[str]) -> Optional[str]:
    """Language named in the title when it contradicts the language tag."""
    tag = normalize_language(language)
    named = languages_in_title(title)
    if tag and len(named) == 1 and named[0] != tag:
        return named[0]
    return None


def _word(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


def sub_is_forced(s: SubtitleTrack) -> bool:
    return s.forced or _word((s.title or "").lower(), "forced", "foreign")


def sub_is_sdh(s: SubtitleTrack) -> bool:
    return s.hearing_impaired or _word((s.title or "").lower(), "sdh", "cc", "hoh", "hearing impaired")


def sub_is_commentary(s: SubtitleTrack) -> bool:
    return s.commentary or "commentary" in (s.title or "").lower()


def sub_description(s: SubtitleTrack) -> str:
    from audio_stream_info import SUBTITLE_CODEC_NAMES
    parts = [f"#{s.index}", SUBTITLE_CODEC_NAMES.get(s.codec, s.codec.upper()), language_label(s.language)]
    if s.title:
        parts.append(f'"{s.title}"')
    flags = [name for name, on in (("default", s.default), ("forced", sub_is_forced(s)),
                                   ("SDH", sub_is_sdh(s)), ("commentary", sub_is_commentary(s))) if on]
    if flags:
        parts.append("{" + ", ".join(flags) + "}")
    if s.frames is not None:
        parts.append(f"{s.frames} events")
    return " ".join(parts)


# --------------------------------------------------------------------------
# Subtitle cleanup policy (used by the rules and by library_repair)
# --------------------------------------------------------------------------

@dataclass
class SubtitlePlan:
    keep: List[Tuple[SubtitleTrack, str]] = field(default_factory=list)      # (track, reason)
    remove: List[Tuple[SubtitleTrack, str]] = field(default_factory=list)
    unlabelled: List[SubtitleTrack] = field(default_factory=list)            # no language tag
    notes: List[str] = field(default_factory=list)

    @property
    def remove_indices(self) -> List[int]:
        return [s.index for s, _ in self.remove]

    @property
    def unlabelled_removed(self) -> List[SubtitleTrack]:
        ids = {s.index for s in self.unlabelled}
        return [s for s, _ in self.remove if s.index in ids]


def duplicate_groups(subs: Sequence[SubtitleTrack], languages: Sequence[str]) -> List[List[SubtitleTrack]]:
    """Regular (not forced/SDH/commentary) tracks of a kept language sharing a codec."""
    groups: Dict[Tuple[str, str], List[SubtitleTrack]] = {}
    for s in subs:
        lang = normalize_language(s.language)
        if lang in languages and not (sub_is_forced(s) or sub_is_sdh(s) or sub_is_commentary(s)):
            groups.setdefault((lang, s.codec), []).append(s)
    return [g for g in groups.values() if len(g) > 1]


def subtitle_cleanup_plan(subs: Sequence[SubtitleTrack], config: ScannerConfig,
                          remove_unlabelled: bool = False,
                          remove_duplicates: Optional[bool] = None) -> SubtitlePlan:
    """Which subtitle tracks to keep / remove under the user's preferences.

    Unlabelled tracks are only removed when ``remove_unlabelled`` is True
    (the UI asks for explicit confirmation first). At least one regular
    track of a kept language is always preserved when one exists.
    """
    plan = SubtitlePlan()
    langs = list(config.keep_languages)
    remove_duplicates = config.remove_duplicate_subs if remove_duplicates is None else remove_duplicates
    duplicates = set()
    if remove_duplicates:
        for group in duplicate_groups(subs, langs):
            duplicates.update(s.index for s in group[1:])

    for s in subs:
        lang = normalize_language(s.language)
        if lang is None:
            plan.unlabelled.append(s)
            if remove_unlabelled:
                plan.remove.append((s, "no language tag (removal confirmed)"))
            else:
                plan.keep.append((s, "no language tag - kept unless you confirm removal"))
        elif lang not in langs:
            plan.remove.append((s, f"{language_label(lang)} is not a kept language"))
        elif sub_is_forced(s):
            if config.keep_forced:
                plan.keep.append((s, "forced"))
            else:
                plan.remove.append((s, "forced subtitles not kept (preference)"))
        elif sub_is_sdh(s) and not config.keep_sdh:
            plan.remove.append((s, "SDH not kept (preference)"))
        elif s.index in duplicates:
            plan.remove.append((s, "duplicate of an earlier track in the same language and format"))
        else:
            plan.keep.append((s, "kept language"))

    # Never leave a kept language without a regular (non-forced) track.
    for lang in langs:
        regular_kept = [s for s, _ in plan.keep
                        if normalize_language(s.language) == lang and not sub_is_forced(s)]
        candidates = [(s, r) for s, r in plan.remove
                      if normalize_language(s.language) == lang and not sub_is_forced(s)]
        if not regular_kept and candidates:
            s, _ = candidates[0]
            plan.remove = [(t, r) for t, r in plan.remove if t.index != s.index]
            plan.keep.append((s, f"kept so at least one regular {language_label(lang)} track remains"))
    plan.keep.sort(key=lambda kr: kr[0].index)

    if subs and not any(normalize_language(s.language) in langs for s in subs):
        plan.notes.append("No subtitle track is in a kept language ("
                          + ", ".join(language_label(l) for l in langs) + ").")
    if plan.unlabelled and not remove_unlabelled:
        plan.notes.append(f"{len(plan.unlabelled)} track(s) have no language tag and are kept.")
    return plan


# --------------------------------------------------------------------------
# Rule registry
# --------------------------------------------------------------------------

@dataclass
class Rule:
    id: str
    category: str
    description: str
    device: Optional[str] = None
    check: Optional[Callable[[MediaReport, ScannerConfig], Iterable[Finding]]] = None


RULES: Dict[str, Rule] = {}


def rule(rule_id: str, category: str, description: str, device: Optional[str] = None):
    def register(func):
        RULES[rule_id] = Rule(rule_id, category, description, device, func)
        return func
    return register


def evaluate(report: MediaReport, config: Optional[ScannerConfig] = None) -> List[Finding]:
    config = config or ScannerConfig()
    findings: List[Finding] = []
    for rule_id, r in RULES.items():
        if not config.enabled(rule_id):
            continue
        if report.errors and rule_id != "file.unreadable":
            continue          # nothing else is known about an unreadable file
        for f in r.check(report, config) or ():
            f.device = r.device
            findings.append(f)
    findings.sort(key=lambda f: (f.device is not None, -SEVERITY_RANK[f.severity], f.category))
    return findings


def _track_name(a: AudioTrack) -> str:
    lang = f" [{a.language}]" if a.language else ""
    return f"audio #{a.index} {a.format_name} {a.channel_layout or (str(a.channels) + 'ch')}{lang}"


def _audio_evidence(a: AudioTrack) -> List[str]:
    lines = [f"ffprobe: codec {a.codec}, profile {a.profile or 'none'}, {a.channels} ch "
             f"({a.channel_layout or 'no layout'}), {a.sample_rate} Hz"
             + (f", {a.bit_depth}-bit" if a.bit_depth else "")]
    if a.title:
        lines.append(f"title: '{a.title}'")
    if a.mediainfo:
        lines.append("MediaInfo: " + ", ".join(f"{k}={v}" for k, v in a.mediainfo.items()))
    return lines


# ---- file -------------------------------------------------------------------

@rule("file.unreadable", CAT_FILE, "File could not be read by ffprobe")
def _file_unreadable(report, config):
    for error in report.errors:
        yield Finding("file.unreadable", ERROR, CAT_FILE, "File could not be analysed", error,
                      evidence=[error])


@rule("file.no_video", CAT_FILE, "File has no video track")
def _no_video(report, config):
    if not [v for v in report.video if not v.attached_pic]:
        yield Finding("file.no_video", WARNING, CAT_FILE, "No video track",
                      "ffprobe found no video stream in this file.")


@rule("file.not_mkv", CAT_FILE, "Non-MKV files are analysed only (repairs need MKV)")
def _not_mkv(report, config):
    if not report.is_mkv:
        yield Finding("file.not_mkv", INFO, CAT_FILE, "Analysis only (not MKV)",
                      "Repairs are only performed on MKV files.", evidence=[f"container: {report.container}"])


# ---- subtitles --------------------------------------------------------------

def _cleanup_action(report: MediaReport, config: ScannerConfig) -> List[str]:
    if not report.is_mkv or not report.subtitles:
        return []
    return [ACTION_CLEAN_SUBS] if subtitle_cleanup_plan(report.subtitles, config).remove else []


@rule("subs.excessive", CAT_SUBTITLES, "More subtitle tracks than the threshold")
def _subs_excessive(report, config):
    n = len(report.subtitles)
    if n > config.subtitle_threshold:
        pgs = sum(1 for s in report.subtitles if s.is_pgs)
        plan = subtitle_cleanup_plan(report.subtitles, config)
        yield Finding(
            "subs.excessive", WARNING, CAT_SUBTITLES, f"{n} subtitle tracks ({pgs} PGS)",
            f"More than {config.subtitle_threshold} subtitle tracks. Cleanup with your preferences would "
            f"keep {len(plan.keep)} and remove {len(plan.remove)}.",
            evidence=[sub_description(s) for s in report.subtitles],
            actions=_cleanup_action(report, config))


@rule("subs.non_english", CAT_SUBTITLES, "Subtitle tracks in languages you do not keep")
def _subs_other_languages(report, config):
    other = [s for s in report.subtitles
             if normalize_language(s.language) and normalize_language(s.language) not in config.keep_languages]
    if other:
        langs = sorted({language_label(s.language) for s in other})
        yield Finding("subs.non_english", INFO, CAT_SUBTITLES,
                      f"{len(other)} subtitle track(s) in other languages",
                      ", ".join(langs), evidence=[sub_description(s) for s in other],
                      actions=_cleanup_action(report, config))


@rule("subs.missing_language", CAT_SUBTITLES, "Subtitle tracks without a language tag")
def _subs_missing_language(report, config):
    missing = [s for s in report.subtitles if normalize_language(s.language) is None]
    if missing:
        yield Finding("subs.missing_language", WARNING, CAT_SUBTITLES,
                      f"{len(missing)} subtitle track(s) without a language tag",
                      "Players cannot pick these by language, and cleanup never removes them without "
                      "your explicit confirmation.",
                      evidence=[sub_description(s) for s in missing])


@rule("subs.ambiguous_language", CAT_SUBTITLES, "Subtitle title names a different language than its tag")
def _subs_ambiguous(report, config):
    for s in report.subtitles:
        named = title_language_conflict(s.language, s.title)
        if named:
            yield Finding("subs.ambiguous_language", WARNING, CAT_SUBTITLES,
                          f"Subtitle #{s.index}: tagged {language_label(s.language)}, title says "
                          f"{language_label(named)}",
                          "The language tag and the track title disagree; cleanup follows the tag.",
                          evidence=[sub_description(s)], stream_index=s.index)


@rule("subs.flags", CAT_SUBTITLES, "Forced and default subtitle tracks")
def _subs_flags(report, config):
    flagged = [s for s in report.subtitles if s.default or sub_is_forced(s)]
    if flagged:
        yield Finding("subs.flags", INFO, CAT_SUBTITLES,
                      f"{sum(1 for s in flagged if sub_is_forced(s))} forced, "
                      f"{sum(1 for s in flagged if s.default)} default subtitle track(s)",
                      evidence=[sub_description(s) for s in flagged])


@rule("subs.duplicate_english", CAT_SUBTITLES, "Several regular tracks of a kept language in the same format")
def _subs_duplicates(report, config):
    for group in duplicate_groups(report.subtitles, config.keep_languages):
        yield Finding("subs.duplicate_english", INFO, CAT_SUBTITLES,
                      f"{len(group)} regular {language_label(group[0].language)} "
                      f"{group[0].codec} tracks (possibly redundant)",
                      "Not removed unless 'remove duplicates' is chosen; the first is kept.",
                      evidence=[sub_description(s) for s in group])


@rule("ub450.subtitles", CAT_SUBTITLES, "Many PGS tracks slow DLNA start-up", device="ub450_dlna")
def _ub450_subs(report, config):
    pgs = [s for s in report.subtitles if s.is_pgs]
    if len(pgs) > config.subtitle_threshold:
        yield Finding("ub450.subtitles", WARNING, CAT_SUBTITLES,
                      f"Slow DLNA start likely ({len(pgs)} PGS tracks)",
                      "The UB450 took minutes to start Spectre with 46 PGS tracks and started almost "
                      "immediately after cleanup. Clean Subtitles is recommended.",
                      evidence=[f"{len(pgs)} PGS of {len(report.subtitles)} subtitle tracks"],
                      actions=_cleanup_action(report, config))


# ---- FLAC -------------------------------------------------------------------

def flac_target(a: AudioTrack) -> Tuple[Optional[str], List[str]]:
    problems = flac_source_problems(a.channel_layout, a.channels, a.sample_rate, a.bit_depth, a.sample_fmt)
    return (None if problems else FLAC_LAYOUTS.get(a.channel_layout or "")), problems


@rule("flac.track", CAT_FLAC, "FLAC audio tracks (details and TrueHD eligibility)")
def _flac(report, config):
    others = [a for a in report.audio if a.codec != "flac"]
    for a in report.audio:
        if a.codec != "flac":
            continue
        target, problems = flac_target(a)
        evidence = _audio_evidence(a)
        evidence.append(f"default: {'yes' if a.default else 'no'}; language: {language_label(a.language)}")
        evidence.append("other audio: " + ("; ".join(_track_name(o) for o in others) or "none"))
        if target:
            evidence.append(f"TrueHD conversion possible: {a.channel_layout} -> TrueHD "
                            f"{'2.0' if target == 'stereo' else target.replace('(side)', '')} "
                            "(encoder capability checked before converting)")
        else:
            evidence += [f"TrueHD conversion not offered: {p}" for p in problems]
        multichannel = (a.channels or 0) > 2
        yield Finding(
            "flac.track", INFO, CAT_FLAC,
            f"FLAC {a.channel_layout or a.channels} {f'{a.sample_rate / 1000:g}' if a.sample_rate else '?'} kHz"
            + (f" {a.bit_depth}-bit" if a.bit_depth else ""),
            ("Multichannel FLAC: some playback devices output it as stereo PCM. This is a potential device "
             "compatibility issue, not a fault in the file." if multichannel else
             "Stereo FLAC."),
            evidence=evidence, stream_index=a.index,
            actions=[ACTION_FLAC_TRUEHD] if target and report.is_mkv else [])


@rule("firetv.flac", CAT_FLAC, "Multichannel FLAC on Fire TV / Plex", device="firetv_plex")
def _firetv_flac(report, config):
    for a in report.audio:
        if a.codec == "flac" and (a.channels or 0) > 2:
            target, _ = flac_target(a)
            yield Finding(
                "firetv.flac", WARNING, CAT_FLAC, f"Multichannel FLAC ({a.channel_layout}) may play as stereo PCM",
                "Fire TV / Plex has been seen to output multichannel FLAC as 2-channel PCM. Adding a lossless "
                "TrueHD copy (FLAC kept) avoids this." if target else
                "Fire TV / Plex has been seen to output multichannel FLAC as 2-channel PCM.",
                evidence=_audio_evidence(a), stream_index=a.index,
                actions=[ACTION_FLAC_TRUEHD] if target and report.is_mkv else [])


# ---- Atmos ------------------------------------------------------------------

@rule("atmos.status", CAT_ATMOS, "Dolby Atmos signalling of E-AC-3 and TrueHD tracks")
def _atmos_status(report, config):
    for a in report.audio:
        an = a.atmos
        if an is None or a.codec == "ac3":
            continue
        severity = INFO
        if an.status in (STATUS_DDP_UNVERIFIED, STATUS_TRUEHD_UNVERIFIED) and an.title_claims_atmos:
            severity = WARNING
        detail = {
            STATUS_DDP_ATMOS: "JOC (Atmos) is signalled consistently. The file is valid; if a device "
                              "outputs PCM 5.1, that is a playback-chain limitation.",
            STATUS_DDP_CONVENTIONAL: "Ordinary Dolby Digital Plus; the bitstream does not signal JOC.",
            STATUS_DDP_NOT_DETECTED: "No Atmos signalling was found, but the bitstream could not be checked, "
                                     "so absence is not certain.",
            STATUS_DDP_UNVERIFIED: "The title claims Atmos but the JOC signalling could not be confirmed.",
            STATUS_TRUEHD_ATMOS: "The TrueHD stream signals the Atmos (16-channel) presentation.",
            STATUS_TRUEHD_NO_ATMOS: "Channel-based TrueHD; no Atmos presentation is signalled.",
            STATUS_TRUEHD_UNVERIFIED: "Atmos presence could not be determined.",
        }.get(an.status)
        if an.status == STATUS_INCONSISTENT:
            continue          # reported by atmos.inconsistent
        if detail is None:
            continue
        title = f"Audio #{a.index}: {an.status}"
        if an.object_count:
            title += f" (complexity index {an.object_count})"
        yield Finding("atmos.status", severity, CAT_ATMOS, title, detail,
                      evidence=an.evidence + [f"confidence: {an.confidence}"], stream_index=a.index)


@rule("atmos.inconsistent", CAT_ATMOS, "Atmos evidence from different tools disagrees")
def _atmos_inconsistent(report, config):
    for a in report.audio:
        if a.atmos and a.atmos.status == STATUS_INCONSISTENT:
            yield Finding("atmos.inconsistent", WARNING, CAT_ATMOS,
                          f"Audio #{a.index}: Atmos evidence conflicts - manual inspection recommended",
                          "; ".join(a.atmos.conflicts),
                          evidence=a.atmos.conflicts + a.atmos.evidence, stream_index=a.index)


@rule("atmos.misleading_title", CAT_ATMOS, "Track title claims Atmos the stream does not carry")
def _atmos_title(report, config):
    for a in report.audio:
        an = a.atmos
        if not an or not an.title_claims_atmos:
            continue
        if an.status == STATUS_AC3_MISLABELLED:
            yield Finding("atmos.misleading_title", WARNING, CAT_ATMOS,
                          f"Audio #{a.index}: AC-3 track labelled Atmos (incorrect or misleading metadata)",
                          "Dolby Digital (AC-3) cannot carry Atmos. The audio itself is fine; only the "
                          "title is misleading. The app does not rename tracks.",
                          evidence=an.evidence, stream_index=a.index)
        elif an.status in (STATUS_DDP_CONVENTIONAL, STATUS_TRUEHD_NO_ATMOS):
            yield Finding("atmos.misleading_title", WARNING, CAT_ATMOS,
                          f"Audio #{a.index}: title claims Atmos but no Atmos is signalled",
                          "The bitstream was checked and does not signal Atmos. Expect normal "
                          f"{'5.1/7.1 ' if a.channels else ''}output, not Atmos.",
                          evidence=an.evidence, stream_index=a.index)


@rule("atmos.codec_mismatch", CAT_ATMOS, "Track tagged AC-3 contains E-AC-3 data")
def _codec_mismatch(report, config):
    for a in report.audio:
        if a.codec == "ac3" and a.bitstream and a.bitstream.get("eac3"):
            yield Finding("atmos.codec_mismatch", ERROR, CAT_ATMOS,
                          f"Audio #{a.index}: tagged AC-3 but contains E-AC-3 frames",
                          "The container's codec ID does not match the stream. Players may decode only "
                          "part of it. Remuxing with MKVToolNix (which re-detects the codec) is the "
                          "usual fix; this app does not change codec IDs automatically.",
                          evidence=[f"codec id: {a.codec_id or 'unknown'}", f"bitstream: {a.bitstream}"],
                          stream_index=a.index)


@rule("firetv.eac3_joc", CAT_ATMOS, "E-AC-3 JOC may be output as PCM by Fire TV", device="firetv_plex")
def _firetv_joc(report, config):
    for a in report.audio:
        if a.codec == "eac3" and a.atmos and a.atmos.status == STATUS_DDP_ATMOS:
            others = [o for o in report.audio if o.atmos and o.atmos.status == STATUS_TRUEHD_ATMOS]
            yield Finding(
                "firetv.eac3_joc", WARNING, CAT_ATMOS,
                f"Valid Atmos - potential Fire TV playback compatibility issue (audio #{a.index})",
                "The DD+ Atmos stream is valid. Fire TV / Plex sometimes decodes E-AC-3 JOC to 5.1 PCM "
                "instead of passing it through, so the AVR shows Multi Ch In / PCM. The file is not "
                "defective; this cannot be repaired by re-encoding without losing Atmos. Check the "
                "Fire TV audio output setting (Dolby Digital Plus / Best Available) and Plex passthrough."
                + (" A TrueHD Atmos track is also present - select it in Plex." if others else ""),
                evidence=a.atmos.evidence, stream_index=a.index)


# ---- DTS --------------------------------------------------------------------

def _is_dts_hd_ma(a: AudioTrack) -> bool:
    return a.codec == "dts" and (a.profile or "").startswith("DTS-HD MA")


@rule("dts.track", CAT_DTS, "DTS / DTS-HD MA / DTS:X tracks")
def _dts(report, config):
    for a in report.audio:
        if a.codec == "dts":
            yield Finding("dts.track", INFO, CAT_DTS, f"Audio #{a.index}: {a.format_name} {a.channel_layout}",
                          "DTS:X object metadata is not identifiable beyond the profile string."
                          if "DTS:X" in (a.profile or "") else "",
                          evidence=_audio_evidence(a), stream_index=a.index,
                          actions=[ACTION_DTS_CONVERT] if report.is_mkv else [])


@rule("firetv.dts_core", CAT_DTS, "DTS-HD MA may play as the lossy DTS core on Fire TV", device="firetv_plex")
def _firetv_dts(report, config):
    for a in report.audio:
        if _is_dts_hd_ma(a):
            has_alt = any(o.codec in ("truehd", "eac3", "ac3", "flac", "pcm_s16le", "pcm_s24le")
                          for o in report.audio)
            yield Finding(
                "firetv.dts_core", WARNING, CAT_DTS,
                f"{a.format_name} may play as DTS core only (audio #{a.index})",
                "Some Fire TV / Plex set-ups pass through only the lossy DTS core. The existing DTS2DD "
                "conversion can add a lossless TrueHD (5.1/7.1) or an E-AC-3 copy."
                + (" Another Dolby/PCM track is already present." if has_alt else ""),
                evidence=_audio_evidence(a), stream_index=a.index,
                actions=[ACTION_DTS_CONVERT] if report.is_mkv else [])


# ---- Dolby Vision / HDR -------------------------------------------------------

def _dv_evidence(v: VideoTrack) -> List[str]:
    lines = [f"ffprobe: DOVI configuration record profile {v.dv_profile}, level {v.dv_level}, "
             f"BL compatibility id {v.dv_compat_id}, RPU {v.dv_rpu}, EL {v.dv_el}, BL {v.dv_bl}",
             f"base layer: {v.codec} {v.color_transfer or 'unknown transfer'} {v.color_primaries or ''}"]
    if v.mediainfo_hdr:
        lines.append(f"MediaInfo: {v.mediainfo_hdr}")
    return lines


@rule("hdr.format", CAT_DV, "Video HDR format (Dolby Vision / HDR10 / HDR10+ / HLG)")
def _hdr(report, config):
    for v in report.video:
        if v.attached_pic or not v.hdr_formats:
            continue
        evidence = [f"ffprobe: transfer {v.color_transfer}, primaries {v.color_primaries}, {v.pix_fmt}"]
        if v.dv_profile is not None:
            evidence = _dv_evidence(v)
        if v.hdr10plus_checked:
            evidence.append("decoded frames checked for HDR10+ dynamic metadata: "
                            + ("found" if "HDR10+" in v.hdr_formats else "none"))
        if v.mastering_metadata is not None:
            evidence.append(f"mastering display metadata: {'present' if v.mastering_metadata else 'absent'}")
        yield Finding("hdr.format", INFO, CAT_DV, f"Video: {v.label}", evidence=evidence, stream_index=v.index)


@rule("dv.no_fallback", CAT_DV, "Dolby Vision profile 5 has no HDR10/SDR fallback")
def _dv_p5(report, config):
    for v in report.video:
        if v.dv_profile == 5:
            yield Finding("dv.no_fallback", WARNING, CAT_DV, "Dolby Vision profile 5 - no HDR10 fallback",
                          "Devices that cannot decode Dolby Vision show profile 5 with wrong (green/purple) "
                          "colours. This is a property of the stream, not corruption.",
                          evidence=_dv_evidence(v), stream_index=v.index)


@rule("dv.rpu_missing", CAT_DV, "Dolby Vision signalled without RPU metadata")
def _dv_rpu(report, config):
    for v in report.video:
        if v.dv_profile is not None and v.dv_rpu is False:
            yield Finding("dv.rpu_missing", ERROR, CAT_DV, "Dolby Vision configured but no RPU present",
                          "The configuration record says no RPU (dynamic metadata) is present, so Dolby "
                          "Vision cannot be displayed.", evidence=_dv_evidence(v), stream_index=v.index)


@rule("dv.unusual_compat", CAT_DV, "Unusual Dolby Vision profile / compatibility combination")
def _dv_compat(report, config):
    valid = {5: {0}, 7: {6}, 8: {1, 2, 4}, 4: {2}, 9: {2}, 10: {0, 1, 2, 4}}
    for v in report.video:
        if v.dv_profile is None or v.dv_compat_id is None:
            continue
        allowed = valid.get(v.dv_profile)
        if allowed is not None and v.dv_compat_id not in allowed:
            yield Finding("dv.unusual_compat", WARNING, CAT_DV,
                          f"Dolby Vision profile {v.dv_profile} with compatibility id {v.dv_compat_id}",
                          "This combination is not a standard Dolby Vision configuration; devices may "
                          "ignore Dolby Vision or misinterpret the base layer.",
                          evidence=_dv_evidence(v), stream_index=v.index)


@rule("dv.profile7", CAT_DV, "Dolby Vision profile 7 (dual layer) in a file")
def _dv_p7(report, config):
    for v in report.video:
        if v.dv_profile == 7:
            yield Finding("dv.profile7", INFO, CAT_DV, "Dolby Vision profile 7 (Blu-ray dual layer)",
                          "Most streaming players use only the HDR10 base layer of profile 7 (FEL/MEL is "
                          "ignored). Expect HDR10 playback; this is a fallback, not corruption.",
                          evidence=_dv_evidence(v), stream_index=v.index)


@rule("ub450.dolby_vision", CAT_DV, "Dolby Vision in MKV over DLNA", device="ub450_dlna")
def _ub450_dv(report, config):
    for v in report.video:
        if v.dv_profile is not None and report.is_mkv:
            p5 = v.dv_profile == 5
            yield Finding("ub450.dolby_vision", WARNING if p5 else INFO, CAT_DV,
                          f"Dolby Vision P{v.dv_profile} in MKV over DLNA",
                          ("Profile 5 has no HDR10 base layer: if the player does not render Dolby Vision "
                           "from this MKV, colours will be wrong." if p5 else
                           "Panasonic players may not render Dolby Vision from MKV files over DLNA; with "
                           "this profile they should fall back to the HDR10 base layer."),
                          evidence=_dv_evidence(v), stream_index=v.index)


@rule("lgc5.dolby_vision", CAT_DV, "Dolby Vision profile/container on LG C5 Plex", device="lgc5_plex")
def _lg_dv(report, config):
    for v in report.video:
        if v.dv_profile is None:
            continue
        if v.dv_profile == 7:
            yield Finding("lgc5.dolby_vision", WARNING, CAT_DV, "Dolby Vision P7 - expect HDR10 on LG C5 Plex",
                          "Plex on LG TVs is not expected to apply the profile 7 enhancement layer / RPU from "
                          "MKV; the TV will most likely show the HDR10 base layer.",
                          evidence=_dv_evidence(v), stream_index=v.index)
        elif report.is_mkv:
            yield Finding("lgc5.dolby_vision", INFO, CAT_DV, f"Dolby Vision P{v.dv_profile} in MKV",
                          "Dolby Vision from MKV depends on the Plex app / webOS version. If the TV shows "
                          "HDR10 instead of Dolby Vision, the file is still valid.",
                          evidence=_dv_evidence(v), stream_index=v.index)


@rule("lgc5.lossless_audio", CAT_METADATA, "Lossless audio passthrough from the TV", device="lgc5_plex")
def _lg_audio(report, config):
    lossless = [a for a in report.audio
                if a.codec in ("truehd", "flac") or a.codec.startswith("pcm_") or _is_dts_hd_ma(a)]
    if lossless and not any(a.codec in ("eac3", "ac3") for a in report.audio):
        yield Finding("lgc5.lossless_audio", INFO, CAT_METADATA, "Lossless audio only - needs eARC passthrough",
                      "From the TV's own Plex app, lossless audio reaches an AV receiver intact only via eARC "
                      "with passthrough enabled; otherwise the TV may transcode or downmix. There is no "
                      "Dolby Digital / DD+ fallback track in this file.",
                      evidence=[_track_name(a) for a in lossless])


@rule("ub450.audio", CAT_METADATA, "Audio formats hardware players rarely support", device="ub450_dlna")
def _ub450_audio(report, config):
    for a in report.audio:
        if a.codec in ("opus", "vorbis"):
            yield Finding("ub450.audio", WARNING, CAT_METADATA,
                          f"Audio #{a.index}: {a.codec.title()} may be unsupported",
                          "Blu-ray player media engines usually do not decode this codec; the film may play "
                          "without sound.", evidence=_audio_evidence(a), stream_index=a.index)


@rule("ub450.truehd_mkv", CAT_ATMOS, "Dolby TrueHD in MKV over DLNA (M2TS remux test)", device="ub450_dlna")
def _ub450_truehd(report, config):
    """The UB450 played the AC-3 fallback but gave no sound from TrueHD Atmos in MKV over DLNA.

    E-AC-3 JOC plays on this player, so it is deliberately not flagged.
    """
    if not report.is_mkv:
        return
    for a in report.audio:
        if a.codec != "truehd":
            continue
        atmos = bool(a.atmos and a.atmos.atmos)
        detail = UB450_TRUEHD_MESSAGE
        video = next((v for v in report.video if not v.attached_pic), None)
        if video is not None and (video.dv_profile is not None or "HDR10+" in video.hdr_formats):
            kinds = " and ".join(k for k, on in (("Dolby Vision", video.dv_profile is not None),
                                                ("HDR10+", "HDR10+" in video.hdr_formats)) if on)
            detail += (f" This film has {kinds}: M2TS remuxing may not preserve all of that video metadata; "
                       "the remux test checks and reports it.")
        fallback = [o for o in report.audio if o.codec in ("ac3", "eac3")]
        evidence = _audio_evidence(a) + ([f"Atmos: {a.atmos.status}"] if a.atmos else [])
        evidence.append("fallback track(s): " + (", ".join(_track_name(o) for o in fallback) or "none"))
        yield Finding("ub450.truehd_mkv", WARNING, CAT_ATMOS,
                      f"Audio #{a.index}: TrueHD{' Atmos' if atmos else ''} in MKV - possibly no sound over DLNA",
                      detail, evidence=evidence, stream_index=a.index, actions=[ACTION_M2TS])


# ---- metadata ---------------------------------------------------------------

@rule("meta.default_audio", CAT_METADATA, "Missing or multiple default audio tracks")
def _default_audio(report, config):
    defaults = [a for a in report.audio if a.default]
    fixable = [ACTION_FIX_DEFAULT] if report.is_mkv else []
    if len(report.audio) > 1 and not defaults:
        yield Finding("meta.default_audio", WARNING, CAT_METADATA, "No default audio track",
                      "Players choose a track themselves; the choice may differ between devices.",
                      evidence=[_track_name(a) for a in report.audio], actions=fixable)
    elif len(defaults) > 1:
        yield Finding("meta.default_audio", WARNING, CAT_METADATA,
                      f"{len(defaults)} audio tracks flagged default",
                      "Players resolve this differently; usually the first one wins.",
                      evidence=[_track_name(a) + " {default}" for a in defaults], actions=fixable)


@rule("meta.audio_language", CAT_METADATA, "Audio tracks with missing or contradicting language")
def _audio_language(report, config):
    for a in report.audio:
        if normalize_language(a.language) is None and len(report.audio) > 1:
            yield Finding("meta.audio_language", WARNING, CAT_METADATA,
                          f"Audio #{a.index} has no language tag",
                          "Players cannot select it by language preference.",
                          evidence=_audio_evidence(a), stream_index=a.index)
        named = title_language_conflict(a.language, a.title)
        if named:
            yield Finding("meta.audio_language", WARNING, CAT_METADATA,
                          f"Audio #{a.index}: tagged {language_label(a.language)}, title says {language_label(named)}",
                          evidence=_audio_evidence(a), stream_index=a.index)


_CODEC_WORDS = {
    "truehd": ("truehd", "true hd"),
    "eac3": ("dd+", "ddp", "e-ac-3", "eac3", "e-ac3", "dolby digital plus"),
    "dts": ("dts",),
    "flac": ("flac",),
    "ac3": ("ac-3", "ac3", "dolby digital"),
    "aac": ("aac",),
    "pcm": ("lpcm", "pcm"),
}


def _codec_family(codec: str) -> str:
    return "pcm" if codec.startswith("pcm_") else codec


@rule("meta.title_mismatch", CAT_METADATA, "Track title contradicts the detected codec or channels")
def _title_mismatch(report, config):
    for a in report.audio:
        title = (a.title or "").lower()
        if not title:
            continue
        actual = _codec_family(a.codec)
        claimed = []
        for codec, words in _CODEC_WORDS.items():
            hits = [w for w in words if re.search(rf"(?<![a-z0-9+]){re.escape(w)}(?![a-z0-9+])", title)]
            if hits:
                claimed.append(codec)
        # "Dolby Digital Plus" also matches "dolby digital"; DD+ wins.
        if "eac3" in claimed and "ac3" in claimed:
            claimed.remove("ac3")
        if claimed and actual not in claimed:
            yield Finding("meta.title_mismatch", WARNING, CAT_METADATA,
                          f"Audio #{a.index}: title '{a.title}' but codec is {a.format_name}",
                          evidence=_audio_evidence(a), stream_index=a.index)
        m = re.search(r"\b([1-7])[.,]([01])\b", title)
        if m and a.channels:
            expected = int(m.group(1)) + int(m.group(2))
            if expected != a.channels and not (a.atmos and a.atmos.atmos):
                yield Finding("meta.title_mismatch", WARNING, CAT_METADATA,
                              f"Audio #{a.index}: title says {m.group(0)} but the stream has {a.channels} channels",
                              evidence=_audio_evidence(a), stream_index=a.index)


_LAYOUT_CHANNELS = {"mono": 1, "stereo": 2, "2.1": 3, "3.0": 3, "quad": 4, "4.0": 4, "5.0": 5, "5.0(side)": 5,
                    "5.1": 6, "5.1(side)": 6, "6.1": 7, "7.1": 8, "7.1(wide)": 8, "7.1(wide-side)": 8}


@rule("meta.layout", CAT_METADATA, "Unexpected or malformed channel information")
def _layout(report, config):
    for a in report.audio:
        if not a.channels or not a.sample_rate:
            yield Finding("meta.layout", ERROR, CAT_METADATA,
                          f"Audio #{a.index}: channel count or sample rate missing",
                          "ffprobe could not read basic stream parameters; the track may be damaged.",
                          evidence=_audio_evidence(a), stream_index=a.index)
            continue
        expected = _LAYOUT_CHANNELS.get(a.channel_layout or "")
        if expected is not None and expected != a.channels:
            yield Finding("meta.layout", WARNING, CAT_METADATA,
                          f"Audio #{a.index}: layout {a.channel_layout} but {a.channels} channels",
                          evidence=_audio_evidence(a), stream_index=a.index)
        elif a.channels > 2 and not a.channel_layout:
            yield Finding("meta.layout", INFO, CAT_METADATA,
                          f"Audio #{a.index}: {a.channels} channels without a channel layout",
                          "Speaker positions are not declared; players assume a default order.",
                          evidence=_audio_evidence(a), stream_index=a.index)
        elif a.channel_layout in ("7.1(wide)", "7.1(wide-side)"):
            yield Finding("meta.layout", INFO, CAT_METADATA,
                          f"Audio #{a.index}: unusual 7.1 layout {a.channel_layout}",
                          "Front-centre speakers instead of rear surrounds; often a mislabelled 7.1.",
                          evidence=_audio_evidence(a), stream_index=a.index)


@rule("meta.duplicate_audio", CAT_METADATA, "Audio tracks that look identical")
def _dup_audio(report, config):
    seen: Dict[tuple, AudioTrack] = {}
    for a in report.audio:
        key = (a.codec, a.profile, a.channels, a.sample_rate, a.bit_rate,
               normalize_language(a.language), (a.title or "").strip().lower())
        if key in seen:
            first = seen[key]
            yield Finding("meta.duplicate_audio", INFO, CAT_METADATA,
                          f"Audio #{a.index} looks like a duplicate of #{first.index}",
                          "Same codec, channels, bitrate, language and title; one may be redundant "
                          "(e.g. a commentary without a title).",
                          evidence=[_track_name(first), _track_name(a)], stream_index=a.index)
        else:
            seen[key] = a


# --------------------------------------------------------------------------
# Summaries for the results table
# --------------------------------------------------------------------------

def worst_severity(findings: Sequence[Finding]) -> Optional[str]:
    worst = max(findings, key=lambda f: SEVERITY_RANK[f.severity], default=None)
    return worst.severity if worst else None


def issue_findings(findings: Sequence[Finding]) -> List[Finding]:
    return [f for f in findings if SEVERITY_RANK[f.severity] >= SEVERITY_RANK[WARNING]]


def recommended_actions(findings: Sequence[Finding]) -> List[str]:
    """Actions attached to warning/error findings (in a stable order)."""
    wanted = {a for f in issue_findings(findings) for a in f.actions}
    return [a for a in ACTION_LABELS if a in wanted]


def available_actions(findings: Sequence[Finding]) -> List[str]:
    wanted = {a for f in findings for a in f.actions}
    return [a for a in ACTION_LABELS if a in wanted]
