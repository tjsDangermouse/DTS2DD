"""M2TS remux unit tests: mocked tsMuxeR output, mocked probes, synthetic bitstreams.

These check the app's logic only. Nothing here shows that the real tsMuxeR
preserves Dolby Vision, HDR10+ or Atmos - see test_m2ts_integration.py
(RealTsMuxerTests) and the manual Panasonic test in USER_GUIDE.md.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sets sys.path)

import atmos_inspector as ai
import compatibility_rules as cr
import m2ts_remux as mr
import media_inspector as mi
import tsmuxer_tool as tt

# What tsMuxeR 2.7.0 prints for a Blade Runner 2049-like UHD remux (format from main.cpp).
BLADE_RUNNER_DETECTION = """tsMuxeR version 2.7.0. github.com/justdan96/tsMuxer
Track ID:    1
Stream type: HEVC
Stream ID:   V_MPEGH/ISO/HEVC
Stream info: Profile: Main10@5.1 Resolution: 3840:2160p Frame rate: 23.976
Stream lang: eng

Track ID:    2
Stream type: TRUE-HD
Stream ID:   A_MLP
Stream info: TRUE-HD + ATMOS. Peak bitrate: 7046Kbps Sample Rate: 48KHz Channels: 8
Stream lang: eng

Track ID:    3
Stream type: AC3
Stream ID:   A_AC3
Stream info: Bitrate: 640Kbps Sample Rate: 48KHz Channels: 6
Stream lang: eng
Stream delay: -12

Track ID:    4
Stream type: PGS
Stream ID:   S_HDMV/PGS
Stream info:
Stream lang: eng

Track ID:    5
Stream type: PGS
Stream ID:   S_HDMV/PGS
Stream info:
Stream lang: fre

Track ID:    6
Can't detect stream type

