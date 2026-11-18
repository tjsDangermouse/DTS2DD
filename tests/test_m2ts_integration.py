"""MKV -> M2TS remux integration tests on real (small) files.

Most tests drive the whole workflow through tests/fake_tsmuxer.py, which
prints tsMuxeR's output format but writes the M2TS with FFmpeg. They test
this app (tool discovery, meta file, progress, cancel, cleanup, overwrite
rules, verification) - NOT tsMuxeR itself. Dolby Vision / Atmos
preservation by the real tsMuxeR is only exercised by
``RealTsMuxerTests``, which runs when DTS2DD_TSMUXER points at a tsMuxeR
executable.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import ffmpeg_path, ffprobe_path, run, sample, sha256

import m2ts_remux as mr
import tsmuxer_tool as tt
from ffmpeg_helper import ConversionCancelled, ProcessHandle
from media_inspector import ToolSet

FFMPEG = ffmpeg_path()
FFPROBE = ffprobe_path()
FAKE = Path(__file__).resolve().parent / "fake_tsmuxer.py"


def has_encoder(name):
    if not FFMPEG:
        return False
    result = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True)
    return f" {name} " in result.stdout


def make_fake_tool(folder: Path) -> str:
    """An executable wrapper around fake_tsmuxer.py (shell script / .bat)."""
    if os.name == "nt":
        path = folder / "tsMuxeR.bat"
        path.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
    else:
        path = folder / "tsMuxeR"
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def build_source(workdir: Path, name="Blade Runner 2049 DV – Ünïcode test.mkv", video="dv", seconds=8.0):
    """MKV with HEVC (Dolby Vision 8.4 sample or x265 HDR10), TrueHD 5.1, AC-3 5.1, DTS-HD MA 5.1,
    E-AC-3, FLAC (unsupported), SRT and ASS subtitles, chapters and an attachment."""
    srt = workdir / "subs.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nHello\n\n", encoding="utf-8")
    ass = workdir / "subs.ass"
    ass.write_text("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
                   "Style: Default,Arial,20\n\n[Events]\nFormat: Layer, Start, End, Style, Text\n"
                   "Dialogue: 0,0:00:00.50,0:00:02.00,Default,Hello\n", encoding="utf-8")
    chapters = workdir / "chapters.txt"
    chapters.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=4000\ntitle=One\n",
                        encoding="utf-8")
    dts = workdir / "audio.dts"
    helpers.repeat_dts(FFMPEG, sample("xll_51_24_48_768.dtshd"), dts, 400)
    font = workdir / "font.ttf"
    font.write_bytes(b"not really a font")
    out = workdir / name
    cmd = [FFMPEG, "-v", "error"]
    if video == "dv":
        cmd += ["-stream_loop", "3", "-i", sample("dv84.mov")]
    else:
        cmd += ["-f", "lavfi", "-i", f"testsrc2=s=640x360:r=24000/1001:d={seconds}"]
    cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=48000",
            "-i", dts, "-i", srt, "-i", ass, "-f", "ffmetadata", "-i", chapters]
    six = "pan=5.1(side)|c0=c0|c1=0.5*c0|c2=c0|c3=0.3*c0|c4=0.2*c0|c5=0.1*c0"
    cmd += ["-filter_complex", f"[1:a]{six},asplit=4[a][b][c][d]",
            "-map", "0:v:0", "-map", "[a]", "-map", "[b]", "-map", "2:a", "-map", "[c]", "-map", "[d]",
            "-map", "3", "-map", "4", "-map_chapters", "5"]
    if video == "dv":
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-x265-params",
                "log-level=error:hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:"
                "colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
                "WP(15635,16450)L(10000000,1):max-cll=1000,400",
                "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"]
    cmd += ["-c:a:0", "truehd", "-strict", "-2", "-c:a:1", "ac3", "-b:a:1", "448k", "-c:a:2", "copy",
            "-c:a:3", "eac3", "-b:a:3", "384k", "-c:a:4", "flac", "-c:s:0", "srt", "-c:s:1", "ass",
            "-t", str(seconds),
            "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=TrueHD 5.1",
            "-metadata:s:a:1", "language=eng", "-metadata:s:a:2", "language=eng",
            "-metadata:s:a:3", "language=fre", "-metadata:s:a:4", "language=eng",
            "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng",
            "-disposition:a:0", "default", "-disposition:a:1", "0",
            "-attach", font, "-metadata:s:t", "mimetype=font/ttf", "-y", out]
    run(cmd)
    return out


@unittest.skipUnless(FFMPEG and FFPROBE and sample("dv84.mov") and sample("xll_51_24_48_768.dtshd"),
                     "needs FFmpeg/ffprobe and the FATE samples (python tests/fetch_samples.py)")
class FakeTsMuxerWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dts2dd m2ts "))
        cls.source = build_source(cls.tmp)
        cls.source_hash = sha256(cls.source)
        cls.tool = make_fake_tool(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        env = {"FAKE_TSMUXER_FFMPEG": FFMPEG, "FAKE_TSMUXER_FFPROBE": FFPROBE}
        for key in ("FAKE_TSMUXER_FAIL", "FAKE_TSMUXER_SLOW", "FAKE_TSMUXER_ALTER_AUDIO", "FAKE_TSMUXER_ATMOS"):
            os.environ.pop(key, None)
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.info = tt.check_tsmuxer(self.tool)
        self.assertTrue(self.info.ok, self.info.message)
        self.out_dir = Path(tempfile.mkdtemp(dir=self.tmp, prefix="out "))
        self.logs = []

    def remuxer(self, handle=None, progress=None):
        return mr.M2TSRemuxer(self.info, FFMPEG, FFPROBE, ToolSet(FFMPEG, FFPROBE), log=self.logs.append,
                              progress=progress or (lambda p, f, d: None), handle=handle)

    def plan(self, **kw):
        out = self.out_dir / "Blade Runner 2049 DV – Ünïcode test.m2ts"
        return self.remuxer().analyse(str(self.source), str(out), **kw)

    def assert_source_untouched(self):
        self.assertEqual(sha256(self.source), self.source_hash)

    def assert_no_partials(self):
        self.assertEqual([p.name for p in self.out_dir.iterdir() if p.name.startswith(".")], [])

    def test_version_and_detection(self):
        self.assertEqual(self.info.version, (2, 7, 0))
        det = tt.detect_tracks(self.tool, str(self.source))
        self.assertEqual([t.codec_id for t in det.tracks][:4], ["V_MPEGH/ISO/HEVC", "A_MLP", "A_AC3", "A_DTS"])
        flac = det.track(6)
        self.assertFalse(flac.detected)            # FLAC: "Can't detect stream type"

    def test_plan_marks_unsupported_tracks(self):
        plan = self.plan()
        by_codec = {t.codec: t for t in plan.tracks}
        self.assertFalse(by_codec["flac"].compatible)
        self.assertFalse(by_codec["ass"].compatible)
        self.assertIn("ASS/SSA", by_codec["ass"].handling)
        self.assertTrue(by_codec["subrip"].compatible)
        self.assertFalse(by_codec["subrip"].selected)          # SRT rendering is opt-in
        self.assertEqual([t.codec for t in plan.ordered_audio()], ["truehd", "ac3", "dts", "eac3"])
        self.assertEqual(plan.first_audio, by_codec["truehd"].index)   # the MKV's default track
        self.assertTrue(plan.metadata_warnings)                 # Dolby Vision source
        self.assertTrue(any("chapters" in i for i in plan.info))

    def test_full_remux_verifies_and_keeps_source(self):
        plan = self.plan()
        progress = []
        result = self.remuxer(progress=lambda p, f, d: progress.append((p, f))).remux(
            plan, mr.RemuxOptions(accept_metadata_risk=True, full_audio_compare=True))
        self.assertTrue(os.path.isfile(result.output))
        self.assertGreater(os.path.getsize(result.output), 100_000)
        self.assert_source_untouched()
        self.assert_no_partials()
        self.assertTrue(result.source_unchanged)
        v = result.verification
        self.assertEqual(v.failures, [], [c.detail for c in v.failures])
        # TrueHD, AC-3, DTS-HD MA, E-AC-3 compared byte for byte.
        self.assertEqual(set(v.audio_identical.values()), {True})
        self.assertIn(mr.OUTCOME_AUDIO_VERIFIED, result.outcomes)
        self.assertIn(mr.OUTCOME_PANASONIC, result.outcomes)
        frames = next(c for c in v.checks if c.name == "Video frames")
        self.assertEqual(frames.status, mr.OK, frames.detail)
        # FFmpeg (the fake) writes no Dolby Vision descriptor: must be reported, not hidden.
        self.assertIn(mr.OUTCOME_HDR_CHANGED, result.outcomes)
        self.assertTrue(any("Dolby Vision configuration" in l and "-> none" in l for l in v.hdr_lines), v.hdr_lines)
        self.assertTrue(any("Dolby Vision RPU" in l and "-> present" in l for l in v.hdr_lines), v.hdr_lines)
        self.assertIn("TrueHD", result.report)
        self.assertIn("flac", result.report.lower())
        self.assertTrue(any(f is not None for _, f in progress))       # tsMuxeR % progress was parsed

    def test_hdr10_source_reports_unchanged_metadata(self):
        if not has_encoder("libx265"):
            self.skipTest("needs FFmpeg with libx265")
        src = build_source(self.out_dir, "HDR10 film.mkv", video="hdr10", seconds=4.0)
        plan = self.remuxer().analyse(str(src), str(self.out_dir / "HDR10 film.m2ts"))
        self.assertIn("mastering", plan.source_hdr.frame_keys)
        result = self.remuxer().remux(plan, mr.RemuxOptions(full_audio_compare=False))
        self.assertIn(mr.OUTCOME_HDR_SAME, result.outcomes, result.verification.hdr_lines)

    def test_remux_failure_removes_partial_output(self):
        os.environ["FAKE_TSMUXER_FAIL"] = "1"
        plan = self.plan()
        with self.assertRaises(mr.RemuxError) as ctx:
            self.remuxer().remux(plan, mr.RemuxOptions(accept_metadata_risk=True))
        self.assertIn("Fake failure", str(ctx.exception))
        self.assertFalse(os.path.exists(plan.output))
        self.assert_no_partials()
        self.assert_source_untouched()

    @unittest.skipIf(os.name == "nt", "the .bat stand-in runs Python as a child process that a cancel "
                                      "does not stop; the real tsMuxeR.exe is a single process")
    def test_cancellation_removes_partial_output(self):
        os.environ["FAKE_TSMUXER_SLOW"] = "1"
        plan = self.plan()
        handle = ProcessHandle()
        seen = threading.Event()

        def progress(phase, fraction, detail):
            if fraction is not None:
                seen.set()
        errors = []

        def worker():
            try:
                self.remuxer(handle, progress).remux(plan, mr.RemuxOptions(accept_metadata_risk=True))
            except Exception as e:      # noqa: BLE001
                errors.append(e)
        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(seen.wait(30), "no progress from the fake tsMuxeR")
        handle.cancel()
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertIsInstance(errors[0], ConversionCancelled)
        self.assert_no_partials()
        self.assertFalse(os.path.exists(plan.output))

    def test_existing_output_is_never_overwritten_without_approval(self):
        plan = self.plan()
        Path(plan.output).write_bytes(b"keep me")
        with self.assertRaises(mr.OutputExists):
            self.remuxer().remux(plan, mr.RemuxOptions(accept_metadata_risk=True))
        self.assertEqual(Path(plan.output).read_bytes(), b"keep me")
        self.assert_no_partials()
        result = self.remuxer().remux(plan, mr.RemuxOptions(accept_metadata_risk=True, overwrite=True,
                                                            full_audio_compare=False))
        self.assertNotEqual(Path(result.output).read_bytes()[:7], b"keep me")
        self.assertEqual(mr.unique_output(plan.output), str(Path(plan.output).with_name(
            Path(plan.output).stem + " (2).m2ts")))

    def test_metadata_risk_needs_confirmation(self):
        with self.assertRaises(mr.RemuxError):
            self.remuxer().remux(self.plan(), mr.RemuxOptions(accept_metadata_risk=False))

    def test_altered_audio_fails_verification_and_is_deleted(self):
        os.environ["FAKE_TSMUXER_ALTER_AUDIO"] = "1"
        plan = self.plan()
        with self.assertRaises(mr.VerificationFailed) as ctx:
            self.remuxer().remux(plan, mr.RemuxOptions(accept_metadata_risk=True, full_audio_compare=False))
        self.assertIn("bitstream", str(ctx.exception))
        self.assertIn("VERIFICATION FAILED", ctx.exception.report)
        self.assertFalse(os.path.exists(plan.output))
        self.assert_no_partials()

    def test_selected_tracks_and_order_reach_the_meta_file(self):
        copy = self.out_dir / "received.meta"
        os.environ["FAKE_TSMUXER_META_COPY"] = str(copy)
        self.addCleanup(os.environ.pop, "FAKE_TSMUXER_META_COPY", None)
        plan = self.plan()
        ac3 = next(t for t in plan.audio if t.codec == "ac3")
        for t in plan.audio:
            t.selected = t.codec in ("truehd", "ac3")
        plan.first_audio = ac3.index
        result = self.remuxer().remux(plan, mr.RemuxOptions(accept_metadata_risk=True, full_audio_compare=False))
        lines = copy.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[0].startswith("MUXOPT"))
        self.assertEqual([l.split(",")[0] for l in lines[1:]], ["V_MPEGH/ISO/HEVC", "A_AC3", "A_MLP"])
        self.assertIn(f'"{self.source}"', lines[1])
        self.assertEqual(len([c for c in result.verification.checks if c.name.startswith("Audio #")
                              and "bitstream" not in c.name and "Atmos" not in c.name]), 2)

    def test_queue_job_runs_and_skips_existing(self):
        import queue_jobs as qj
        from compatibility_rules import ScannerConfig
        from job_queue import DONE, SKIPPED
        env = qj.QueueEnv(ffmpeg=FFMPEG, ffprobe=FFPROBE, encoder=lambda: None, tools=ToolSet(FFMPEG, FFPROBE),
                          config=ScannerConfig(), cache=None, log=self.logs.append, tsmuxer=lambda: self.info)
        job = qj.m2ts_job(str(self.source), output_dir=str(self.out_dir), accept_metadata_risk=True,
                          full_verify=False)
        result = qj.run_m2ts(job, env)
        self.assertEqual(result.status, DONE, result.message)
        self.assertTrue(os.path.isfile(result.final_path))
        again = qj.run_m2ts(job, env)
        self.assertEqual(again.status, SKIPPED)
        unconfirmed = qj.m2ts_job(str(self.source), output_dir=str(self.out_dir), on_exists="rename")
        self.assertEqual(qj.run_m2ts(unconfirmed, env).status, SKIPPED)     # DV risk not confirmed
        self.assert_source_untouched()


@unittest.skipUnless(os.environ.get("DTS2DD_TSMUXER") and FFMPEG and FFPROBE and sample("dv84.mov")
                     and sample("xll_51_24_48_768.dtshd"), "set DTS2DD_TSMUXER to a real tsMuxeR to run")
class RealTsMuxerTests(unittest.TestCase):
    """The real tsMuxeR on a synthetic MKV (Dolby Vision 8.4 sample video, TrueHD, AC-3, DTS-HD MA)."""

    def test_real_remux(self):
        tmp = Path(tempfile.mkdtemp(prefix="dts2dd real tsmuxer "))
        self.addCleanup(shutil.rmtree, tmp, True)
        source = build_source(tmp)
        before = sha256(source)
        info = tt.check_tsmuxer(os.environ["DTS2DD_TSMUXER"])
        self.assertTrue(info.ok, info.message)
        remuxer = mr.M2TSRemuxer(info, FFMPEG, FFPROBE, ToolSet(FFMPEG, FFPROBE), log=print)
        plan = remuxer.analyse(str(source), str(tmp / "out.m2ts"))
        result = remuxer.remux(plan, mr.RemuxOptions(accept_metadata_risk=True))
        print(result.report)
        self.assertEqual(result.verification.failures, [])
        self.assertEqual(sha256(source), before)
        self.assertIn(mr.OUTCOME_AUDIO_VERIFIED, result.outcomes)


@unittest.skipUnless(FFMPEG, "needs FFmpeg")
class MissingToolTests(unittest.TestCase):
    def test_missing_tsmuxer(self):
        with mock.patch.object(tt, "candidates", return_value=[("/nowhere/tsMuxeR", "configured")]):
            info = tt.check_tsmuxer("/nowhere/tsMuxeR")
        self.assertEqual(info.status, tt.STATUS_MISSING)
        self.assertIn("does not exist", info.message)
        self.assertIn(tt.OFFICIAL_RELEASES_URL, info.message)
        remuxer = mr.M2TSRemuxer(info, FFMPEG, FFPROBE)
        with self.assertRaises(tt.TsMuxerError):
            remuxer.analyse(__file__)

    def test_wrong_program_is_rejected(self):
        info = tt.check_tsmuxer(FFMPEG)        # runs, but prints no tsMuxeR banner
        self.assertEqual(info.status, tt.STATUS_BROKEN)


if __name__ == "__main__":
    unittest.main()
