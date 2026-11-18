"""Library Scanner integration tests with real FFmpeg-made MKV fixtures.

Needs FFmpeg (DTS2DD_FFMPEG or PATH). FLAC -> TrueHD tests additionally need
LibreMPEG (DTS2DD_LIBREMPEG); the Dolby Vision test needs the FATE sample
dv84.mov (python tests/fetch_samples.py).

The "JOC" fixture is FFmpeg-encoded E-AC-3 with JOC *signalling* inserted
(eac3_fixtures.py) - it is not genuine Atmos material.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import ffmpeg_path, ffprobe_path, librempeg_path, run, sample

import atmos_inspector as ai
import compatibility_rules as cr
import library_repair as lr
import media_inspector as mi
import truehd_encoder as te
from eac3_fixtures import add_joc_to_stream
from library_scanner import LibraryScanner, ScanCache

FFMPEG = ffmpeg_path()
FFPROBE = ffprobe_path()
LIBRE = librempeg_path()
needs_ffmpeg = unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg / ffprobe not available")
needs_libre = unittest.skipUnless(FFMPEG and FFPROBE and LIBRE, "LibreMPEG / FFmpeg not available")

SUB_LANGS = ["eng", "eng", "fre", "ger", "spa", "ita", "dut", "swe", "nor", "dan", "fin", "pol", "cze", "hun",
             "rus", "gre", "tur", "ara", "heb", "jpn", "kor", "chi", "tha", "por", "rum", "bul", "hrv", "", "eng"]


def encode_eac3(workdir: Path, name: str, seconds: float = 6.0) -> Path:
    raw = workdir / f"{name}.eac3"
    run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"sine=d={seconds}:r=48000", "-ac", "6",
         "-c:a", "eac3", "-b:a", "448k", "-y", raw])
    return raw


def build_film(workdir: Path, name: str, audio_inputs, audio_meta, subs=SUB_LANGS, seconds=6.0,
               extra_args=()) -> Path:
    """MKV with H.264 video, the given audio inputs (stream copied or encoded), SRT subtitles,
    two chapters and a font attachment."""
    srt = workdir / "s.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nHello\n", encoding="utf-8")
    chapters = workdir / "chapters.txt"
    chapters.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=3000\ntitle=A\n"
                        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=3000\nEND=6000\ntitle=B\n", encoding="utf-8")
    font = workdir / "font.ttf"
    font.write_bytes(b"not a font")
    cmd = [FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=d={seconds}:s=320x180:r=24"]
    for inp in audio_inputs:
        cmd += inp
    for _ in subs:
        cmd += ["-i", srt]
    cmd += ["-f", "ffmetadata", "-i", chapters, "-map", "0"]
    n_audio = len(audio_inputs)
    for i in range(n_audio):
        cmd += ["-map", str(1 + i)]
    for j in range(len(subs)):
        cmd += ["-map", str(1 + n_audio + j)]
    cmd += ["-map_chapters", str(1 + n_audio + len(subs)), "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "copy", "-c:s", "srt"]
    cmd += list(extra_args)
    for i, meta in enumerate(audio_meta):
        for key, value in meta.items():
            if key == "disposition":
                cmd += [f"-disposition:a:{i}", value]
            else:
                cmd += [f"-metadata:s:a:{i}", f"{key}={value}"]
    for j, lang in enumerate(subs):
        if lang:
            cmd += [f"-metadata:s:s:{j}", f"language={lang}"]
    cmd += ["-disposition:s:1", "forced", "-metadata:s:s:28", "title=English SDH",
            "-attach", font, "-metadata:s:t", "mimetype=font/ttf", "-t", str(seconds),
            "-y", workdir / name]
    run(cmd)
    return workdir / name


@needs_ffmpeg
class ScannerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="dts2dd-library-"))
        work = cls.root / ".work"
        work.mkdir()
        plain = encode_eac3(work, "plain")
        joc = work / "joc.eac3"
        joc.write_bytes(add_joc_to_stream(plain.read_bytes(), complexity=16))
        (cls.root / "Movies").mkdir()
        cls.joc_film = build_film(
            cls.root / "Movies", "Spectre (2015).mkv",
            [["-f", "eac3", "-i", joc], ["-f", "eac3", "-i", plain]],
            [{"language": "eng", "title": "DD+ Atmos", "disposition": "default"},
             {"language": "eng", "title": "Atmos", "disposition": "default"}])
        thd = work / "thd.mkv"
        run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=d=6:r=48000", "-af", "pan=5.1(side)|c0=c0",
             "-c:a", "truehd", "-strict", "experimental", "-y", thd])
        cls.truehd_film = build_film(
            cls.root / "Movies", "TrueHD Film.mkv",
            [["-i", thd]], [{"language": "eng", "title": "TrueHD Atmos 5.1", "disposition": "default"}],
            subs=SUB_LANGS[:2] + [""] * 27)
        cls.cache = ScanCache(cls.root / ".work" / "cache.json")
        cls.tools = mi.discover_tools(FFMPEG, FFPROBE)
        cls.tools.mediainfo = cls.tools.mkvmerge = None      # deterministic: ffprobe + bitstream only
        cls.items = {}
        LibraryScanner(cls.tools, cr.ScannerConfig(), cls.cache).scan(
            str(cls.root), lambda item: cls.items.__setitem__(Path(item.path).name, item))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_recursive_scan_found_films(self):
        self.assertEqual(sorted(self.items), ["Spectre (2015).mkv", "TrueHD Film.mkv"])

    def test_eac3_joc_confirmed_by_parser_and_ffprobe(self):
        a = self.items["Spectre (2015).mkv"].report.audio[0]
        self.assertEqual(a.profile, "Dolby Digital Plus + Dolby Atmos")    # FFmpeg agrees with our parser
        self.assertEqual(a.atmos.status, ai.STATUS_DDP_ATMOS)
        self.assertEqual(a.atmos.confidence, "high")
        self.assertEqual(a.atmos.object_count, 16)
        self.assertGreaterEqual(a.bitstream["frames"], ai.MIN_FRAMES_FOR_ABSENCE)

    def test_eac3_labelled_atmos_without_joc(self):
        item = self.items["Spectre (2015).mkv"]
        a = item.report.audio[1]
        self.assertEqual(a.atmos.status, ai.STATUS_DDP_CONVENTIONAL)
        titles = [f.title for f in item.findings if f.rule_id == "atmos.misleading_title"]
        self.assertTrue(any(f"Audio #{a.index}" in t for t in titles))

    def test_fire_tv_warning_does_not_call_file_defective(self):
        item = self.items["Spectre (2015).mkv"]
        fire = [f for f in item.findings if f.rule_id == "firetv.eac3_joc"]
        self.assertEqual(len(fire), 1)
        self.assertEqual(fire[0].severity, cr.WARNING)
        self.assertFalse([f for f in item.findings if f.severity == cr.ERROR])

    def test_truehd_without_atmos(self):
        item = self.items["TrueHD Film.mkv"]
        a = item.report.audio[0]
        self.assertEqual(a.atmos.status, ai.STATUS_TRUEHD_NO_ATMOS)
        self.assertTrue(any(f.rule_id == "atmos.misleading_title" for f in item.findings))

    def test_subtitle_findings(self):
        item = self.items["Spectre (2015).mkv"]
        ids = {f.rule_id for f in item.findings}
        self.assertTrue({"subs.excessive", "subs.non_english", "subs.missing_language", "meta.default_audio"} <= ids)
        self.assertEqual(len([f for f in item.findings if f.rule_id == "subs.missing_language"]), 1)
        self.assertEqual(len([f for f in item.findings if f.rule_id == "subs.missing_language"][0].evidence), 1)

    def test_rescan_uses_cache(self):
        summary = LibraryScanner(self.tools, cr.ScannerConfig(), ScanCache(self.cache.path)).scan(
            str(self.root), lambda i: None)
        self.assertEqual((summary.cached, summary.scanned), (2, 0))


@needs_ffmpeg
class RepairIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dts2dd-repair-"))
        self.tools = mi.ToolSet(FFMPEG, FFPROBE)
        self.cache = ScanCache(self.dir / "cache.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def film(self, name):
        plain = encode_eac3(self.dir, name)
        return build_film(self.dir, f"{name}.mkv",
                          [["-f", "eac3", "-i", plain], ["-f", "eac3", "-i", plain]],
                          [{"language": "eng", "disposition": "default"}, {"language": "fre", "disposition": "default"}])

    def scan(self, path):
        return LibraryScanner(self.tools, cr.ScannerConfig(), self.cache).inspect_one(str(path), use_cache=False)

    def ctx(self, **kw):
        return lr.RepairContext(FFMPEG, FFPROBE, None, lr.RepairOptions(**kw), log=lambda m: None)

    def probe(self, path):
        from media_probe import run_ffprobe
        return run_ffprobe(FFPROBE, str(path))

    def test_batch_subtitle_cleanup_and_default_fix(self):
        films = [self.film("One"), self.film("Two")]
        before = {f: self.probe(f) for f in films}
        video_before = {f: helpers.run([FFMPEG, "-v", "error", "-i", f, "-map", "0:v", "-c", "copy",
                                        "-f", "framemd5", "-"]).stdout for f in films}
        plans = [lr.plan_repair(self.scan(f), [cr.ACTION_CLEAN_SUBS, cr.ACTION_FIX_DEFAULT], lr.RepairOptions(),
                                cr.ScannerConfig(), None) for f in films]
        self.assertTrue(all(p.method == "remux" for p in plans))
        report = lr.run_batch(plans, self.ctx())
        self.assertEqual(report.count(lr.STATUS_REPAIRED), 2, report.text())
        for f in films:
            out = self.probe(f)
            subs = [s for s in out["streams"] if s["codec_type"] == "subtitle"]
            # English regular + forced + SDH kept; the untagged track kept (not confirmed); others removed
            self.assertEqual([(s["tags"].get("language"), s["disposition"]["forced"]) for s in subs],
                             [("eng", 0), ("eng", 1), (None, 0), ("eng", 0)])
            self.assertEqual(subs[3]["tags"].get("title"), "English SDH")
            audio = [s for s in out["streams"] if s["codec_type"] == "audio"]
            self.assertEqual([a["disposition"]["default"] for a in audio], [1, 0])
            self.assertEqual(len(out["chapters"]), len(before[f]["chapters"]))
            self.assertEqual(len([s for s in out["streams"] if s["codec_type"] == "attachment"]), 1)
            video_after = helpers.run([FFMPEG, "-v", "error", "-i", f, "-map", "0:v", "-c", "copy",
                                       "-f", "framemd5", "-"]).stdout
            self.assertEqual(video_after.splitlines()[-50:], video_before[f].splitlines()[-50:])
            rescanned = self.scan(f)
            self.assertFalse([x for x in rescanned.findings if x.rule_id in ("subs.excessive", "meta.default_audio")])

    def test_existing_track_removal_keeps_default_flags(self):
        """Regression: Remove Ticked Tracks must not mark the first remaining subtitle default."""
        import ffmpeg_helper
        f = self.film("Seven")
        ffmpeg_helper.remove_streams_in_place(str(f), FFMPEG, list(range(5, 31)))
        subs = [s for s in self.probe(f)["streams"] if s["codec_type"] == "subtitle"]
        self.assertEqual([s["disposition"]["default"] for s in subs], [0, 0, 0])

    def test_confirmed_unlabelled_removal(self):
        f = self.film("Three")
        plan = lr.plan_repair(self.scan(f), [cr.ACTION_CLEAN_SUBS], lr.RepairOptions(remove_unlabelled_subs=True),
                              cr.ScannerConfig(), None)
        self.assertEqual(lr.execute_plan(plan, self.ctx()).status, lr.STATUS_REPAIRED)
        langs = [s["tags"].get("language") for s in self.probe(f)["streams"] if s["codec_type"] == "subtitle"]
        self.assertEqual(langs, ["eng", "eng", "eng"])

    def test_validation_failure_leaves_original(self):
        f = self.film("Four")
        digest = helpers.sha256(f)
        plan = lr.plan_repair(self.scan(f), [cr.ACTION_CLEAN_SUBS], lr.RepairOptions(), cr.ScannerConfig(), None)
        with mock.patch.object(lr, "validate_output", side_effect=RuntimeError("Validation failed - test")):
            outcome = lr.execute_plan(plan, self.ctx())
        self.assertEqual(outcome.status, lr.STATUS_FAILED)
        self.assertEqual(helpers.sha256(f), digest)
        self.assertEqual([p.name for p in self.dir.iterdir() if "converting" in p.name], [])

    def test_ffmpeg_failure_leaves_original(self):
        f = self.film("Five")
        digest = helpers.sha256(f)
        plan = lr.plan_repair(self.scan(f), [cr.ACTION_CLEAN_SUBS], lr.RepairOptions(), cr.ScannerConfig(), None)
        import ffmpeg_helper
        real = ffmpeg_helper.build_remux_command
        broken = lambda *a, **k: real(*a, **k)[:-1] + ["-no_such_option", "1", real(*a, **k)[-1]]
        with mock.patch.object(ffmpeg_helper, "build_remux_command", broken):
            outcome = lr.execute_plan(plan, self.ctx())
        self.assertIn("failed", outcome.message)
        self.assertEqual(outcome.status, lr.STATUS_FAILED)
        self.assertEqual(helpers.sha256(f), digest)

    def test_cancel_leaves_original(self):
        f = self.film("Six")
        digest = helpers.sha256(f)
        plan = lr.plan_repair(self.scan(f), [cr.ACTION_CLEAN_SUBS], lr.RepairOptions(), cr.ScannerConfig(), None)
        ctx = self.ctx()
        ctx.handle.cancel()
        outcome = lr.execute_plan(plan, ctx)
        self.assertEqual(outcome.status, lr.STATUS_CANCELLED)
        self.assertEqual(helpers.sha256(f), digest)

    def test_dts_to_eac3_with_cleanup_in_one_pass(self):
        dts = self.dir / "core.dts"
        src = sample("core_51_24_48_768_1.dtshd")
        if not src:
            self.skipTest("FATE DTS sample missing")
        helpers.repeat_dts(FFMPEG, src, dts, 400)
        f = build_film(self.dir, "Dts.mkv", [["-i", dts]], [{"language": "eng", "title": "DTS 5.1",
                                                              "disposition": "default"}])
        plan = lr.plan_repair(self.scan(f), [cr.ACTION_DTS_CONVERT, cr.ACTION_CLEAN_SUBS], lr.RepairOptions(),
                              cr.ScannerConfig(), None)
        self.assertEqual((plan.method, plan.audio_job.kind), ("lossy", "eac3"))
        outcome = lr.execute_plan(plan, self.ctx())
        self.assertEqual(outcome.status, lr.STATUS_REPAIRED, outcome.message)
        audio = [s for s in self.probe(f)["streams"] if s["codec_type"] == "audio"]
        self.assertEqual([(a["codec_name"], a["disposition"]["default"], a["tags"].get("title")) for a in audio],
                         [("dts", 0, "DTS 5.1"), ("eac3", 1, "Dolby Digital Plus 5.1")])


@needs_ffmpeg
class DolbyVisionIntegrationTests(unittest.TestCase):
    def test_dolby_vision_mkv_detected(self):
        src = sample("dv84.mov")
        if not src:
            self.skipTest("dv84.mov missing (python tests/fetch_samples.py)")
        with tempfile.TemporaryDirectory() as d:
            mkv = Path(d) / "DV.mkv"
            run([FFMPEG, "-v", "error", "-i", src, "-map", "0:v", "-c", "copy", "-y", mkv])
            report = mi.inspect_file(str(mkv), mi.ToolSet(FFMPEG, FFPROBE))
            v = report.video[0]
            self.assertEqual((v.dv_profile, v.dv_compat_id), (8, 4))
            self.assertIn("Dolby Vision", v.hdr_formats)
            self.assertIn("HLG", v.hdr_formats)
            item_findings = cr.evaluate(report, cr.ScannerConfig())
            self.assertTrue(any(f.rule_id == "hdr.format" for f in item_findings))
            self.assertFalse([f for f in item_findings if f.category == cr.CAT_DV and f.severity == cr.ERROR])


@needs_libre
class FlacTrueHDIntegrationTests(unittest.TestCase):
    """FLAC 2.0 / 5.1 / 7.1 -> TrueHD through the scanner repair path, with real LibreMPEG."""

    @classmethod
    def setUpClass(cls):
        cls.encoder = te.check_encoder(LIBRE, reference_decoder=FFMPEG)
        if not cls.encoder.is_usable:
            raise unittest.SkipTest(f"LibreMPEG not verified: {cls.encoder.message}")

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dts2dd-flac-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def flac_film(self, layout, channels, bits):
        pcm = te.synth_labelled_pcm(channels, 48000 * 5 + 17, bits)
        raw = self.dir / f"{layout}.raw"
        raw.write_bytes(te._to_little_endian_bytes(pcm))
        flac = self.dir / f"{layout}.mka"
        run([FFMPEG, "-v", "error", "-f", "s32le", "-ar", "48000", "-ch_layout", layout, "-i", raw,
             "-c:a", "flac", "-sample_fmt", "s32" if bits == 24 else "s16", "-y", flac])
        return build_film(self.dir, f"Film {layout}.mkv", [["-i", flac]],
                          [{"language": "eng", "disposition": "default"}], seconds=5.0)

    def decode(self, path, audio_index):
        data = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", f"0:a:{audio_index}",
                               "-c:a", "pcm_s32le", "-f", "s32le", "-"], capture_output=True, check=True).stdout
        return te._from_little_endian_bytes(data)

    def test_flac_layouts_to_truehd_channel_mapping(self):
        tools = mi.ToolSet(FFMPEG, FFPROBE)
        for layout, channels, bits, target in (("stereo", 2, 16, "stereo"), ("5.1", 6, 24, "5.1(side)"),
                                               ("7.1", 8, 24, "7.1")):
            with self.subTest(layout=layout):
                f = self.flac_film(layout, channels, bits)
                item = LibraryScanner(tools, cr.ScannerConfig(), ScanCache(self.dir / "c.json")).inspect_one(str(f))
                plan = lr.plan_repair(item, [cr.ACTION_FLAC_TRUEHD, cr.ACTION_CLEAN_SUBS], lr.RepairOptions(),
                                      cr.ScannerConfig(), self.encoder)
                self.assertEqual(plan.method, "truehd", plan.describe())
                ctx = lr.RepairContext(FFMPEG, FFPROBE, self.encoder, lr.RepairOptions(), log=lambda m: None)
                outcome = lr.execute_plan(plan, ctx)
                self.assertEqual(outcome.status, lr.STATUS_REPAIRED, outcome.message)
                self.assertEqual(outcome.final_path, str(f))         # replaced after verification
                from media_probe import run_ffprobe
                out = run_ffprobe(FFPROBE, str(f))
                audio = [s for s in out["streams"] if s["codec_type"] == "audio"]
                self.assertEqual([a["codec_name"] for a in audio], ["flac", "truehd"])   # FLAC kept
                self.assertEqual(audio[1]["channel_layout"], target)
                self.assertEqual([a["disposition"]["default"] for a in audio], [0, 1])
                self.assertNotIn("atmos", (audio[1].get("profile") or "").lower())
                subs = [s for s in out["streams"] if s["codec_type"] == "subtitle"]
                self.assertEqual(len(subs), 4)                         # cleaned in the same pass
                flac_pcm, thd_pcm = self.decode(f, 0), self.decode(f, 1)
                self.assertEqual(len(flac_pcm), len(thd_pcm))
                mapping = te.identify_channels(flac_pcm, thd_pcm, channels)
                self.assertEqual(mapping, [[c] for c in range(channels)])
                self.assertEqual(flac_pcm, thd_pcm)


if __name__ == "__main__":
    unittest.main()