Duration: 02:43:50.123
"""


def stream(index, kind, codec, **kw):
    s = {"index": index, "codec_type": kind, "codec_name": codec, "tags": {}, "disposition": {}}
    for key in ("language", "title"):
        if key in kw:
            s["tags"][key] = kw.pop(key)
    for key in ("default", "forced"):
        if kw.pop(key, False):
            s["disposition"][key] = 1
    s.update(kw)
    return s


def blade_runner_probe():
    dovi = {"side_data_type": "DOVI configuration record", "dv_profile": 8, "dv_level": 6,
            "rpu_present_flag": 1, "el_present_flag": 0, "bl_present_flag": 1, "dv_bl_signal_compatibility_id": 1}
    return {
        "format": {"format_name": "matroska,webm", "duration": "9830.123"},
        "chapters": [{}] * 16,
        "streams": [
            stream(0, "video", "hevc", profile="Main 10", width=3840, height=2160, pix_fmt="yuv420p10le",
                   color_transfer="smpte2084", color_primaries="bt2020", r_frame_rate="24000/1001",
                   side_data_list=[dovi, {"side_data_type": "Mastering display metadata"}],
                   tags={"NUMBER_OF_BYTES": "60000000000"}),
            stream(1, "audio", "truehd", profile="Dolby TrueHD + Dolby Atmos", channels=8, channel_layout="7.1",
                   sample_rate="48000", language="eng", title="TrueHD Atmos 7.1", default=True,
                   tags={"NUMBER_OF_BYTES": "5000000000"}),
            stream(2, "audio", "ac3", channels=6, channel_layout="5.1(side)", sample_rate="48000", language="eng",
                   tags={"NUMBER_OF_BYTES": "780000000"}),
            stream(3, "subtitle", "hdmv_pgs_subtitle", language="eng", tags={"NUMBER_OF_BYTES": "40000000"}),
            stream(4, "subtitle", "hdmv_pgs_subtitle", language="fre", tags={"NUMBER_OF_BYTES": "40000000"}),
            stream(5, "subtitle", "ass", language="eng", tags={"NUMBER_OF_BYTES": "100000"}),
            stream(6, "attachment", "ttf"),
        ],
    }


def blade_runner(path="/films/Blade Runner 2049 DV.mkv", atmos=True, hdr10plus=True):
    probe = blade_runner_probe()
    if not atmos:
        probe["streams"][1]["profile"] = None
    for s in probe["streams"]:          # language/title got overwritten by tags=... above; restore
        if s["codec_type"] in ("audio", "subtitle"):
            s["tags"].setdefault("language", {1: "eng", 2: "eng", 3: "eng", 4: "fre", 5: "eng"}[s["index"]])
    report = mi.report_from_probe(path, probe, size=66_000_000_000, mtime_ns=123)
    v = report.video[0]
    if hdr10plus:
        v.hdr_formats.append("HDR10+")
    truehd = report.audio[0]
    sync = truehd_sync(atmos)
    truehd.atmos = ai.analyse_truehd(truehd.profile, truehd.title, ai.summarize_truehd_bitstream(sync * 40))
    ac3 = report.audio[1]
    ac3.atmos = ai.analyse_ac3(None, None)
    hdr = mr.HdrSnapshot(dovi=mr._dovi_from_stream(probe["streams"][0]), container=["mastering"],
                         transfer="smpte2084", primaries="bt2020",
                         frames={"start": sorted(["rpu", "mastering", "cll"] + (["hdr10plus"] if hdr10plus else [])),
                                 "middle": sorted(["rpu", "mastering", "cll"] + (["hdr10plus"] if hdr10plus else []))})
    return report, probe, tt.parse_detection(BLADE_RUNNER_DETECTION), hdr


def truehd_sync(atmos, substreams=None):
    """Minimal TrueHD access unit with a major sync header (see atmos_inspector.parse_truehd_syncs)."""
    unit = bytearray(28)
    unit[4:8] = ai.TRUEHD_SYNC
    unit[8] = 0x0                         # 48 kHz
    unit[12:14] = ai.TRUEHD_SIGNATURE
    unit[20] = ((substreams or (4 if atmos else 3)) << 4)
    unit[21] = 0x80 if atmos else 0x00
    return bytes(unit)


class DetectionParsingTests(unittest.TestCase):
    def test_blade_runner_tracks(self):
        det = tt.parse_detection(BLADE_RUNNER_DETECTION)
        self.assertEqual([t.track_id for t in det.tracks], [1, 2, 3, 4, 5, 6])
        self.assertEqual(det.track(2).codec_id, "A_MLP")
        self.assertTrue(det.track(2).atmos)
        self.assertEqual(det.track(3).delay_ms, -12)
        self.assertFalse(det.track(6).detected)
        self.assertAlmostEqual(det.duration, 9830.123, places=2)

    def test_escape_codes_and_carriage_returns(self):
        det = tt.parse_detection("\x1b[ATrack ID:    1\r\nStream ID:   V_MPEG4/ISO/AVC\r\n")
        self.assertEqual(det.track(1).codec_id, "V_MPEG4/ISO/AVC")

    def test_versions(self):
        self.assertEqual(tt.parse_version("tsMuxeR version 2.7.0. github.com/justdan96/tsMuxer")[0], (2, 7, 0))
        self.assertEqual(tt.parse_version("tsMuxeR version 2.6.12. github")[0], (2, 6, 12))
        self.assertEqual(tt.parse_version("tsMuxeR version git-9e1b64c. github")[1], "git-9e1b64c")
        self.assertEqual(tt.parse_version("ffmpeg version 7.1"), (None, ""))

    def test_check_states(self):
        tool = tempfile.NamedTemporaryFile(delete=False)
        tool.close()
        self.addCleanup(os.unlink, tool.name)
        with mock.patch.object(tt, "candidates", return_value=[(tool.name, "configured")]):
            ok = tt.check_tsmuxer(tool.name, runner=lambda c, t: (255, "tsMuxeR version 2.7.0. github.com"))
            old = tt.check_tsmuxer(tool.name, runner=lambda c, t: (255, "tsMuxeR version 2.6.12. github.com"))
            dev = tt.check_tsmuxer(tool.name, runner=lambda c, t: (255, "tsMuxeR version git-abc. github.com"))
            bad = tt.check_tsmuxer(tool.name, runner=lambda c, t: (1, "usage: something else"))

            def boom(c, t):
                raise OSError("permission denied")
            err = tt.check_tsmuxer(tool.name, runner=boom)
        self.assertTrue(ok.ok)
        self.assertEqual(old.status, tt.STATUS_OLD)
        self.assertFalse(old.ok)
        self.assertTrue(dev.ok)
        self.assertIn("not recognised", dev.message)
        self.assertEqual(bad.status, tt.STATUS_BROKEN)
        self.assertEqual(err.status, tt.STATUS_BROKEN)
        with mock.patch.object(tt, "candidates", return_value=[]):
            missing = tt.check_tsmuxer(None)
        self.assertEqual(missing.status, tt.STATUS_MISSING)

    def test_detect_tracks_errors(self):
        with self.assertRaises(tt.TsMuxerError):
            tt.detect_tracks("tsMuxeR", "x.mkv", runner=lambda c, t: (255, "Can't open file"))


class MetaFileTests(unittest.TestCase):
    def test_quoting_language_and_options(self):
        path = "D:/Films/Blade Runner 2049, Final – Ünïcode.mkv"
        text = tt.build_meta(path, [
            tt.MetaTrack("V_MPEGH/ISO/HEVC", 1, "eng", [("fps", "23.976")]),
            tt.MetaTrack("A_MLP", 2, "und"),
            tt.MetaTrack("A_AC3", 3, "en", [("timeshift", "-12ms")]),
            tt.MetaTrack("S_TEXT/UTF8", 4, "eng", [("font-name", "Times New Roman")]),
        ])
        lines = text.splitlines()
        self.assertEqual(lines[0], tt.MUXOPT)
        self.assertEqual(lines[1], f'V_MPEGH/ISO/HEVC, "{path}", fps=23.976, track=1, lang=eng')
        self.assertEqual(lines[2], f'A_MLP, "{path}", track=2')               # 'und' is not written
        self.assertEqual(lines[3], f'A_AC3, "{path}", timeshift=-12ms, track=3')   # not 3 letters
        self.assertIn('font-name="Times New Roman"', lines[4])

    def test_rejects_double_quotes(self):
        with self.assertRaises(tt.TsMuxerError):
            tt.build_meta('/x/a "b".mkv', [])

    def test_meta_is_utf8(self):
        path = tt.write_meta("V_MPEGH/ISO/HEVC, \"/f/Ünï.mkv\", track=1\n")
        self.addCleanup(lambda: __import__("shutil").rmtree(os.path.dirname(path), ignore_errors=True))
        self.assertTrue(path.endswith(".meta"))
        self.assertIn("Ünï", Path(path).read_text(encoding="utf-8"))

    def test_fps(self):
        self.assertEqual(tt.fps_value("24000/1001"), "23.976")
        self.assertEqual(tt.fps_value("25/1"), "25")
        self.assertEqual(tt.fps_value("60000/1001"), "59.94")
        self.assertIsNone(tt.fps_value("1000/33"))
        self.assertIsNone(tt.fps_value(None))


class PlanTests(unittest.TestCase):
    def plan(self, **kw):
        report, probe, det, hdr = blade_runner(**kw)
        return mr.build_plan(report.path, report, probe, det, hdr)

    def test_blade_runner_plan(self):
        plan = self.plan()
        self.assertEqual(plan.blockers, [])
        self.assertEqual(plan.output, str(Path("/films/Blade Runner 2049 DV.m2ts")))
        truehd, ac3 = plan.audio
        self.assertTrue(truehd.selected and ac3.selected)          # every compatible audio track kept
        self.assertEqual(truehd.ts.codec_id, "A_MLP")
        self.assertIn("no AC-3 core", truehd.handling)
        self.assertEqual(truehd.atmos, ai.STATUS_TRUEHD_ATMOS)
        self.assertEqual(plan.first_audio, 1)                       # MKV default track first
        eng, fre, ass = plan.subtitles
        self.assertTrue(eng.selected)                               # compatible English kept
        self.assertFalse(fre.selected)
        self.assertFalse(ass.compatible)
        self.assertIn("ASS/SSA", ass.handling)
        # Dolby Vision 8.1 and HDR10+ need an explicit compatibility-test confirmation.
        self.assertEqual(len(plan.metadata_warnings), 2)
        self.assertTrue(any("8.1" in w for w in plan.metadata_warnings))
        self.assertTrue(any("HDR10+" in w for w in plan.metadata_warnings))
        self.assertTrue(plan.needs_confirmation)
        self.assertTrue(any("AC-3 core" in i for i in plan.info))
        self.assertTrue(any("16 chapter" in i for i in plan.info))
        meta = plan.meta_text().splitlines()
        self.assertEqual([l.split(",")[0] for l in meta[1:]], ["V_MPEGH/ISO/HEVC", "A_MLP", "A_AC3", "S_HDMV/PGS"])
        self.assertIn("fps=23.976", meta[1])
        self.assertIn("timeshift=-12ms", meta[3])
        self.assertIn("track=4", meta[4])

    def test_first_audio_reorders_meta(self):
        plan = self.plan()
        plan.first_audio = 2
        meta = plan.meta_text().splitlines()
        self.assertEqual([l.split(",")[0] for l in meta[2:4]], ["A_AC3", "A_MLP"])

    def test_estimated_size_from_statistics(self):
        plan = self.plan()
        expected = (60_000_000_000 + 5_000_000_000 + 780_000_000 + 40_000_000) * mr.TS_OVERHEAD
        self.assertAlmostEqual(plan.estimated_bytes(), expected, delta=2)
        for t in plan.subtitles:
            t.selected = False
        self.assertLess(plan.estimated_bytes(), expected)

    def test_selection_problems(self):
        plan = self.plan()
        for t in plan.audio:
            t.selected = False
        self.assertTrue(any("No audio" in p for p in plan.selection_problems()))
        plan.subtitles[2].selected = True       # ASS
        self.assertTrue(any("ASS/SSA" in p for p in plan.selection_problems()))

    def test_track_numbering_mismatch_blocks(self):
        report, probe, det, hdr = blade_runner()
        det.track(2).codec_id = "A_DTS"
        plan = mr.build_plan(report.path, report, probe, det, hdr)
        self.assertTrue(any("numbers the tracks differently" in b for b in plan.blockers))

    def test_undetected_track_is_left_out(self):
        report, probe, det, hdr = blade_runner()
        det.track(3).detected = False
        plan = mr.build_plan(report.path, report, probe, det, hdr)
        self.assertFalse(plan.audio[1].compatible)
        self.assertIn("could not identify", plan.audio[1].handling)
        self.assertTrue(any("will be left out" in w for w in plan.warnings))

    def test_conventional_truehd_and_sdr(self):
        report, probe, det, _ = blade_runner(atmos=False, hdr10plus=False)
        det.track(2).info = "TRUE-HD. Peak bitrate: 4000Kbps"
        report.video[0].dv_profile = None
        report.video[0].hdr_formats = ["HDR10"]
        plan = mr.build_plan(report.path, report, probe, det, None)
        self.assertEqual(plan.audio[0].atmos, ai.STATUS_TRUEHD_NO_ATMOS)
        self.assertEqual(plan.metadata_warnings, [])

    def test_tsmuxer_atmos_label_never_overrides_bitstream(self):
        report, probe, det, hdr = blade_runner(atmos=False)
        plan = mr.build_plan(report.path, report, probe, det, hdr)
        self.assertTrue(any("bitstream check found no Atmos" in w for w in plan.warnings))

    def test_mastering_only_in_mkv_header_is_warned(self):
        report, probe, det, hdr = blade_runner()
        hdr.frames = {"start": ["rpu"]}
        plan = mr.build_plan(report.path, report, probe, det, hdr)
        self.assertTrue(any("only in the MKV header" in w for w in plan.metadata_warnings))

    def test_non_mkv_and_no_audio(self):
        report, probe, det, hdr = blade_runner(path="/films/x.mp4")
        plan = mr.build_plan(report.path, report, probe, det, hdr)
        self.assertTrue(any("Only MKV" in b for b in plan.blockers))
        report, probe, det, hdr = blade_runner()
        for s in report.audio:
            s.codec = "flac"
        plan = mr.build_plan(report.path, report, probe, det, hdr)
        self.assertTrue(any("No audio track can be carried" in b for b in plan.blockers))

    def test_dts_hd_ma_and_eac3_joc(self):
        probe = {"format": {"format_name": "matroska,webm", "duration": "100"}, "streams": [
            stream(0, "video", "h264", width=1920, height=1080, r_frame_rate="24/1"),
            stream(1, "audio", "dts", profile="DTS-HD MA", channels=8, channel_layout="7.1", sample_rate="48000",
                   language="eng"),
            stream(2, "audio", "eac3", profile="Dolby Digital Plus + Dolby Atmos", channels=6,
                   channel_layout="5.1(side)", sample_rate="48000", language="eng"),
            stream(3, "subtitle", "subrip", language="eng"),
        ]}
        report = mi.report_from_probe("/f/m.mkv", probe, 1000, 1)
        report.audio[1].atmos = ai.AtmosAnalysis(ai.STATUS_DDP_ATMOS, True, "high")
        det = tt.parse_detection("Track ID: 1\nStream ID: V_MPEG4/ISO/AVC\n\nTrack ID: 2\nStream ID: A_DTS\n"
                                 "\nTrack ID: 3\nStream ID: A_AC3\n\nTrack ID: 4\nStream ID: S_TEXT/UTF8\n")
        plan = mr.build_plan("/f/m.mkv", report, probe, det, None)
        dts, eac3 = plan.audio
        self.assertTrue(dts.selected and eac3.selected)
        self.assertIn("core and extensions", dts.handling)
        self.assertEqual(eac3.atmos_flag, True)
        srt = plan.subtitles[0]
        self.assertTrue(srt.compatible)
        self.assertFalse(srt.selected)
        srt.selected = True
        line = plan.meta_text().splitlines()[-1]
        self.assertTrue(line.startswith("S_TEXT/UTF8"))
        self.assertIn("video-width=1920", line)
        self.assertIn("fps=24", line)

    def test_subtitle_modes(self):
        report, probe, det, hdr = blade_runner()
        none = mr.build_plan(report.path, report, probe, det, hdr, subtitle_mode="none")
        self.assertEqual(none.selected("subtitle"), [])
        every = mr.build_plan(report.path, report, probe, det, hdr, subtitle_mode="all")
        self.assertEqual([t.language for t in every.selected("subtitle")], ["eng", "fre"])


class ComparisonTests(unittest.TestCase):
    def test_window_matching(self):
        src = os.urandom(100_000)
        self.assertTrue(mr.window_matches(src, src[1234:60_000]))
        altered = bytearray(src[1234:60_000])
        altered[30_000] ^= 1
        self.assertFalse(mr.window_matches(src, bytes(altered)))
        self.assertFalse(mr.window_matches(src, os.urandom(10_000)))
        self.assertIsNone(mr.window_matches(src, b"short"))

    def test_frame_matching(self):
        src = [f"h{i}" for i in range(40)]
        self.assertTrue(mr.frames_match(src, src[5:17]))
        self.assertFalse(mr.frames_match(src, src[5:10] + ["x"] + src[11:17]))
        self.assertIsNone(mr.frames_match(src, ["zz"]))

    def hdr_check(self, src, out):
        v = mr.Verification()
        mr._compare_hdr(v, src, out)
        return v

    def test_hdr_unchanged(self):
        _, _, _, src = blade_runner()
        out = mr.HdrSnapshot(dovi=dict(src.dovi), transfer="smpte2084", frames=dict(src.frames))
        v = self.hdr_check(src, out)
        self.assertEqual(v.hdr_status, mr.OUTCOME_HDR_SAME, v.hdr_lines)

    def test_dovi_profile_or_hdr10plus_change_is_reported(self):
        _, _, _, src = blade_runner()
        out = mr.HdrSnapshot(dovi={**src.dovi, "compat": 4}, transfer="smpte2084", frames=dict(src.frames))
        self.assertEqual(self.hdr_check(src, out).hdr_status, mr.OUTCOME_HDR_CHANGED)
        lost = mr.HdrSnapshot(dovi=dict(src.dovi), transfer="smpte2084",
                              frames={k: [x for x in keys if x != "hdr10plus"] for k, keys in src.frames.items()})
        v = self.hdr_check(src, lost)
        self.assertEqual(v.hdr_status, mr.OUTCOME_HDR_CHANGED)
        self.assertTrue(any("HDR10+" in l and "ABSENT" in l for l in v.hdr_lines))
        level = mr.HdrSnapshot(dovi={**src.dovi, "level": 9}, transfer="smpte2084", frames=dict(src.frames))
        v = self.hdr_check(src, level)
        self.assertEqual(v.hdr_status, mr.OUTCOME_HDR_SAME)      # tsMuxeR recomputes the level
        self.assertTrue(any("level differs" in l for l in v.hdr_lines))

    def test_hdr_unverified_when_frames_missing(self):
        _, _, _, src = blade_runner()
        out = mr.HdrSnapshot(dovi=dict(src.dovi), transfer="smpte2084", frames={"start": src.frames["start"]},
                             errors=["middle: timeout"])
        self.assertEqual(self.hdr_check(src, out).hdr_status, mr.OUTCOME_HDR_CHANGED)
        self.assertEqual(self.hdr_check(None, out).hdr_status, mr.OUTCOME_HDR_CHANGED)

    def test_atmos_check_from_output_bitstream(self):
        report, probe, det, hdr = blade_runner()
        plan = mr.build_plan(report.path, report, probe, det, hdr)
        truehd = plan.audio[0]
        for atmos, status in ((True, mr.OK), (False, mr.FAIL)):
            v = mr.Verification()
            with mock.patch.object(mr, "raw_sample", return_value=truehd_sync(atmos) * 40):
                mr._check_atmos(v, plan, truehd, {"index": 1, "profile": None}, "ffmpeg", "out.m2ts", 9000, None)
            self.assertEqual(v.checks[-1].status, status)
            outcome = mr.OUTCOME_ATMOS if atmos else mr.OUTCOME_ATMOS_MISSING
            self.assertIn(outcome, v.outcomes())
            self.assertIn(mr.OUTCOME_PANASONIC, v.outcomes())

    def test_outcomes_never_claim_atmos_or_verified_audio_without_evidence(self):
        v = mr.Verification()
        v.add("Audio #1", mr.OK)
        v.audio_identical[1] = None           # e.g. full comparison off / not comparable
        self.assertIn(mr.OUTCOME_AUDIO_PRESENT, v.outcomes())
        self.assertNotIn(mr.OUTCOME_ATMOS, v.outcomes())

    def test_truehd_core_substream_is_not_a_track(self):
        probe = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc", "id": "0x1011"},
            {"index": 1, "codec_type": "audio", "codec_name": "truehd", "id": "0x1100", "channels": 8},
            {"index": 2, "codec_type": "audio", "codec_name": "ac3", "id": "0x1100", "channels": 0},
            {"index": 3, "codec_type": "audio", "codec_name": "ac3", "id": "0x1101", "channels": 6},
        ]}
        video, audio, subs, cores = mr._output_groups(probe)
        self.assertEqual([a["index"] for a in audio], [1, 3])
        self.assertEqual(cores["0x1100"]["channels"], 0)


class RemuxGuardTests(unittest.TestCase):
    """Checks that run before tsMuxeR is started (no tools needed)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.source = self.tmp / "Blade Runner 2049 DV.mkv"
        self.source.write_bytes(b"mkv" * 100)
        st = self.source.stat()
        report, probe, det, hdr = blade_runner(path=str(self.source))
        self.plan = mr.build_plan(str(self.source), report, probe, det, hdr)
        self.plan.source_size, self.plan.source_mtime_ns = st.st_size, st.st_mtime_ns
        info = tt.TsMuxerInfo("/bin/tsMuxeR", status=tt.STATUS_READY)
        self.remuxer = mr.M2TSRemuxer(info, "ffmpeg", "ffprobe")

    def test_insufficient_disk_space(self):
        Usage = __import__("collections").namedtuple("Usage", "total used free")
        with mock.patch.object(mr.shutil, "disk_usage", return_value=Usage(1, 1, 1000)), \
                mock.patch.object(mr, "run_tsmuxer") as run:
            with self.assertRaises(mr.RemuxError) as ctx:
                self.remuxer.remux(self.plan, mr.RemuxOptions(accept_metadata_risk=True))
        self.assertIn("Not enough free disk space", str(ctx.exception))
        run.assert_not_called()
        self.assertEqual([p.name for p in self.tmp.iterdir()], [self.source.name])

    def test_existing_output(self):
        Path(self.plan.output).write_bytes(b"x")
        with mock.patch.object(mr, "run_tsmuxer") as run, self.assertRaises(mr.OutputExists):
            self.remuxer.remux(self.plan, mr.RemuxOptions(accept_metadata_risk=True))
        run.assert_not_called()

    def test_source_changed_since_analysis(self):
        self.source.write_bytes(b"changed")
        with self.assertRaises(mr.RemuxError) as ctx:
            self.remuxer.remux(self.plan, mr.RemuxOptions(accept_metadata_risk=True))
        self.assertIn("changed after it was analysed", str(ctx.exception))

    def test_output_must_differ_and_be_m2ts(self):
        self.plan.output = str(self.source)
        with self.assertRaises(mr.RemuxError):
            self.remuxer.remux(self.plan, mr.RemuxOptions(accept_metadata_risk=True))
        self.plan.output = str(self.tmp / "out.ts")
        with self.assertRaises(mr.RemuxError):
            self.remuxer.remux(self.plan, mr.RemuxOptions(accept_metadata_risk=True))

    def test_tsmuxer_failure_cleans_up(self):
        Usage = __import__("collections").namedtuple("Usage", "total used free")
        with mock.patch.object(mr, "run_tsmuxer",
                               return_value=tt.MuxResult(255, False, ["Error:", "Bad meta file"])), \
                mock.patch.object(mr.shutil, "disk_usage", return_value=Usage(1, 1, 10 ** 15)):
            with self.assertRaises(mr.RemuxError) as ctx:
                self.remuxer.remux(self.plan, mr.RemuxOptions(accept_metadata_risk=True))
        self.assertIn("Bad meta file", str(ctx.exception))
        self.assertEqual([p.name for p in self.tmp.iterdir()], [self.source.name])

    def test_stale_partials(self):
        partial = self.tmp / f".Blade Runner 2049 DV{mr.PARTIAL_INFIX}abc.m2ts"
        partial.write_bytes(b"")
        self.assertEqual(mr.find_stale_partials(self.plan.output), [str(partial)])


