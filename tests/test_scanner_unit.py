"""Library Scanner unit tests: mocked metadata and synthetic bitstreams only."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sets sys.path)

import atmos_inspector as ai
import compatibility_rules as cr
import library_repair as lr
import media_inspector as mi
import truehd_encoder as te
import truehd_workflow as tw
from library_scanner import LibraryScanner, ScanCache, ScanItem, find_media_files

LANGS = ["eng", "fre", "ger", "spa", "ita", "dut", "swe", "nor", "dan", "fin", "pol", "cze", "hun", "rus",
         "gre", "tur", "ara", "heb", "hin", "jpn", "kor", "chi", "tha", "por", "rum", "bul", "hrv", "slv",
         "slo", "ice", "est", "lav", "lit", "cat", "baq", "glg", "tam", "tel", "ukr", "vie", "ind", "may",
         "per", "srp", "fil"]


def stream(index, kind, codec, **kw):
    s = {"index": index, "codec_type": kind, "codec_name": codec, "tags": {}, "disposition": {}}
    for key in ("language", "title"):
        if key in kw:
            s["tags"][key] = kw.pop(key)
    for key in ("default", "forced", "hearing_impaired", "comment"):
        if kw.pop(key, False):
            s["disposition"][key] = 1
    s.update(kw)
    return s


def audio(index, codec, channels=6, layout="5.1(side)", rate=48000, **kw):
    return stream(index, "audio", codec, channels=channels, channel_layout=layout, sample_rate=str(rate), **kw)


def video(index=0, **kw):
    return stream(index, "video", kw.pop("codec", "hevc"), width=3840, height=2160, pix_fmt="yuv420p10le", **kw)


def make_report(streams, path="/films/Film.mkv", chapters=0):
    probe = {"format": {"format_name": "matroska,webm", "duration": "7200"}, "streams": streams,
             "chapters": [{}] * chapters}
    return mi.report_from_probe(path, probe, size=1000, mtime_ns=1)


def findings(report, config=None, rule=None):
    result = cr.evaluate(report, config or cr.ScannerConfig())
    return [f for f in result if rule is None or f.rule_id == rule]


def spectre_like():
    """46 PGS subtitle tracks like the tested Spectre (2015) remux."""
    streams = [video(), audio(1, "truehd", 8, "7.1", language="eng", default=True)]
    for i, lang in enumerate(LANGS[:45]):
        streams.append(stream(2 + i, "subtitle", "hdmv_pgs_subtitle", language=lang))
    streams.append(stream(47, "subtitle", "hdmv_pgs_subtitle", language="eng", forced=True, title="Forced"))
    return make_report(streams)


# --------------------------------------------------------------------------
# Subtitles
# --------------------------------------------------------------------------

class SubtitleRuleTests(unittest.TestCase):
    def test_dozens_of_pgs_tracks(self):
        report = spectre_like()
        self.assertEqual(len(report.subtitles), 46)
        excessive = findings(report, rule="subs.excessive")[0]
        self.assertEqual(excessive.severity, cr.WARNING)
        self.assertIn("46 subtitle tracks (46 PGS)", excessive.title)
        self.assertIn(cr.ACTION_CLEAN_SUBS, excessive.actions)
        ub = findings(report, rule="ub450.subtitles")[0]
        self.assertEqual(ub.device, "ub450_dlna")
        plan = cr.subtitle_cleanup_plan(report.subtitles, cr.ScannerConfig())
        kept = [s.index for s, _ in plan.keep]
        self.assertEqual(kept, [2, 47])          # regular English + forced English
        self.assertEqual(len(plan.remove), 44)

    def test_threshold_is_editable(self):
        report = make_report([video()] + [stream(i, "subtitle", "subrip", language="eng") for i in range(1, 6)])
        self.assertFalse(findings(report, rule="subs.excessive"))              # 5 is not > 5
        self.assertTrue(findings(report, cr.ScannerConfig(subtitle_threshold=4), "subs.excessive"))

    def test_forced_and_sdh_preserved(self):
        subs = make_report([video(),
                            stream(1, "subtitle", "hdmv_pgs_subtitle", language="eng"),
                            stream(2, "subtitle", "hdmv_pgs_subtitle", language="eng", forced=True),
                            stream(3, "subtitle", "hdmv_pgs_subtitle", language="eng", title="English SDH"),
                            stream(4, "subtitle", "hdmv_pgs_subtitle", language="en", hearing_impaired=True),
                            stream(5, "subtitle", "hdmv_pgs_subtitle", language="fre", forced=True),
                            stream(6, "subtitle", "subrip", language="spa")]).subtitles
        plan = cr.subtitle_cleanup_plan(subs, cr.ScannerConfig())
        self.assertEqual([s.index for s, _ in plan.keep], [1, 2, 3, 4])
        self.assertEqual(plan.remove_indices, [5, 6])     # forced French is not English

    def test_one_regular_english_always_kept(self):
        subs = make_report([video(), stream(1, "subtitle", "subrip", language="eng", hearing_impaired=True),
                            stream(2, "subtitle", "subrip", language="eng", forced=True)]).subtitles
        plan = cr.subtitle_cleanup_plan(subs, cr.ScannerConfig(keep_sdh=False))
        self.assertEqual([s.index for s, _ in plan.keep], [1, 2])
        self.assertIn("at least one regular", dict((s.index, r) for s, r in plan.keep)[1])

    def test_unlabelled_never_removed_without_confirmation(self):
        subs = make_report([video(), stream(1, "subtitle", "subrip"),
                            stream(2, "subtitle", "subrip", language="und"),
                            stream(3, "subtitle", "subrip", language="ger")]).subtitles
        plan = cr.subtitle_cleanup_plan(subs, cr.ScannerConfig())
        self.assertEqual(plan.remove_indices, [3])
        self.assertEqual([s.index for s in plan.unlabelled], [1, 2])
        confirmed = cr.subtitle_cleanup_plan(subs, cr.ScannerConfig(), remove_unlabelled=True)
        self.assertEqual(confirmed.remove_indices, [1, 2, 3])
        self.assertEqual([s.index for s in confirmed.unlabelled_removed], [1, 2])

    def test_language_tags_and_titles(self):
        report = make_report([video(), stream(1, "subtitle", "subrip"),
                              stream(2, "subtitle", "subrip", language="eng", title="French"),
                              stream(3, "subtitle", "subrip", language="en-GB", title="English (UK)")])
        self.assertEqual(len(findings(report, rule="subs.missing_language")), 1)
        ambiguous = findings(report, rule="subs.ambiguous_language")
        self.assertEqual([f.stream_index for f in ambiguous], [2])

    def test_duplicate_english_reported_and_optional_removal(self):
        report = make_report([video()] + [stream(i, "subtitle", "hdmv_pgs_subtitle", language="eng")
                                          for i in (1, 2, 3)]
                             + [stream(4, "subtitle", "subrip", language="eng")])
        dup = findings(report, rule="subs.duplicate_english")
        self.assertEqual(len(dup), 1)
        self.assertIn("3 regular English", dup[0].title)
        self.assertEqual(cr.subtitle_cleanup_plan(report.subtitles, cr.ScannerConfig()).remove_indices, [])
        plan = cr.subtitle_cleanup_plan(report.subtitles, cr.ScannerConfig(remove_duplicate_subs=True))
        self.assertEqual(plan.remove_indices, [2, 3])

    def test_other_keep_languages(self):
        subs = make_report([video(), stream(1, "subtitle", "subrip", language="eng"),
                            stream(2, "subtitle", "subrip", language="fra")]).subtitles
        plan = cr.subtitle_cleanup_plan(subs, cr.ScannerConfig(keep_languages=["eng", "fre"]))
        self.assertEqual(plan.remove_indices, [])

    def test_language_normalisation(self):
        for code, expect in (("en", "eng"), ("en-US", "eng"), ("English", "eng"), ("fra", "fre"),
                             ("zho", "chi"), ("und", None), ("", None), (None, None), ("mul", None)):
            self.assertEqual(cr.normalize_language(code), expect, code)


# --------------------------------------------------------------------------
# FLAC
# --------------------------------------------------------------------------

def flac(index, channels, layout, bits=24, rate=48000, **kw):
    fmt = "s32" if bits > 16 else "s16"
    return audio(index, "flac", channels, layout, rate, sample_fmt=fmt, bits_per_raw_sample=str(bits), **kw)


def ready_encoder(layouts=("5.1(side)", "7.1", "stereo")):
    report = te.EncoderReport(path="/x/ffmpeg", status=te.STATUS_READY)
    for layout in te.TESTED_LAYOUTS:
        report.tests.append(te.LayoutTest(layout, 24, layout in layouts, ""))
    return report


class FlacTests(unittest.TestCase):
    def test_flac_layouts_and_targets(self):
        cases = [(2, "stereo", "stereo"), (6, "5.1", "5.1(side)"), (6, "5.1(side)", "5.1(side)"), (8, "7.1", "7.1")]
        for channels, layout, target in cases:
            a = make_report([video(), flac(1, channels, layout)]).audio[0]
            self.assertEqual(cr.flac_target(a)[0], target, layout)
            self.assertEqual(tw.truehd_target_layout(lr.stream_info(a)), target)
            self.assertEqual(tw.validate_source(lr.stream_info(a), ready_encoder()), [])

    def test_unsupported_flac_refused(self):
        for channels, layout, bits, rate, words in ((8, "7.1(wide)", 24, 48000, "not supported"),
                                                    (1, "mono", 16, 48000, "not supported"),
                                                    (7, "6.1", 24, 48000, "not supported"),
                                                    (6, "5.1", 32, 48000, "24-bit maximum"),
                                                    (8, "7.1", 24, 192000, "not a valid Dolby TrueHD")):
            a = make_report([video(), flac(1, channels, layout, bits, rate)]).audio[0]
            target, problems = cr.flac_target(a)
            self.assertIsNone(target, layout)
            self.assertIn(words, " ".join(problems))

    def test_encoder_capability_checked(self):
        a = make_report([video(), flac(1, 8, "7.1")]).audio[0]
        problems = tw.validate_source(lr.stream_info(a), ready_encoder(("5.1(side)",)))
        self.assertIn("has not passed the 7.1 self-test", problems[0])
        self.assertTrue(tw.validate_source(lr.stream_info(a), None))

    def test_flac_findings(self):
        report = make_report([video(), audio(1, "ac3", default=True, language="eng"),
                              flac(2, 6, "5.1", language="eng")])
        info = findings(report, rule="flac.track")[0]
        self.assertEqual(info.severity, cr.INFO)
        self.assertIn("not a fault", info.detail)
        self.assertIn(cr.ACTION_FLAC_TRUEHD, info.actions)
        self.assertTrue(any("other audio: audio #1" in e for e in info.evidence))
        fire = findings(report, rule="firetv.flac")[0]
        self.assertEqual((fire.severity, fire.device), (cr.WARNING, "firetv_plex"))

    def test_relabel_is_positional_and_planar(self):
        a = lr.stream_info(make_report([video(), flac(1, 6, "5.1", bits=16)]).audio[0])
        self.assertEqual(tw.encoder_sample_format(a), "s16p")
        job = te.TrueHDJob("/in.mkv", "/out.mkv", 1, 1, "5.1(side)", "s16p", 48000, source_layout="5.1")
        cmd = te.build_truehd_command("/x/ffmpeg", job)
        self.assertIn("channelmap=channel_layout=5.1(side)", cmd)
        self.assertEqual(cmd[cmd.index("-ch_layout:a:1") + 1], "5.1(side)")
        same = te.TrueHDJob("/in.mkv", "/out.mkv", 1, 1, "7.1", "s32p", 48000, source_layout="7.1")
        self.assertFalse(any("channelmap" in c for c in te.build_truehd_command("/x/ffmpeg", same)))

    def test_flac_warnings_say_not_atmos(self):
        a = lr.stream_info(make_report([video(), flac(1, 6, "5.1")]).audio[0])
        text = " ".join(tw.source_warnings(a))
        self.assertIn("does not create Dolby Atmos", text)
        self.assertIn("surround", text)


# --------------------------------------------------------------------------
# Bitstreams and Atmos classification
# --------------------------------------------------------------------------

class BitWriter:
    def __init__(self):
        self.bits = ""

    def put(self, value, n):
        self.bits += format(value, f"0{n}b") if n else ""
        return self

    def bytes(self, size=None):
        bits = self.bits + "0" * (-len(self.bits) % 8)
        data = int(bits, 2).to_bytes(len(bits) // 8, "big")
        return data.ljust(size or len(data), b"\0")


def eac3_frame(joc=False, complexity=16, acmod=7, lfe=1, strmtyp=0, substream=0, words=96):
    """Minimal E-AC-3 syncframe header (bsi only; no audio blocks)."""
    w = BitWriter().put(0x0B77, 16).put(strmtyp, 2).put(substream, 3).put(words - 1, 11)
    w.put(0, 2).put(3, 2).put(acmod, 3).put(lfe, 1).put(16, 5)     # 48 kHz, 6 blocks, bsid 16
    w.put(31, 5).put(0, 1)                                           # dialnorm, compre
    if strmtyp == 1:
        w.put(0, 1)                                                  # chanmape
    w.put(0, 1).put(0, 1)                                            # mixmdate, infomdate
    if joc:
        w.put(1, 1).put(1, 6).put(1, 8).put(complexity, 8)           # addbsi: JOC + complexity
    else:
        w.put(0, 1)
    return w.bytes(words * 2)


def truehd_sync(atmos, substreams=None):
    substreams = substreams if substreams is not None else (4 if atmos else 2)
    data = bytearray(32)
    data[0:4] = ai.TRUEHD_SYNC
    data[4] = 0x00                       # 48 kHz
    data[8:10] = ai.TRUEHD_SIGNATURE
    data[16] = substreams << 4
    data[17] = 0x80 if atmos else 0x3C
    return bytes(data)


class AtmosParserTests(unittest.TestCase):
    def test_eac3_without_joc(self):
        s = ai.summarize_ac3_bitstream(eac3_frame() * 60)
        self.assertEqual((s.frames, s.joc_frames, s.channel_mode), (60, 0, "3/2 + LFE"))
        a = ai.analyse_eac3(None, "English 5.1", s)
        self.assertEqual((a.status, a.atmos), (ai.STATUS_DDP_CONVENTIONAL, False))

    def test_eac3_with_confirmed_joc(self):
        s = ai.summarize_ac3_bitstream(eac3_frame(joc=True, complexity=16) * 60)
        self.assertEqual((s.joc_frames, s.complexity), (60, [16]))
        a = ai.analyse_eac3("Dolby Digital Plus + Dolby Atmos", "DD+ Atmos", s,
                            {"Format": "E-AC-3 JOC", "Format_AdditionalFeatures": "JOC"})
        self.assertEqual((a.status, a.atmos, a.confidence, a.object_count),
                         (ai.STATUS_DDP_ATMOS, True, "high", 16))

    def test_dependent_substream_counted(self):
        s = ai.summarize_ac3_bitstream((eac3_frame(joc=True) + eac3_frame(strmtyp=1, acmod=2, lfe=0)) * 40)
        self.assertEqual((s.frames, s.joc_frames, s.dependent_substreams), (40, 40, 1))

    def test_labelled_atmos_but_no_joc(self):
        s = ai.summarize_ac3_bitstream(eac3_frame() * 60)
        a = ai.analyse_eac3(None, "TrueHD Atmos 7.1 / DD+ Atmos", s)
        self.assertEqual(a.status, ai.STATUS_DDP_CONVENTIONAL)
        self.assertTrue(a.title_claims_atmos)
        # Without a bitstream check the claim stays unverified (not "absent").
        b = ai.analyse_eac3(None, "Atmos", None)
        self.assertEqual((b.status, b.atmos), (ai.STATUS_DDP_UNVERIFIED, None))

    def test_too_few_frames_is_not_absence(self):
        s = ai.summarize_ac3_bitstream(eac3_frame() * 5)
        self.assertEqual(ai.analyse_eac3(None, None, s).status, ai.STATUS_DDP_NOT_DETECTED)

    def test_conflicting_evidence(self):
        s = ai.summarize_ac3_bitstream(eac3_frame() * 60)
        a = ai.analyse_eac3("Dolby Digital Plus + Dolby Atmos", None, s)
        self.assertEqual((a.status, a.atmos), (ai.STATUS_INCONSISTENT, None))
        mixed = ai.summarize_ac3_bitstream(eac3_frame(joc=True) * 30 + eac3_frame() * 30)
        self.assertEqual(ai.analyse_eac3(None, None, mixed).status, ai.STATUS_INCONSISTENT)
        mi_no = ai.analyse_eac3(None, None, ai.summarize_ac3_bitstream(eac3_frame(joc=True) * 60),
                                {"Format": "E-AC-3"})
        self.assertEqual(mi_no.status, ai.STATUS_INCONSISTENT)

    def test_incomplete_metadata_uses_tools(self):
        a = ai.analyse_eac3("Dolby Digital Plus + Dolby Atmos", None, None)
        self.assertEqual((a.status, a.confidence), (ai.STATUS_DDP_ATMOS, "medium"))
        self.assertEqual(ai.analyse_eac3(None, None, None).status, ai.STATUS_DDP_NOT_DETECTED)

    def test_truncated_frames_resync(self):
        data = b"\x0b\x77garbage" + eac3_frame() * 40 + eac3_frame()[:50]
        s = ai.summarize_ac3_bitstream(data)
        self.assertEqual(s.frames, 40)

    def test_truehd_atmos(self):
        s = ai.summarize_truehd_bitstream(truehd_sync(True) * 5)
        self.assertEqual((s.syncs, s.atmos_syncs, s.substreams), (5, 5, [4]))
        a = ai.analyse_truehd("Dolby TrueHD + Dolby Atmos", "TrueHD Atmos 7.1", s)
        self.assertEqual((a.status, a.confidence), (ai.STATUS_TRUEHD_ATMOS, "high"))

    def test_truehd_without_atmos(self):
        s = ai.summarize_truehd_bitstream(truehd_sync(False) * 5)
        a = ai.analyse_truehd(None, "TrueHD 7.1", s)
        self.assertEqual((a.status, a.atmos), (ai.STATUS_TRUEHD_NO_ATMOS, False))
        self.assertEqual(ai.analyse_truehd(None, None, None).status, ai.STATUS_TRUEHD_UNVERIFIED)
        conflict = ai.analyse_truehd("Dolby TrueHD + Dolby Atmos", None, s)
        self.assertEqual(conflict.status, ai.STATUS_INCONSISTENT)

    def test_bad_signature_ignored(self):
        bad = bytearray(truehd_sync(True))
        bad[8] = 0
        self.assertEqual(ai.parse_truehd_syncs(bytes(bad)), [])

    def test_ac3(self):
        a = ai.analyse_ac3("Atmos 5.1", None)
        self.assertEqual(a.status, ai.STATUS_AC3_MISLABELLED)
        mismatch = ai.analyse_ac3(None, ai.summarize_ac3_bitstream(eac3_frame() * 40))
        self.assertEqual(mismatch.status, ai.STATUS_INCONSISTENT)


def with_atmos(report, index, analysis, bitstream=None):
    track = report.audio_by_index(index)
    track.atmos = analysis
    track.bitstream = bitstream
    return report


class AtmosRuleTests(unittest.TestCase):
    def joc_report(self):
        report = make_report([video(), audio(1, "eac3", profile="Dolby Digital Plus + Dolby Atmos",
                                             title="DD+ Atmos", default=True, language="eng")])
        s = ai.summarize_ac3_bitstream(eac3_frame(joc=True) * 60)
        return with_atmos(report, 1, ai.analyse_eac3(report.audio[0].profile, report.audio[0].title, s))

    def test_valid_joc_is_not_an_error(self):
        result = findings(self.joc_report())
        self.assertFalse([f for f in result if f.severity == cr.ERROR])
        status = [f for f in result if f.rule_id == "atmos.status"][0]
        self.assertEqual(status.severity, cr.INFO)
        fire = [f for f in result if f.rule_id == "firetv.eac3_joc"][0]
        self.assertEqual((fire.severity, fire.device), (cr.WARNING, "firetv_plex"))
        self.assertIn("Valid Atmos - potential Fire TV playback compatibility issue", fire.title)
        self.assertIn("not defective", fire.detail)

    def test_device_profiles_can_be_disabled(self):
        result = findings(self.joc_report(), cr.ScannerConfig(devices=[]))
        self.assertFalse([f for f in result if f.device])
        no_rule = findings(self.joc_report(), cr.ScannerConfig(disabled_rules=["atmos.status"]))
        self.assertFalse([f for f in no_rule if f.rule_id == "atmos.status"])

    def test_misleading_titles(self):
        report = make_report([video(), audio(1, "ac3", title="Atmos 5.1", default=True),
                              audio(2, "eac3", title="Dolby Atmos")])
        with_atmos(report, 1, ai.analyse_ac3("Atmos 5.1", None))
        with_atmos(report, 2, ai.analyse_eac3(None, "Dolby Atmos", ai.summarize_ac3_bitstream(eac3_frame() * 60)))
        titles = [f.title for f in findings(report, rule="atmos.misleading_title")]
        self.assertTrue(any("AC-3 track labelled Atmos" in t for t in titles))
        self.assertTrue(any("no Atmos is signalled" in t for t in titles))
        self.assertFalse(findings(report, rule="firetv.eac3_joc"))

    def test_codec_mismatch_is_error(self):
        report = make_report([video(), audio(1, "ac3", default=True)])
        s = ai.summarize_ac3_bitstream(eac3_frame() * 40)
        with_atmos(report, 1, ai.analyse_ac3(None, s), s.to_dict())
        f = findings(report, rule="atmos.codec_mismatch")[0]
        self.assertEqual(f.severity, cr.ERROR)

    def test_inconsistent_recommends_manual_inspection(self):
        report = make_report([video(), audio(1, "eac3", default=True)])
        mixed = ai.summarize_ac3_bitstream(eac3_frame(joc=True) * 30 + eac3_frame() * 30)
        with_atmos(report, 1, ai.analyse_eac3(None, None, mixed))
        f = findings(report, rule="atmos.inconsistent")[0]
        self.assertIn("manual inspection", f.title)


# --------------------------------------------------------------------------
# Dolby Vision / DTS / metadata
# --------------------------------------------------------------------------

def dv_video(profile, compat, rpu=1, el=0, transfer="smpte2084"):
    return video(color_transfer=transfer, color_primaries="bt2020", side_data_list=[
        {"side_data_type": "DOVI configuration record", "dv_profile": profile, "dv_level": 6,
         "rpu_present_flag": rpu, "el_present_flag": el, "bl_present_flag": 1,
         "dv_bl_signal_compatibility_id": compat}])


class VideoAndMetadataTests(unittest.TestCase):
    def test_dolby_vision_detection(self):
        p81 = make_report([dv_video(8, 1), audio(1, "eac3", default=True)])
        self.assertEqual(p81.video[0].hdr_formats, ["Dolby Vision", "HDR10"])
        self.assertIn("DV P8.1", p81.video[0].label)
        self.assertFalse([f for f in findings(p81) if f.category == cr.CAT_DV and f.severity == cr.ERROR])
        p5 = make_report([dv_video(5, 0), audio(1, "eac3", default=True)])
        self.assertEqual(p5.video[0].hdr_formats, ["Dolby Vision"])
        self.assertEqual(findings(p5, rule="dv.no_fallback")[0].severity, cr.WARNING)
        self.assertEqual(findings(p5, rule="ub450.dolby_vision")[0].severity, cr.WARNING)
        p7 = make_report([dv_video(7, 6, el=1), audio(1, "truehd", 8, "7.1", default=True)])
        self.assertTrue(findings(p7, rule="dv.profile7"))
        self.assertEqual(findings(p7, rule="lgc5.dolby_vision")[0].severity, cr.WARNING)
        self.assertEqual(findings(make_report([dv_video(8, 1, rpu=0)]), rule="dv.rpu_missing")[0].severity, cr.ERROR)
        self.assertTrue(findings(make_report([dv_video(8, 0)]), rule="dv.unusual_compat"))

    def test_hdr10_and_hlg(self):
        self.assertEqual(make_report([video(color_transfer="smpte2084")]).video[0].hdr_formats, ["HDR10"])
        self.assertEqual(make_report([video(color_transfer="arib-std-b67")]).video[0].hdr_formats, ["HLG"])
        self.assertEqual(make_report([video()]).video[0].hdr_formats, [])

    def test_dts_hd_ma_fire_tv(self):
        report = make_report([video(), audio(1, "dts", 8, "7.1", profile="DTS-HD MA", default=True)])
        f = findings(report, rule="firetv.dts_core")[0]
        self.assertEqual((f.severity, f.actions), (cr.WARNING, [cr.ACTION_DTS_CONVERT]))
        plain = make_report([video(), audio(1, "dts", 6, profile="DTS", default=True)])
        self.assertFalse(findings(plain, rule="firetv.dts_core"))

    def test_default_audio_flags(self):
        none = make_report([video(), audio(1, "ac3"), audio(2, "eac3")])
        self.assertIn("No default", findings(none, rule="meta.default_audio")[0].title)
        many = make_report([video(), audio(1, "ac3", default=True), audio(2, "eac3", default=True)])
        self.assertIn("2 audio tracks flagged default", findings(many, rule="meta.default_audio")[0].title)
        self.assertFalse(findings(make_report([video(), audio(1, "ac3")]), rule="meta.default_audio"))

    def test_title_and_layout_contradictions(self):
        report = make_report([video(), audio(1, "ac3", 6, title="DTS-HD MA 7.1", default=True, language="eng"),
                              audio(2, "eac3", 6, layout="7.1", language="eng"),
                              audio(3, "aac", 2, layout="stereo", language="eng", title="French")])
        titles = [f.title for f in findings(report, rule="meta.title_mismatch")]
        self.assertTrue(any("codec is Dolby Digital" in t for t in titles))
        self.assertTrue(any("says 7.1 but the stream has 6" in t for t in titles))
        self.assertTrue(findings(report, rule="meta.layout"))
        self.assertTrue(any(f.stream_index == 3 for f in findings(report, rule="meta.audio_language")))

    def test_duplicate_audio_and_malformed(self):
        report = make_report([video(), audio(1, "ac3", default=True, bit_rate="640000"),
                              audio(2, "ac3", bit_rate="640000"), audio(3, "eac3", 0, None, 0)])
        self.assertTrue(findings(report, rule="meta.duplicate_audio"))
        self.assertEqual(findings(report, rule="meta.layout")[0].severity, cr.ERROR)

    def test_unreadable_file(self):
        report = mi.MediaReport(path="/x.mkv", size=1, mtime_ns=1, errors=["ffprobe could not read the file"])
        result = findings(report)
        self.assertEqual([(f.rule_id, f.severity) for f in result], [("file.unreadable", cr.ERROR)])


class MediaInfoTests(unittest.TestCase):
    def test_mediainfo_fields_mapped(self):
        report = make_report([dv_video(8, 1), audio(1, "eac3", default=True), audio(2, "truehd", 8, "7.1")])
        data = {"media": {"track": [
            {"@type": "General"},
            {"@type": "Video", "StreamOrder": "0", "HDR_Format": "Dolby Vision / SMPTE ST 2094 App 4",
             "HDR_Format_Compatibility": "HDR10+ Profile A / HDR10"},
            {"@type": "Audio", "StreamOrder": "1", "Format": "E-AC-3", "Format_AdditionalFeatures": "JOC",
             "Format_Commercial_IfAny": "Dolby Digital Plus with Dolby Atmos", "ComplexityIndex": "16"},
            {"@type": "Audio", "StreamOrder": "2", "Format": "MLP FBA", "Format_AdditionalFeatures": "16-ch"},
        ]}}
        mi.apply_mediainfo(report, data)
        self.assertIn("HDR10+", report.video[0].hdr_formats)
        self.assertEqual(report.audio[0].mediainfo["ComplexityIndex"], "16")
        a = ai.analyse_eac3(None, None, None, report.audio[0].mediainfo)
        self.assertEqual(a.status, ai.STATUS_DDP_ATMOS)
        t = ai.analyse_truehd(None, None, None, report.audio[1].mediainfo)
        self.assertEqual(t.status, ai.STATUS_TRUEHD_ATMOS)

    def test_mkvmerge_flags(self):
        report = make_report([video(), stream(1, "subtitle", "subrip", language="eng")])
        mi.apply_mkvmerge(report, {"tracks": [{"id": 1, "properties": {
            "codec_id": "S_TEXT/UTF8", "flag_hearing_impaired": True, "forced_track": False}}]})
        self.assertTrue(report.subtitles[0].hearing_impaired)
        self.assertEqual(report.subtitles[0].codec_id, "S_TEXT/UTF8")

    def test_report_roundtrip(self):
        report = self.joc()
        again = mi.MediaReport.from_dict(report.to_dict())
        self.assertEqual(again.audio[0].atmos.status, ai.STATUS_DDP_ATMOS)
        self.assertEqual(again.to_dict(), report.to_dict())

    def joc(self):
        return AtmosRuleTests().joc_report()


# --------------------------------------------------------------------------
# Scanner, cache and cancellation
# --------------------------------------------------------------------------

class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dts2dd-scan-"))
        (self.dir / "A").mkdir()
        (self.dir / ".hidden").mkdir()
        for name in ("A/one.mkv", "two.MP4", "notes.txt", ".hidden/x.mkv", ".one.truehd-partial-abc.mkv",
                     "A/.Film.converting-1.mkv"):
            (self.dir / name).write_bytes(b"x")
        self.calls = []

    def fake_inspect(self, path, tools, options, cancel):
        self.calls.append(path)
        st = os.stat(path)
        return mi.MediaReport(path=path, size=st.st_size, mtime_ns=st.st_mtime_ns,
                              video=[mi.VideoTrack(0, "hevc")])

    def scanner(self, cache=None):
        tools = mi.ToolSet("ffmpeg", "ffprobe")
        return LibraryScanner(tools, cr.ScannerConfig(), cache or ScanCache(self.dir / "cache.json"),
                              inspector=self.fake_inspect)

    def test_recursive_discovery_skips_hidden_and_temp(self):
        names = sorted(os.path.relpath(p, self.dir) for p in find_media_files(str(self.dir)))
        self.assertEqual(names, [os.path.join("A", "one.mkv"), "two.MP4"])

    def test_cache_skips_unchanged_files(self):
        items = []
        summary = self.scanner().scan(str(self.dir), items.append)
        self.assertEqual((summary.total, summary.scanned, summary.cached), (2, 2, 0))
        self.calls.clear()
        summary = self.scanner().scan(str(self.dir), items.append)        # fresh cache object from disk
        self.assertEqual((summary.scanned, summary.cached, self.calls), (0, 2, []))
        target = self.dir / "two.MP4"
        target.write_bytes(b"changed!")
        summary = self.scanner().scan(str(self.dir), items.append)
        self.assertEqual((summary.scanned, summary.cached), (1, 1))
        self.assertEqual(self.calls, [str(target)])
        self.calls.clear()
        self.scanner().scan(str(self.dir), items.append, force=True)
        self.assertEqual(len(self.calls), 2)

    def test_cache_ignores_other_inspector_version(self):
        cache = ScanCache(self.dir / "cache.json")
        self.scanner(cache).scan(str(self.dir), lambda i: None)
        with mock.patch("library_scanner.INSPECTOR_VERSION", 999), \
                mock.patch.object(mi, "INSPECTOR_VERSION", 999):
            self.calls.clear()
            self.scanner(cache).scan(str(self.dir), lambda i: None)
        self.assertEqual(len(self.calls), 2)

    def test_progress_and_eta(self):
        progress = []
        self.scanner().scan(str(self.dir), lambda i: None, progress.append)
        self.assertEqual(progress[-1].done, 2)
        self.assertEqual(progress[-1].total, 2)

    def test_cancel_stops_scan(self):
        cancel = threading.Event()
        items = []

        def inspector(path, tools, options, c):
            cancel.set()
            raise mi.InspectionCancelled("stop")
        scanner = LibraryScanner(mi.ToolSet("f", "p"), cr.ScannerConfig(workers=1),
                                 ScanCache(self.dir / "cache.json"), inspector=inspector)
        summary = scanner.scan(str(self.dir), items.append, cancel=cancel)
        self.assertTrue(summary.cancelled)
        self.assertEqual(items, [])

    def test_failing_file_does_not_stop_scan(self):
        def inspector(path, tools, options, c):
            if path.endswith("one.mkv"):
                raise ValueError("boom")
            return self.fake_inspect(path, tools, options, c)
        items = []
        scanner = LibraryScanner(mi.ToolSet("f", "p"), cr.ScannerConfig(), ScanCache(self.dir / "c.json"),
                                 inspector=inspector)
        summary = scanner.scan(str(self.dir), items.append)
        self.assertEqual((summary.total, summary.errors), (2, 1))
        bad = [i for i in items if i.report.errors][0]
        self.assertEqual(bad.severity, cr.ERROR)

    def test_scan_never_writes_media(self):
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.dir.rglob("*") if p.is_file()}
        self.scanner().scan(str(self.dir), lambda i: None)
        for p, state in before.items():
            self.assertEqual((p.stat().st_mtime_ns, p.read_bytes()), state)


# --------------------------------------------------------------------------
# Repair planning and batch behaviour
# --------------------------------------------------------------------------

def item_of(report):
    item = ScanItem(report=report)
    item.refresh(cr.ScannerConfig())
    return item


class RepairPlanTests(unittest.TestCase):
    def test_combined_plan_is_one_pass(self):
        report = make_report([video(), flac(1, 8, "7.1", default=True, language="eng")]
                             + [stream(2 + i, "subtitle", "hdmv_pgs_subtitle", language=l)
                                for i, l in enumerate(LANGS[:10])])
        plan = lr.plan_repair(item_of(report), [cr.ACTION_CLEAN_SUBS, cr.ACTION_FLAC_TRUEHD, cr.ACTION_FIX_DEFAULT],
                              lr.RepairOptions(), cr.ScannerConfig(), ready_encoder())
        self.assertEqual(plan.method, "truehd")
        self.assertEqual(plan.remove_streams, list(range(3, 12)))
        self.assertEqual(plan.actions, [cr.ACTION_FLAC_TRUEHD, cr.ACTION_CLEAN_SUBS, cr.ACTION_FIX_DEFAULT])
        text = "\n".join(plan.describe())
        self.assertIn("Dolby TrueHD 7.1", text)
        self.assertIn("REMOVE", text)

    def test_flac_skipped_without_verified_encoder(self):
        report = make_report([video(), flac(1, 8, "7.1")])
        plan = lr.plan_repair(item_of(report), [cr.ACTION_FLAC_TRUEHD], lr.RepairOptions(), cr.ScannerConfig(),
                              ready_encoder(("5.1(side)",)))
        self.assertIsNone(plan.method)
        self.assertIn("7.1 self-test", plan.skipped[0][1])

    def test_one_audio_conversion_per_file(self):
        report = make_report([video(), flac(1, 6, "5.1"), audio(2, "dts", 8, "7.1", profile="DTS-HD MA")])
        plan = lr.plan_repair(item_of(report), [cr.ACTION_FLAC_TRUEHD, cr.ACTION_DTS_CONVERT], lr.RepairOptions(),
                              cr.ScannerConfig(), ready_encoder())
        self.assertEqual(plan.audio_job.action, cr.ACTION_FLAC_TRUEHD)
        self.assertEqual(plan.skipped[0][0], cr.ACTION_DTS_CONVERT)

    def test_dts_auto_target(self):
        hd = make_report([video(), audio(1, "dts", 8, "7.1", profile="DTS-HD MA", sample_fmt="s32p",
                                         bits_per_raw_sample="24")])
        plan = lr.plan_repair(item_of(hd), [cr.ACTION_DTS_CONVERT], lr.RepairOptions(), cr.ScannerConfig(),
                              ready_encoder())
        self.assertEqual(plan.audio_job.kind, "truehd")
        lossy = make_report([video(), audio(1, "dts", 6, profile="DTS", sample_fmt="fltp")])
        plan = lr.plan_repair(item_of(lossy), [cr.ACTION_DTS_CONVERT], lr.RepairOptions(), cr.ScannerConfig(),
                              ready_encoder())
        self.assertEqual((plan.audio_job.kind, plan.audio_job.bitrate), ("eac3", "640k"))
        ac3 = lr.plan_repair(item_of(lossy), [cr.ACTION_DTS_CONVERT],
                             lr.RepairOptions(dts_target="ac3", lossy_bitrate="768k"), cr.ScannerConfig(), None)
        self.assertEqual(ac3.audio_job.bitrate, "640k")        # AC-3 maximum

    def test_existing_truehd_not_duplicated(self):
        report = make_report([video(), flac(1, 8, "7.1"), audio(2, "truehd", 8, "7.1")])
        plan = lr.plan_repair(item_of(report), [cr.ACTION_FLAC_TRUEHD], lr.RepairOptions(), cr.ScannerConfig(),
                              ready_encoder())
        self.assertIsNone(plan.method)

    def test_default_fix_dispositions(self):
        report = make_report([video(), audio(1, "ac3", default=True), audio(2, "eac3", default=True),
                              audio(3, "aac", 2, "stereo")])
        plan = lr.plan_repair(item_of(report), [cr.ACTION_FIX_DEFAULT], lr.RepairOptions(), cr.ScannerConfig(), None)
        self.assertEqual(plan.dispositions, {"a:0": "+default", "a:1": "-default", "a:2": "-default"})
        self.assertEqual(plan.method, "remux")

    def test_non_mkv_and_unreadable_never_repaired(self):
        mp4 = make_report([video(), audio(1, "ac3")] + [stream(i, "subtitle", "mov_text", language="fre")
                                                        for i in range(2, 10)], path="/f/Film.mp4")
        plan = lr.plan_repair(item_of(mp4), [cr.ACTION_CLEAN_SUBS], lr.RepairOptions(), cr.ScannerConfig(), None)
        self.assertIsNone(plan.method)
        self.assertIn("only performed on MKV", plan.skipped[0][1])
        self.assertTrue(findings(mp4, rule="file.not_mkv"))

    def test_remux_command_is_stream_copy(self):
        import ffmpeg_helper
        cmd = ffmpeg_helper.build_remux_command("ffmpeg", "in.mkv", "out.mkv", [5, 3], {"a:0": "+default"})
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertNotIn("-c:v", cmd)
        self.assertIn("-0:3", cmd)
        self.assertIn("passthrough", cmd)
        self.assertEqual(cmd[cmd.index("-disposition:a:0") + 1], "+default")

    def test_ffmpeg_never_invents_default_flags(self):
        """Without an explicit disposition the ffmpeg CLI flags the first subtitle default."""
        import ffmpeg_helper
        probe = {"streams": [{"index": 0, "codec_type": "video", "disposition": {"default": 1, "dub": 0}},
                             {"index": 1, "codec_type": "audio", "disposition": {"default": 0}}]}
        self.assertEqual(ffmpeg_helper.keep_dispositions_args(probe), ["-disposition:0", "default"])
        self.assertEqual(ffmpeg_helper.keep_dispositions_args(probe, [0]), ["-disposition:0", "0"])
        cmd = ffmpeg_helper.build_remove_streams_command("ffmpeg", "i.mkv", "o.mkv", [3], ["-disposition:0", "0"])
        self.assertIn("-disposition:0", cmd)

    def test_lossy_append_metadata(self):
        import ffmpeg_helper
        cmd = ffmpeg_helper.build_append_audio_command("ffmpeg", "i.mkv", "o.mkv", 1, 2, "eac3", "640k", False,
                                                       make_default=True, title="Dolby Digital Plus 5.1")
        self.assertIn("title=Dolby Digital Plus 5.1", cmd)
        self.assertEqual(cmd[cmd.index("-disposition:a:2") + 1], "default")
        legacy = ffmpeg_helper.build_append_audio_command("ffmpeg", "i.mkv", "o.mkv", 1, 2, "eac3", "640k", False)
        self.assertFalse(any(c.startswith("-disposition") for c in legacy))   # original behaviour unchanged


class BatchTests(unittest.TestCase):
    def plan(self, path, method="remux"):
        report = make_report([video(), audio(1, "ac3", default=True), audio(2, "ac3", default=True)], path=path)
        return lr.plan_repair(item_of(report), [cr.ACTION_FIX_DEFAULT], lr.RepairOptions(), cr.ScannerConfig(), None)

    def test_batch_reports_each_outcome(self):
        plans = [self.plan(f"/f/{n}.mkv") for n in ("a", "b", "c")]
        results = iter([lr.RepairOutcome("/f/a.mkv", lr.STATUS_REPAIRED, [cr.ACTION_FIX_DEFAULT]),
                        lr.RepairOutcome("/f/b.mkv", lr.STATUS_FAILED, message="ffmpeg failed"),
                        lr.RepairOutcome("/f/c.mkv", lr.STATUS_SKIPPED, message="nothing to do")])
        ctx = lr.RepairContext("ffmpeg", "ffprobe", None, lr.RepairOptions())
        report = lr.run_batch(plans, ctx, executor=lambda p, c: next(results))
        self.assertEqual((report.count(lr.STATUS_REPAIRED), report.count(lr.STATUS_FAILED),
                          report.count(lr.STATUS_SKIPPED)), (1, 1, 1))
        text = report.text()
        self.assertIn("ffmpeg failed", text)
        self.assertIn("Fix default audio", text)

    def test_cancel_skips_remaining(self):
        plans = [self.plan(f"/f/{n}.mkv") for n in ("a", "b", "c")]
        ctx = lr.RepairContext("ffmpeg", "ffprobe", None, lr.RepairOptions())

        def executor(plan, c):
            c.handle.cancel()
            return lr.RepairOutcome(plan.path, lr.STATUS_CANCELLED)
        report = lr.run_batch(plans, ctx, executor=executor)
        self.assertEqual(report.count(lr.STATUS_CANCELLED), 3)
        self.assertEqual(len([o for o in report.outcomes if "not started" in o.message]), 2)

    def test_file_changed_since_scan_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "Film.mkv")
            Path(path).write_bytes(b"x")
            plan = self.plan(path)          # report size/mtime do not match the real file
            outcome = lr.execute_plan(plan, lr.RepairContext("ffmpeg", "ffprobe", None, lr.RepairOptions()))
            self.assertEqual(outcome.status, lr.STATUS_SKIPPED)
            self.assertIn("changed after it was scanned", outcome.message)
            self.assertEqual(Path(path).read_bytes(), b"x")


if __name__ == "__main__":
    unittest.main()
