"""Integration tests with real executables and real DTS-HD MA / Dolby Vision samples.

Requires LibreMPEG (DTS2DD_LIBREMPEG), standard FFmpeg + ffprobe, and the
FATE samples (python tests/fetch_samples.py). Tests skip when unavailable.
These do NOT test Plex, Fire TV or AV-receiver behaviour - see USER_GUIDE.md.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import build_fixture, ffmpeg_path, ffprobe_path, librempeg_path, sample, sha256

import ffmpeg_helper
import media_probe
import truehd_encoder as te
import truehd_workflow as tw
from ffmpeg_helper import FFmpegResult

LIBRE = librempeg_path()
FFMPEG = ffmpeg_path()
FFPROBE = ffprobe_path()
HAVE_TOOLS = bool(LIBRE and FFMPEG and FFPROBE)
HAVE_SAMPLES = all(sample(n) for n in ("xll_71_24_48_768_0.dtshd", "xll_51_24_48_768.dtshd", "dv84.mov"))

needs_tools = unittest.skipUnless(HAVE_TOOLS, "LibreMPEG / FFmpeg / ffprobe not available")
needs_samples = unittest.skipUnless(HAVE_TOOLS and HAVE_SAMPLES,
                                    "FATE samples missing: run python tests/fetch_samples.py")

_REPORT = None


def encoder_report():
    global _REPORT
    if _REPORT is None:
        _REPORT = te.check_encoder(LIBRE, reference_decoder=FFMPEG)
    return _REPORT


class Workdir(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dts2dd-it-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def probe(self, path):
        return media_probe.run_ffprobe(FFPROBE, str(path))

    def converter(self, report=None, runner=None):
        kw = {"runner": runner} if runner else {}
        return tw.TrueHDConverter(LIBRE, FFPROBE, FFMPEG, report or encoder_report(),
                                  log=lambda m: None, **kw)


@needs_tools
class EncoderSelfTests(Workdir):
    def test_a_b_librempeg_verified_for_51_and_71(self):
        report = encoder_report()
        self.assertEqual(report.status, te.STATUS_READY, report.message)
        self.assertEqual(report.verified_layouts, ["5.1(side)", "7.1"])
        self.assertTrue(all(t.passed for t in report.tests))

    def test_e_standard_ffmpeg_lacks_71(self):
        report = te.check_encoder(FFMPEG)
        self.assertEqual(report.status, te.STATUS_NO_71)
        self.assertFalse(report.can_encode("7.1"))
        # Standard FFmpeg also lacks the side_data filter needed to keep the
        # final samples in MKV, so it is not trusted for 5.1 either.
        self.assertEqual(report.verified_layouts, [])

    def test_d_missing_executable(self):
        with mock.patch.object(te, "bundled_candidates", return_value=[]), \
                mock.patch.object(te.shutil, "which", return_value=None):
            self.assertEqual(te.check_encoder(str(self.dir / "nope.exe")).status, te.STATUS_MISSING)

    def test_f_wrong_channel_mapping_is_detected(self):
        """An encoder that swaps side and rear surrounds must fail the self-test."""
        def swapping_encode(encoder, raw, layout, bits, out):
            fmt = "s16le" if bits == 16 else "s32le"
            remap = ("FL-FL|FR-FR|FC-FC|LFE-LFE|BL-SL|BR-SR|SL-BL|SR-BR" if layout == "7.1"
                     else "FL-FR|FR-FL|FC-FC|LFE-LFE|SL-SL|SR-SR")
            cmd = [encoder, "-hide_banner", "-v", "error", "-f", fmt, "-ar", "48000",
                   "-ch_layout", layout, "-i", raw, "-af",
                   f"channelmap=map={remap}:channel_layout={layout}",
                   "-c:a", "truehd", "-strict", "experimental", "-bsf:a", te.END_PADDING_FIX, "-y", out]
            return subprocess.run(cmd, capture_output=True, text=True)

        with mock.patch.object(te, "encode_test_signal", swapping_encode):
            for layout in te.SUPPORTED_LAYOUTS:
                result = te.run_layout_test(LIBRE, layout, 24, str(self.dir), FFMPEG)
                self.assertFalse(result.passed, layout)
                self.assertIn("channel mapping wrong", result.detail)

    def test_6_1_is_not_bit_exact(self):
        """Documents why 6.1 is excluded even though LibreMPEG accepts it."""
        src = sample("xll_xch_61_24_48_768.dtshd")
        if not src:
            self.skipTest("6.1 sample missing")
        out = self.dir / "x61.mkv"
        helpers.run([LIBRE, "-v", "error", "-i", src, "-c:a", "truehd", "-strict", "experimental",
                     "-ch_layout:a:0", "6.1", "-y", out])
        decode = lambda f: subprocess.run([FFMPEG, "-v", "error", "-i", f, "-f", "s32le", "-"],
                                          capture_output=True).stdout
        self.assertNotEqual(decode(src), decode(out))


@needs_samples
class TrueHDWorkflowTests(Workdir):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = Path(tempfile.mkdtemp(prefix="dts2dd-fixtures-"))
        cls.fixture71 = build_fixture(FFMPEG, cls.fixtures, sample("xll_71_24_48_768_0.dtshd"), "Movie71.mkv")
        sub = cls.fixtures / "51"
        sub.mkdir()
        cls.fixture51 = build_fixture(FFMPEG, sub, sample("xll_51_24_48_768.dtshd"), "Movie51.mkv")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fixtures, ignore_errors=True)

    def copy(self, fixture, name="Spectre.mkv"):
        target = self.dir / name
        shutil.copy2(fixture, target)
        return target

    def convert(self, source, options=None, **kw):
        before = sha256(source)
        result = self.converter(**kw).convert(str(source), 1, options or tw.TrueHDOptions())
        if not result.awaiting_replace_confirmation:
            self.assertEqual(sha256(source), before, "source must not change")
        return result

    def assert_truehd(self, path, layout, channels, audio_index):
        out = self.probe(path)
        audio = media_probe.streams_of_type(out, "audio")
        new = audio[audio_index]
        self.assertEqual((new["codec_name"], new["channel_layout"], new["channels"]),
                         ("truehd", layout, channels))
        self.assertEqual(int(new["sample_rate"]), 48000)
        self.assertEqual(media_probe.tag(new, "language"), "eng")
        self.assertNotIn("Atmos", media_probe.tag(new, "title"))
        sync = te.read_major_sync(LIBRE, str(path), audio_index)
        self.assertIsNone(te.check_channel_assignment(layout, sync))
        return out

    def test_a_dts_hd_ma_51_to_truehd_51(self):
        source = self.copy(self.fixture51)
        result = self.convert(source)
        self.assertTrue(result.passed and result.lossless, result.lossless_detail)
        self.assertEqual(Path(result.final_path).name, "Spectre_Sound_TrueHD.mkv")
        self.assert_truehd(result.final_path, "5.1(side)", 6, 2)

    def test_b_i_l_m_dts_hd_ma_71_to_truehd_71_keeps_everything(self):
        source = self.copy(self.fixture71)
        result = self.convert(source)
        self.assertTrue(result.passed, [c for c in result.checks if not c.passed])
        self.assertTrue(result.lossless, result.lossless_detail)
        src, out = self.probe(source), self.assert_truehd(result.final_path, "7.1", 8, 2)
        # I: original DTS-HD MA retained and still default; new track not default
        audio = media_probe.streams_of_type(out, "audio")
        self.assertEqual([a["codec_name"] for a in audio], ["dts", "ac3", "truehd"])
        self.assertEqual(audio[0].get("profile"), "DTS-HD MA")
        self.assertEqual([a["disposition"]["default"] for a in audio], [1, 0, 0])
        # L: subtitles (with forced flag), chapters, attachments preserved
        subs = media_probe.streams_of_type(out, "subtitle")
        self.assertEqual((len(subs), subs[0]["disposition"]["forced"]), (1, 1))
        self.assertEqual(len(out["chapters"]), len(src["chapters"]))
        self.assertEqual(len(media_probe.streams_of_type(out, "attachment")), 1)
        # M: video stream untouched including Dolby Vision configuration
        v_src = media_probe.streams_of_type(src, "video")[0]
        v_out = media_probe.streams_of_type(out, "video")[0]
        self.assertEqual(media_probe.video_signature(v_src), media_probe.video_signature(v_out))
        dovi = [s for s in v_out.get("side_data_list", []) if s.get("side_data_type") == "DOVI configuration record"]
        self.assertEqual(dovi[0]["dv_profile"], 8)
        self.assertEqual(media_probe.video_packet_hashes(FFMPEG, str(source), max_packets=10000),
                         media_probe.video_packet_hashes(FFMPEG, result.final_path, max_packets=10000))
        self.assertTrue(any(c.name.startswith("Video 1 packet") and c.passed for c in result.checks))

    def test_j_k_remove_original_and_make_default(self):
        source = self.copy(self.fixture71)
        result = self.convert(source, tw.TrueHDOptions(remove_source=True, make_default=True))
        self.assertTrue(result.passed and result.lossless)
        audio = media_probe.streams_of_type(self.probe(result.final_path), "audio")
        self.assertEqual([a["codec_name"] for a in audio], ["ac3", "truehd"])
        self.assertEqual([a["disposition"]["default"] for a in audio], [0, 1])

    def test_k_make_default_keeps_original(self):
        source = self.copy(self.fixture71)
        result = self.convert(source, tw.TrueHDOptions(make_default=True))
        audio = media_probe.streams_of_type(self.probe(result.final_path), "audio")
        self.assertEqual([a["disposition"]["default"] for a in audio], [0, 0, 1])

    def test_p_test_clip(self):
        source = self.copy(self.fixture71)
        result = self.convert(source, tw.TrueHDOptions(test_clip=True, clip_seconds=5, clip_start=4.0))
        self.assertTrue(result.passed and result.lossless, result.lossless_detail)
        self.assertEqual(Path(result.final_path).name, "Spectre_TrueHD_Test_5s.mkv")
        out = self.probe(result.final_path)
        self.assertAlmostEqual(float(out["format"]["duration"]), 5.0, delta=1.0)
        audio = media_probe.streams_of_type(out, "audio")
        # TrueHD starts exactly with the copied original DTS track (A/V sync),
        # and (strict check above) its first sample is the DTS track's first sample.
        self.assertAlmostEqual(float(audio[2]["start_time"]), float(audio[0]["start_time"]), delta=0.002)
        self.assertTrue(any(c.name == "Audio sync with original track" and c.passed for c in result.checks))
        self.assertEqual(tw.find_stale_partials(str(source)), [])

    def test_audio_delay_preserved(self):
        """A source audio track that starts late keeps that delay in TrueHD."""
        delayed = self.dir / "Delayed.mkv"
        helpers.run([FFMPEG, "-v", "error", "-i", self.fixture71, "-itsoffset", "0.5", "-i", self.fixture71,
                     "-map", "0:v", "-map", "1:1", "-c", "copy", delayed])
        result = self.converter().convert(str(delayed), 1, tw.TrueHDOptions())
        self.assertTrue(result.passed and result.lossless)
        audio = media_probe.streams_of_type(self.probe(result.final_path), "audio")
        self.assertAlmostEqual(float(audio[1]["start_time"]), 0.5, delta=0.002)

    def test_final_short_access_unit_kept(self):
        """Lengths that are not a multiple of 40 samples keep every sample in MKV."""
        clip = self.dir / "Odd.mkv"
        # 0.5 s of DTS-HD MA = 24000 samples -> 600 AUs; trim to 497 DTS frames for an odd tail
        helpers.run([FFMPEG, "-v", "error", "-i", self.fixture71, "-map", "0:1", "-c", "copy",
                     "-frames:a", "497", clip])
        result = self.converter().convert(str(clip), 0, tw.TrueHDOptions())
        self.assertTrue(result.lossless, result.lossless_detail)
        decoded = subprocess.run([FFMPEG, "-v", "error", "-i", result.final_path, "-map", "0:a:1",
                                  "-c:a", "pcm_s32le", "-f", "s32le", "-"], capture_output=True).stdout
        self.assertEqual(len(decoded) // 32, 254464)    # 497 DTS frames x 512, none dropped

    def test_replace_original_requires_confirmation_step(self):
        source = self.copy(self.fixture71)
        before = sha256(source)
        result = self.convert(source, tw.TrueHDOptions(replace_original=True))
        self.assertTrue(result.awaiting_replace_confirmation)
        self.assertEqual(sha256(source), before)               # untouched until confirmed
        tw.TrueHDConverter.finalize_replace(result)
        audio = media_probe.streams_of_type(self.probe(source), "audio")
        self.assertEqual(audio[-1]["codec_name"], "truehd")
        self.assertEqual(tw.find_stale_partials(str(source)), [])

    def test_existing_output_is_not_overwritten(self):
        source = self.copy(self.fixture71)
        existing = self.dir / "Spectre_Sound_TrueHD.mkv"
        existing.write_bytes(b"keep me")
        result = self.convert(source)
        self.assertEqual(existing.read_bytes(), b"keep me")
        self.assertEqual(Path(result.final_path).name, "Spectre_Sound_TrueHD (2).mkv")

    def test_c_wide_71_rejected(self):
        wide = sample("master_audio_7.1_24bit.dts")
        if not wide:
            self.skipTest("7.1(wide) sample missing")
        source = self.dir / "Wide.mkv"
        helpers.run([FFMPEG, "-v", "error", "-i", wide, "-c", "copy", "-t", "3", source])
        with self.assertRaises(te.TrueHDError) as ctx:
            self.converter().convert(str(source), 0, tw.TrueHDOptions())
        self.assertIn("7.1(wide)", str(ctx.exception))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["Wide.mkv"])

    def test_lossy_dts_core_rejected(self):
        core = sample("core_51_24_48_768_1.dtshd")
        if not core:
            self.skipTest("core sample missing")
        source = self.dir / "Core.mkv"
        helpers.run([FFMPEG, "-v", "error", "-i", core, "-c", "copy", source])
        with self.assertRaises(te.TrueHDError) as ctx:
            self.converter().convert(str(source), 0, tw.TrueHDOptions())
        self.assertIn("only offered for DTS-HD Master Audio", str(ctx.exception))

    def test_g_failed_encoding(self):
        source = self.copy(self.fixture71)
        fail = lambda cmd, **kw: FFmpegResult(1, ["[truehd] Error while opening encoder"])
        with self.assertRaises(te.TrueHDError) as ctx:
            self.convert(source, runner=fail)
        self.assertIn("Error while opening encoder", str(ctx.exception))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["Spectre.mkv"])

    def test_h_failed_output_verification(self):
        """An output missing the TrueHD track must be rejected and deleted."""
        source = self.copy(self.fixture71)

        def copy_only(cmd, **kw):
            shutil.copyfile(source, cmd[cmd.index("-y") + 1])   # the MKV output
            return FFmpegResult(0, [])

        with self.assertRaises(te.TrueHDError) as ctx:
            self.convert(source, runner=copy_only)
        self.assertIn("verification failed", str(ctx.exception))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["Spectre.mkv"])

    def test_strict_uses_fingerprint_and_independent_decoder(self):
        """The source fingerprint is made during encoding and matches a full decode."""
        source = self.copy(self.fixture71)
        logs = []
        conv = tw.TrueHDConverter(LIBRE, FFPROBE, FFMPEG, encoder_report(), log=logs.append)
        result = conv.convert(str(source), 1, tw.TrueHDOptions())
        self.assertTrue(result.lossless, result.lossless_detail)
        self.assertIn("SHA-256 of all decoded samples identical", result.lossless_detail)
        digest = next(l.split()[-1] for l in logs if l.strip().startswith("SHA256") or l.strip().startswith("SHA-256 "))
        full = subprocess.run([FFMPEG, "-v", "error", "-i", source, "-map", "0:1", "-c:a", "pcm_s32le",
                               "-f", "s32le", "-"], capture_output=True).stdout
        self.assertEqual(digest, hashlib.sha256(full).hexdigest())
        # the verification decode must not read the source film again
        verify_cmds = [l for l in logs if l.startswith("Command:") and "output.sha256" in l]
        self.assertTrue(verify_cmds and all(str(source) not in c for c in verify_cmds))

    def test_fingerprint_mismatch_fails_and_locates(self):
        """A wrong source fingerprint fails verification and reports where it differs."""
        source = self.copy(self.fixture71)
        real_encode = tw.TrueHDConverter._encode

        def encode_then_corrupt(conv, job, total, source_hash_path=None):
            real_encode(conv, job, total, source_hash_path)
            with open(source_hash_path, "w") as f:
                f.write("SHA256=" + "0" * 64 + "\n")

        with mock.patch.object(tw.TrueHDConverter, "_encode", encode_then_corrupt), \
                mock.patch.object(tw.TrueHDConverter, "_locate_difference",
                                  autospec=True, side_effect=lambda *a: None) as locate:
            with self.assertRaises(te.TrueHDError) as ctx:
                self.converter().convert(str(source), 1, tw.TrueHDOptions())
        locate.assert_called_once()
        self.assertIn("Lossless verification failed", str(ctx.exception))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["Spectre.mkv"])

    def test_cancel_removes_temp(self):
        source = self.copy(self.fixture71)
        conv = self.converter()

        def cancelled_runner(cmd, handle=None, **kw):
            handle.cancel()
            raise ffmpeg_helper.ConversionCancelled("cancelled")

        conv.runner = cancelled_runner
        with self.assertRaises(ffmpeg_helper.ConversionCancelled):
            conv.convert(str(source), 1, tw.TrueHDOptions())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["Spectre.mkv"])


@needs_samples
class ExistingWorkflowTests(Workdir):
    """N/O: the original in-place AC-3 / E-AC-3 workflow still works."""

    @classmethod
    def setUpClass(cls):
        cls.fixtures = Path(tempfile.mkdtemp(prefix="dts2dd-fixtures-"))
        cls.fixture = build_fixture(FFMPEG, cls.fixtures, sample("xll_51_24_48_768.dtshd"), "Movie.mkv")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fixtures, ignore_errors=True)

    def run_existing(self, codec, bitrate, remove):
        source = self.dir / "Movie.mkv"
        shutil.copy2(self.fixture, source)
        os.chmod(source, 0o644)
        streams = ffmpeg_helper.get_audio_streams(str(source), FFMPEG)
        self.assertTrue(ffmpeg_helper.convert_and_append_audio(
            str(source), bitrate, FFMPEG, streams[0].stream_index, len(streams),
            codec=codec, remove_source_stream=remove))
        out = self.probe(source)
        if os.name != "nt":   # POSIX permission bits; Windows uses inherited ACLs
            self.assertEqual(os.stat(source).st_mode & 0o777, 0o644, "permissions kept")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["Movie.mkv"])
        return out

    def test_n_dts_to_ac3_keep(self):
        out = self.run_existing("ac3", "640k", False)
        audio = media_probe.streams_of_type(out, "audio")
        self.assertEqual([a["codec_name"] for a in audio], ["dts", "ac3", "ac3"])
        self.assertEqual(int(audio[2]["bit_rate"]), 640000)
        self.assertEqual(len(media_probe.streams_of_type(out, "attachment")), 1)
        self.assertEqual(len(out["chapters"]), 2)

    def test_o_dts_to_eac3_remove(self):
        out = self.run_existing("eac3", "768k", True)
        audio = media_probe.streams_of_type(out, "audio")
        self.assertEqual([a["codec_name"] for a in audio], ["ac3", "eac3"])
        self.assertEqual(len(media_probe.streams_of_type(out, "video")), 1)

    def test_legacy_text_probe_still_works(self):
        source = self.dir / "Movie.mkv"
        shutil.copy2(self.fixture, source)
        streams = ffmpeg_helper._get_audio_streams_text(str(source), FFMPEG)
        self.assertEqual([s.codec_name for s in streams], ["DTS", "AC3"])


@needs_tools
class SpeakerTestFileTests(Workdir):
    def test_speaker_test_file_is_verified(self):
        out = self.dir / "speaker_test.mkv"
        te.create_channel_test_file(LIBRE, str(out), video_ffmpeg=FFMPEG)
        data = self.probe(out)
        kinds = [s["codec_type"] for s in data["streams"]]
        self.assertEqual(sorted(kinds), ["audio", "subtitle", "video"])
        audio = media_probe.streams_of_type(data, "audio")[0]
        self.assertEqual((audio["codec_name"], audio["channel_layout"]), ("truehd", "7.1"))


@unittest.skipUnless(os.environ.get("DTS2DD_TEST_PACKAGING"), "set DTS2DD_TEST_PACKAGING=1 to build with PyInstaller")
class PackagingTests(unittest.TestCase):
    """Q (partial): PyInstaller builds with the new modules. A Windows .exe
    build must still be checked on Windows with build_windows.bat."""

    def test_pyinstaller_build(self):
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run(
                [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", f"{d}/dist",
                 "--workpath", f"{d}/build", "converter.spec"],
                cwd=helpers.ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            warn = Path(d, "build", "converter", "warn-converter.txt")
            self.assertTrue(any(Path(d, "dist").iterdir()), "no build output")
            if warn.exists():
                text = warn.read_text()
                for module in ("truehd_encoder", "truehd_workflow", "media_probe", "app_settings"):
                    self.assertNotIn(f"No module named '{module}'", text)


if __name__ == "__main__":
    unittest.main()