class ScannerRuleTests(unittest.TestCase):
    def findings(self, report, devices=("ub450_dlna",)):
        config = cr.ScannerConfig(devices=list(devices))
        return [f for f in cr.evaluate(report, config) if f.rule_id == "ub450.truehd_mkv"]

    def test_truehd_atmos_in_mkv(self):
        report, *_ = blade_runner()
        found = self.findings(report)
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual(f.severity, cr.WARNING)
        self.assertIn(cr.UB450_TRUEHD_MESSAGE, f.detail)
        self.assertIn("Dolby Vision and HDR10+", f.detail)
        self.assertIn("TrueHD Atmos", f.title)
        self.assertEqual(f.actions, [cr.ACTION_M2TS])
        self.assertIn(cr.ACTION_M2TS, cr.recommended_actions(cr.evaluate(report, cr.ScannerConfig())))

    def test_not_for_eac3_joc_or_other_devices_or_mp4(self):
        probe = {"format": {"format_name": "matroska,webm"}, "streams": [
            stream(0, "video", "hevc"), stream(1, "audio", "eac3", channels=6, sample_rate="48000")]}
        report = mi.report_from_probe("/f/joc.mkv", probe)
        report.audio[0].atmos = ai.AtmosAnalysis(ai.STATUS_DDP_ATMOS, True, "high")
        self.assertEqual(self.findings(report), [])
        br, *_ = blade_runner()
        self.assertEqual(self.findings(br, devices=("firetv_plex",)), [])
        mp4, *_ = blade_runner(path="/f/film.mp4")
        self.assertEqual(self.findings(mp4), [])


class QueueJobTests(unittest.TestCase):
    def setUp(self):
        import queue_jobs as qj
        self.qj = qj
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.film = self.tmp / "Film.mkv"
        self.film.write_bytes(b"x")
        self.env = qj.QueueEnv(ffmpeg="ffmpeg", ffprobe="ffprobe", encoder=lambda: None, tools=None,
                               config=cr.ScannerConfig(), cache=None, log=lambda m: None)

    def test_skipped_without_tsmuxer(self):
        from job_queue import SKIPPED
        job = self.qj.m2ts_job(str(self.film))
        self.assertEqual(self.qj.run_m2ts(job, self.env).status, SKIPPED)
        self.env.tsmuxer = lambda: tt.TsMuxerInfo(None, message="tsMuxeR was not found.")
        self.assertIn("not found", self.qj.run_m2ts(job, self.env).message)

    def test_existing_output_skipped_or_renamed(self):
        from job_queue import SKIPPED
        (self.tmp / "Film.m2ts").write_bytes(b"old")
        self.env.tsmuxer = lambda: tt.TsMuxerInfo("/bin/tsMuxeR", status=tt.STATUS_READY)
        skip = self.qj.m2ts_job(str(self.film), on_exists="skip")
        self.assertEqual(self.qj.run_m2ts(skip, self.env).status, SKIPPED)
        rename = self.qj.m2ts_job(str(self.film), on_exists="rename")
        with mock.patch.object(self.qj.M2TSRemuxer, "analyse", side_effect=mr.RemuxError("stop")) as analyse:
            self.qj.run_m2ts(rename, self.env)
        self.assertEqual(analyse.call_args[0][1], str(self.tmp / "Film (2).m2ts"))
        self.assertEqual((self.tmp / "Film.m2ts").read_bytes(), b"old")

    def test_job_params_are_json_safe(self):
        import json
        job = self.qj.m2ts_job(str(self.film), output_dir=str(self.tmp), config=cr.ScannerConfig())
        json.dumps(job.params)
        self.assertEqual(job.kind, "m2ts")


if __name__ == "__main__":
    unittest.main()
