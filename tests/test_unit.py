"""Unit tests: no encoder or media files required (mocks / synthetic data)."""
import array
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sets sys.path)

import app_paths
import app_settings
import ffmpeg_downloader
import ffmpeg_helper
import media_probe
import truehd_encoder as te
import truehd_workflow as tw
from audio_stream_info import AudioStreamInfo
from ffmpeg_helper import FFmpegResult

LIBREMPEG_HELP = """Encoder truehd [TrueHD]:
    General capabilities: dr1 delay small exp
    Threading capabilities: none
    Supported sample rates: 44100 48000 88200 96000 176400 192000
    Supported sample formats: s16p s32p
    Supported channel layouts: mono stereo 2.1 3.0 3.1 4.0 4.1 5.0(side) 5.1(side) 5.0 5.1 6.0 6.1 7.0 7.1
mlpenc AVOptions:
  -max_interval      <int>        E...A...... Max number of frames between each new header
  -peak_bitrate      <int>        E...A...... Peak data rate signalled in the major sync header (bits/s).
"""
FFMPEG_HELP = LIBREMPEG_HELP.replace(" 5.0 5.1 6.0 6.1 7.0 7.1", "").replace(
    "  -peak_bitrate      <int>        E...A...... Peak data rate signalled in the major sync header (bits/s).\n", "")


def dts_stream(layout="7.1", channels=8, profile="DTS-HD MA", fmt="s32p", rate=48000,
               bits=24, index=1, audio_index=0):
    return AudioStreamInfo(
        stream_index=index, codec_name="DTS", channels=layout, bitrate=None, language="eng",
        audio_index=audio_index, format_name=media_probe.classify_audio(
            {"codec_name": "dts", "profile": profile}),
        profile=profile, channel_count=channels, channel_layout=layout, sample_rate=rate,
        sample_fmt=fmt, bit_depth=bits,
    )


def report_with(layouts):
    report = te.EncoderReport(path="/x/ffmpeg", status=te.STATUS_READY)
    for layout in te.SUPPORTED_LAYOUTS:
        report.tests.append(te.LayoutTest(layout, 24, layout in layouts, ""))
    return report


class FormatIdentificationTests(unittest.TestCase):
    def test_classification(self):
        cases = {
            ("dts", "DTS"): "DTS",
            ("dts", "DTS-HD HRA"): "DTS-HD High Resolution",
            ("dts", "DTS-HD MA"): "DTS-HD Master Audio",
            ("dts", "DTS-HD MA + DTS:X"): "DTS:X (DTS-HD Master Audio core)",
            ("dts", "DTS-HD MA + DTS:X IMAX"): "DTS:X (DTS-HD Master Audio core)",
            ("dts", None): "DTS (Unknown profile)",
            ("truehd", None): "Dolby TrueHD",
            ("truehd", "Dolby TrueHD + Dolby Atmos"): "Dolby TrueHD Atmos",
            ("ac3", None): "Dolby Digital",
            ("eac3", None): "Dolby Digital Plus",
            ("eac3", "Dolby Digital Plus + Dolby Atmos"): "Dolby Digital Plus Atmos",
            ("", None): "Unknown",
        }
        for (codec, profile), expected in cases.items():
            self.assertEqual(media_probe.classify_audio({"codec_name": codec, "profile": profile}),
                             expected, (codec, profile))

    def test_probe_json_to_streams(self):
        data = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {"index": 1, "codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD MA",
             "channels": 8, "channel_layout": "7.1", "sample_rate": "48000", "sample_fmt": "s32p",
             "bits_per_raw_sample": "24", "bit_rate": "4744000",
             "tags": {"language": "eng", "title": "DTS-HD MA 7.1"},
             "disposition": {"default": 1, "forced": 0}},
            {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 2,
             "channel_layout": "stereo", "sample_rate": "48000", "sample_fmt": "fltp",
             "tags": {"LANGUAGE": "fre"}, "disposition": {"default": 0, "forced": 1}},
        ]}
        streams = media_probe.audio_streams_from_probe(data)
        self.assertEqual(len(streams), 2)
        ma = streams[0]
        self.assertEqual((ma.stream_index, ma.audio_index, ma.channel_layout, ma.channel_count),
                         (1, 0, "7.1", 8))
        self.assertEqual((ma.sample_rate, ma.bit_depth, ma.title, ma.is_default),
                         (48000, 24, "DTS-HD MA 7.1", True))
        self.assertEqual(ma.bitrate, "4744 kb/s")
        self.assertEqual(ma.format_name, "DTS-HD Master Audio")
        self.assertEqual((streams[1].language, streams[1].is_forced, streams[1].audio_index),
                         ("fre", True, 1))
        self.assertIn("DTS-HD Master Audio", str(ma))

    def test_legacy_str_format_unchanged(self):
        s = AudioStreamInfo(1, "DTS", "5.1(side)", "1536 kb/s", "eng")
        self.assertEqual(str(s), "Stream #1: DTS - 5.1(side) (1536 kb/s) [eng]")

    def test_timecodes(self):
        self.assertEqual(media_probe.parse_timecode("1:02:03"), 3723)
        self.assertEqual(media_probe.parse_timecode("90"), 90)
        self.assertEqual(media_probe.parse_timecode("10:00"), 600)
        with self.assertRaises(ValueError):
            media_probe.parse_timecode("1:2:3:4")
        with self.assertRaises(ValueError):
            media_probe.parse_timecode("-5")


class SourceValidationTests(unittest.TestCase):
    """A/B/C: supported and unsupported inputs."""

    def test_a_dts_hd_ma_51_supported(self):
        stream = dts_stream("5.1(side)", 6)
        self.assertEqual(tw.validate_source(stream, report_with(["5.1(side)", "7.1"])), [])

    def test_b_dts_hd_ma_71_supported(self):
        self.assertEqual(tw.validate_source(dts_stream(), report_with(["5.1(side)", "7.1"])), [])

    def test_c_unsupported_layouts_rejected(self):
        report = report_with(["5.1(side)", "7.1"])
        for layout, ch in (("7.1(wide)", 8), ("6.1", 7), ("5.1", 6), ("stereo", 2), (None, 8)):
            problems = tw.validate_source(dts_stream(layout, ch), report)
            self.assertTrue(problems, layout)
            self.assertIn("not supported", problems[0])
        self.assertIn("front-centre", tw.validate_source(dts_stream("7.1(wide)"), report)[0])

    def test_lossy_core_decode_rejected(self):
        problems = tw.validate_source(dts_stream(fmt="fltp"), report_with(["7.1"]))
        self.assertTrue(any("lossy DTS core" in p for p in problems))

    def test_lossy_formats_rejected(self):
        for profile in ("DTS", "DTS-HD HRA", "DTS Express"):
            problems = tw.validate_source(dts_stream(profile=profile), report_with(["7.1"]))
            self.assertTrue(problems and "only offered for DTS-HD Master Audio" in problems[0], profile)

    def test_invalid_truehd_sample_rate_rejected(self):
        problems = tw.validate_source(dts_stream(rate=192000), report_with(["7.1"]))
        self.assertTrue(any("not a valid Dolby TrueHD configuration" in p for p in problems))

    def test_high_sample_rate_peak_warning(self):
        """Warn only where even incompressible audio could exceed 1875-byte access units."""
        cases = [
            (8, 48000, 24, False), (6, 48000, 24, False), (6, 96000, 24, False),
            (8, 96000, 24, True), (6, 192000, 24, True), (8, 88200, 24, True),
            (8, 96000, 16, False), (8, 44100, 24, False),
        ]
        for channels, rate, bits, expect in cases:
            self.assertEqual(te.may_exceed_peak_rate(channels, rate, bits), expect, (channels, rate, bits))
        warn = tw.source_warnings(dts_stream("7.1", 8, rate=96000))
        self.assertTrue(any("18 Mb/s maximum" in w for w in warn))
        self.assertFalse(any("18 Mb/s" in w for w in tw.source_warnings(dts_stream())))
        # It is a warning, not a block: validation still allows 7.1/96k
        self.assertEqual(tw.validate_source(dts_stream("7.1", 8, rate=96000), report_with(["7.1"])), [])

    def test_dtsx_allowed_with_warning(self):
        stream = dts_stream(profile="DTS-HD MA + DTS:X")
        self.assertEqual(tw.validate_source(stream, report_with(["7.1"])), [])
        self.assertIn("NOT Dolby Atmos", tw.source_warnings(stream)[0])

    def test_e_encoder_without_71_blocks_71_only(self):
        report = report_with(["5.1(side)"])
        self.assertTrue(tw.validate_source(dts_stream(), report))
        self.assertEqual(tw.validate_source(dts_stream("5.1(side)", 6), report), [])

    def test_text_probe_only_is_rejected(self):
        legacy = AudioStreamInfo(1, "DTS", "7.1", None)
        self.assertIn("ffprobe", tw.validate_source(legacy, report_with(["7.1"]))[0])


class EncoderDiscoveryTests(unittest.TestCase):
    def test_capability_parsing(self):
        caps = te.parse_encoder_help(LIBREMPEG_HELP)
        self.assertTrue(caps.has_encoder and caps.experimental)
        self.assertIn("7.1", caps.layouts)
        self.assertEqual(caps.sample_formats, ["s16p", "s32p"])
        std = te.parse_encoder_help(FFMPEG_HELP)
        self.assertNotIn("7.1", std.layouts)
        self.assertFalse(te.parse_encoder_help("Codec 'truehd' is not recognized").has_encoder)

    def test_d_missing_executable(self):
        with mock.patch.object(te, "bundled_candidates", return_value=[]), \
                mock.patch.object(te.shutil, "which", return_value=None):
            report = te.check_encoder("/definitely/not/here/ffmpeg")
        self.assertEqual(report.status, te.STATUS_MISSING)
        self.assertIn("not found", report.message)
        self.assertFalse(report.is_usable)

    def test_e_encoder_lacking_71(self):
        caps = te.parse_encoder_help(FFMPEG_HELP)
        caps.has_peak_option = caps.has_zlib = True
        ok = te.LayoutTest("5.1(side)", 24, True, "ok")
        with tempfile.NamedTemporaryFile() as fake, \
                mock.patch.object(te, "query_capabilities", return_value=caps), \
                mock.patch.object(te, "run_layout_test", return_value=ok):
            report = te.check_encoder(fake.name)
        self.assertEqual(report.status, te.STATUS_NO_71)
        self.assertEqual(report.verified_layouts, ["5.1(side)"])
        self.assertFalse(report.can_encode("7.1"))

    def test_unpatched_or_no_zlib_build_not_verified(self):
        """7.1-capable LibreMPEG without the peak fix or zlib must not be used."""
        for peak, zlib, expect in ((False, True, "peak data rate fix"), (True, False, "without zlib")):
            caps = te.parse_encoder_help(LIBREMPEG_HELP)
            caps.has_peak_option, caps.has_zlib = peak, zlib
            ok = te.LayoutTest("x", 24, True, "ok")
            with tempfile.NamedTemporaryFile() as fake, \
                    mock.patch.object(te, "query_capabilities", return_value=caps), \
                    mock.patch.object(te, "run_layout_test", return_value=ok) as run:
                report = te.check_encoder(fake.name)
            self.assertEqual(report.status, te.STATUS_NOT_VERIFIED)
            self.assertIn(expect, report.message)
            self.assertFalse(report.is_usable)
            run.assert_not_called()

    def test_capability_flags_parsed(self):
        self.assertTrue(te.parse_encoder_help(LIBREMPEG_HELP).has_peak_option)
        self.assertFalse(te.parse_encoder_help(FFMPEG_HELP).has_peak_option)

    def test_encoder_without_truehd(self):
        with tempfile.NamedTemporaryFile() as fake, \
                mock.patch.object(te, "query_capabilities", return_value=te.AdvertisedCapabilities()):
            report = te.check_encoder(fake.name)
        self.assertEqual(report.status, te.STATUS_MISSING)

    def test_failed_selftest_is_not_verified(self):
        caps = te.parse_encoder_help(LIBREMPEG_HELP)
        bad = te.LayoutTest("x", 24, False, "channel mapping wrong")
        with tempfile.NamedTemporaryFile() as fake, \
                mock.patch.object(te, "query_capabilities", return_value=caps), \
                mock.patch.object(te, "run_layout_test", return_value=bad):
            report = te.check_encoder(fake.name)
        self.assertEqual(report.status, te.STATUS_NOT_VERIFIED)
        self.assertFalse(report.is_usable)

    def test_configured_path_wins_and_persists(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"DTS2DD_SETTINGS_DIR": d}):
            exe = Path(d) / "ffmpeg"
            exe.write_text("")
            app_settings.set_setting("truehd_encoder_path", str(exe))
            self.assertEqual(app_settings.get_setting("truehd_encoder_path"), str(exe))
            self.assertEqual(te.discover_encoder(app_settings.get_setting("truehd_encoder_path")),
                             (str(exe), "configured"))


class ChannelMappingTests(unittest.TestCase):
    """F: incorrect channel mapping is detected."""

    def test_identity_mapping_passes(self):
        pcm = te.synth_labelled_pcm(8, 2000, 24)
        mapping = te.identify_channels(pcm, array.array("i", pcm), 8)
        self.assertIsNone(te.describe_mapping_problem("7.1", mapping))

    def test_side_rear_swap_detected(self):
        pcm = te.synth_labelled_pcm(8, 2000, 24)
        swapped = array.array("i", pcm)
        for i in range(2000):   # swap BL<->SL and BR<->SR
            f = i * 8
            swapped[f + 4], swapped[f + 6] = pcm[f + 6], pcm[f + 4]
            swapped[f + 5], swapped[f + 7] = pcm[f + 7], pcm[f + 5]
        problem = te.describe_mapping_problem("7.1", te.identify_channels(pcm, swapped, 8))
        self.assertIn("BL output contains SL", problem)
        self.assertIn("SR output contains BR", problem)

    def test_left_right_swap_detected(self):
        pcm = te.synth_labelled_pcm(6, 1000, 16)
        swapped = array.array("i", pcm)
        for i in range(1000):
            swapped[i * 6], swapped[i * 6 + 1] = pcm[i * 6 + 1], pcm[i * 6]
        self.assertIn("FL output contains FR",
                      te.describe_mapping_problem("5.1(side)", te.identify_channels(pcm, swapped, 6)))

    def test_channels_are_unique(self):
        pcm = te.synth_labelled_pcm(8, 500, 24)
        cols = [pcm[k::8] for k in range(8)]
        self.assertEqual(len({c.tobytes() for c in cols}), 8)

    def test_major_sync_parser(self):
        # rate=48k(0), ch6=0x0F, ch8=0x4F  (7.1)
        word = (0 << 28) | (0x0F << 15) | 0x4F
        data = b"\x00" * 4 + te.TRUEHD_SYNC + word.to_bytes(4, "big") + b"\x00" * 8
        sync = te.parse_major_sync(data)
        self.assertEqual((sync.sample_rate, sync.ch6_assignment, sync.ch8_assignment), (48000, 0x0F, 0x4F))
        self.assertIsNone(te.check_channel_assignment("7.1", sync))
        self.assertIn("Lrs/Rrs", te.MajorSync.describe(0x4F))
        self.assertEqual(sync.peak_bitrate, 0)
        peak_word = (1 << 15) | 5999                   # is_vbr + 18 Mb/s at 48 kHz
        data18 = b"\x00" * 4 + te.TRUEHD_SYNC + word.to_bytes(4, "big") + b"\xb7\x52" + b"\x00" * 4 \
            + peak_word.to_bytes(2, "big") + b"\x00" * 4
        self.assertEqual(te.parse_major_sync(data18).peak_bitrate, 17_997_000)
        wrong = te.MajorSync(48000, 0x07, 0x47)       # what 5.1(back) produces
        self.assertIsNotNone(te.check_channel_assignment("7.1", wrong))
        self.assertIsNone(te.parse_major_sync(b"no sync here"))


class CommandTests(unittest.TestCase):
    """I/J/K/N/O/P: command construction."""

    def job(self, **kw):
        base = dict(source_path="/m/in.mkv", output_path="/m/.tmp.mkv", source_stream_index=1,
                    audio_stream_count=2, layout="7.1", sample_fmt="s32p", sample_rate=48000,
                    language="eng")
        base.update(kw)
        return te.TrueHDJob(**base)

    def test_i_retain_original(self):
        cmd = te.build_truehd_command("enc", self.job())
        self.assertEqual(cmd[cmd.index("-i") + 1:cmd.index("-c")],
                         ["/m/in.mkv", "-map", "0", "-map", "-0:t?", "-map", "0:1", "-map", "0:t?"])
        for arg in ("-c:a:2", "-ch_layout:a:2", "-sample_fmt:a:2", "-ar:a:2", "-strict:a:2"):
            self.assertIn(arg, cmd)
        self.assertEqual(cmd[cmd.index("-ch_layout:a:2") + 1], "7.1")
        self.assertIn("title=Dolby TrueHD 7.1", cmd)
        self.assertIn("language=eng", cmd)
        self.assertEqual(cmd[cmd.index("-disposition:a:2") + 1], "0")
        self.assertNotIn("-af", cmd)
        self.assertNotIn("-filter_complex", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertNotIn("-ss", cmd)

    def test_j_remove_original(self):
        cmd = te.build_truehd_command("enc", self.job(remove_source=True))
        self.assertIn("-0:1", cmd)
        self.assertIn("-c:a:1", cmd)
        self.assertNotIn("-c:a:2", cmd)

    def test_k_make_default(self):
        cmd = te.build_truehd_command("enc", self.job(make_default=True))
        self.assertEqual(cmd[cmd.index("-disposition:a:0") + 1], "-default")
        self.assertEqual(cmd[cmd.index("-disposition:a:1") + 1], "-default")
        self.assertEqual(cmd[cmd.index("-disposition:a:2") + 1], "default")
        self.assertEqual(cmd[cmd.index("-default_mode") + 1], "passthrough")

    def test_p_clip_cut_is_stream_copy(self):
        cmd = te.build_clip_cut_command("enc", "/m/in.mkv", "/m/.cut.mkv", 600.0, 60.0)
        i = cmd.index("-i")
        self.assertEqual(cmd[i - 4:i], ["-ss", "600.000000", "-t", "60.000000"])
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertNotIn("truehd", cmd)
        # the TrueHD encode itself never seeks
        self.assertNotIn("-ss", te.build_truehd_command("enc", self.job()))
        self.assertNotIn("-ss", te.build_source_decode_command("enc", self.job()))

    def test_source_fingerprint_output(self):
        plain = te.build_truehd_command("enc", self.job())
        self.assertNotIn("hash", plain)
        cmd = te.build_truehd_command("enc", self.job(), "/tmp/src.sha256")
        mkv = cmd.index("/m/.tmp.mkv")
        tail = cmd[mkv + 1:]
        self.assertEqual(tail, ["-map", "0:1", "-c:a", "pcm_s32le", "-f", "hash",
                                "-hash", "sha256", "/tmp/src.sha256"])
        self.assertEqual(cmd[:mkv + 1], plain)      # MKV output itself unchanged

    def test_output_hash_command_reads_only_output(self):
        cmd = te.build_output_hash_command("ff", "/m/out.mkv", 2, "/tmp/out.sha256")
        self.assertEqual(cmd[cmd.index("-i") + 1], "/m/out.mkv")
        self.assertEqual(cmd.count("-i"), 1)
        self.assertEqual(cmd[cmd.index("-map") + 1], "0:a:2")
        self.assertEqual(cmd[-1], "/tmp/out.sha256")

    def test_read_hash_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "h")
            p.write_text("SHA256=" + "AB" * 32 + "\n")
            self.assertEqual(te.read_hash_file(str(p)), "ab" * 32)
            p.write_text("garbage")
            self.assertIsNone(te.read_hash_file(str(p)))
            self.assertIsNone(te.read_hash_file(str(Path(d, "missing"))))

    def test_peak_rate_and_packet_sizes_in_command(self):
        cmd = te.build_truehd_command("enc", self.job(), packet_sizes_path="/tmp/sizes.txt")
        self.assertEqual(cmd[cmd.index("-peak_bitrate:a:2") + 1], "18000000")
        self.assertEqual(cmd[cmd.index("-stats_enc_post:a:2") + 1], "/tmp/sizes.txt")
        self.assertEqual(cmd[cmd.index("-stats_enc_post_fmt:a:2") + 1], "{size}")
        self.assertLess(cmd.index("-stats_enc_post:a:2"), cmd.index("/m/.tmp.mkv"))

    def test_access_unit_size_check(self):
        self.assertAlmostEqual(te.max_access_unit_bytes(9_600_000, 48000), 1000)
        self.assertAlmostEqual(te.max_access_unit_bytes(18_000_000, 96000), 1875)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "sizes.txt")
            p.write_text("900\n1018\n950\n")             # Spectre at 0:34.5 with a 9.6 Mb/s header
            error, _ = te.check_access_unit_sizes(str(p), 9_600_000, 48000)
            self.assertIn("1 access unit(s) exceed", error)
            error, summary = te.check_access_unit_sizes(str(p), 17_997_000, 48000)
            self.assertIsNone(error)
            self.assertIn("largest access unit 1018 bytes", summary)
            self.assertIsNotNone(te.check_access_unit_sizes(str(Path(d, "none.txt")), 18_000_000, 48000)[0])

    def test_end_padding_fix_applied(self):
        cmd = te.build_truehd_command("enc", self.job())
        self.assertEqual(cmd[cmd.index("-bsf:a:2") + 1], "side_data=remove=skip_samples")

    def test_n_o_ac3_eac3_commands(self):
        for codec, rate in (("ac3", "640k"), ("eac3", "768k")):
            cmd = ffmpeg_helper.build_append_audio_command("ff", "/m/in.mkv", "/m/t.mkv", 1, 2,
                                                           codec, rate, False)
            self.assertEqual(cmd[cmd.index("-c:a:2") + 1], codec)
            self.assertEqual(cmd[cmd.index("-b:a:2") + 1], rate)
            self.assertIn("0:1", cmd)
            removed = ffmpeg_helper.build_append_audio_command("ff", "/m/in.mkv", "/m/t.mkv", 1, 2,
                                                               codec, rate, True)
            self.assertIn("-0:1", removed)
            self.assertIn("-c:a:1", removed)


class TrackRemovalCommandTests(unittest.TestCase):
    """Removing ticked audio/subtitle tracks, alone or while converting."""

    def test_stream_maps_drop_ticked_tracks(self):
        maps = ffmpeg_helper.build_stream_maps(1, False, [4, 2, 1])
        self.assertEqual(maps, ["-map", "0", "-map", "-0:2", "-map", "-0:4",
                                "-map", "-0:t?", "-map", "0:1", "-map", "0:t?"])

    def test_ac3_index_accounts_for_removed_audio(self):
        cmd = ffmpeg_helper.build_append_audio_command("ff", "/m/in.mkv", "/m/t.mkv", 1, 3,
                                                       "ac3", "640k", True, [2, 5], 1)
        self.assertIn("-0:1", cmd)
        self.assertIn("-0:2", cmd)
        self.assertIn("-0:5", cmd)
        self.assertEqual(cmd[cmd.index("-c:a:1") + 1], "ac3")

    def test_truehd_index_accounts_for_removed_audio(self):
        job = te.TrueHDJob(source_path="/m/in.mkv", output_path="/m/.tmp.mkv", source_stream_index=1,
                           audio_stream_count=3, layout="7.1", sample_fmt="s32p", sample_rate=48000,
                           remove_streams=(2, 6), removed_other_audio=1)
        self.assertEqual(job.new_audio_index, 2)
        self.assertEqual(job.removed_indices, {2, 6})
        cmd = te.build_truehd_command("enc", job)
        self.assertIn("-0:2", cmd)
        self.assertIn("-0:6", cmd)
        self.assertIn("-c:a:2", cmd)

    def test_remove_only_is_stream_copy(self):
        cmd = ffmpeg_helper.build_remove_streams_command("ff", "/m/in.mkv", "/m/t.mkv", [3, 2])
        self.assertEqual(cmd[cmd.index("-i") + 1:cmd.index("-c")],
                         ["/m/in.mkv", "-map", "0", "-map", "-0:2", "-map", "-0:3"])
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertNotIn("-c:a", " ".join(cmd))

    def test_subtitle_text_probe(self):
        stderr = ("  Stream #0:3(eng): Subtitle: hdmv_pgs_subtitle (pgssub) (default) (forced)\n"
                  "  Stream #0:4: Subtitle: subrip (srt)\n")
        subs = ffmpeg_helper._parse_subtitles_text(stderr)
        self.assertEqual([(s.stream_index, s.language, s.is_forced) for s in subs],
                         [(3, "eng", True), (4, None, False)])
        self.assertEqual(str(subs[0]), "PGS [eng] {default, forced}")


class ProgressParsingTests(unittest.TestCase):
    """Removal / AC-3 jobs report percentage and bytes written."""

    def test_progress_reports_fraction_and_bytes(self):
        script = ("import sys\n"
                  "for t, b in ((30, 1000000), (60, 2500000)):\n"
                  "    print(f'out_time_us={t * 1000000}'); print(f'total_size={b}')\n"
                  "    print('speed=2.0x'); print('progress=continue')\n")
        seen = []
        result = ffmpeg_helper.run_ffmpeg([sys.executable, "-c", script], total_seconds=120,
                                          on_progress=seen.append)
        self.assertEqual(result.returncode, 0)
        self.assertEqual([(p.fraction, p.out_bytes, p.speed) for p in seen],
                         [(0.25, 1000000, 2.0), (0.5, 2500000, 2.0)])

    def test_copy_summary(self):
        self.assertEqual(ffmpeg_helper.copy_summary(40_100_000_000, 845),
                         "Wrote 40.1 GB in 14:05 (47 MB/s)")


class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.source = Path(self.dir) / "Spectre.mkv"
        self.source.write_bytes(b"original")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_output_names(self):
        self.assertEqual(tw.output_name(self.source, None).name, "Spectre_Sound_TrueHD.mkv")
        self.assertEqual(tw.output_name(self.source, 60).name, "Spectre_TrueHD_Test_60s.mkv")

    def test_unique_path_never_overwrites(self):
        target = self.source.with_name("Spectre_Sound_TrueHD.mkv")
        target.write_bytes(b"existing")
        self.assertEqual(tw.unique_path(target).name, "Spectre_Sound_TrueHD (2).mkv")

    def test_stale_partials_found(self):
        stale = self.source.with_name(".Spectre.truehd-partial-abc.mkv")
        stale.write_bytes(b"")
        self.assertEqual(tw.find_stale_partials(str(self.source)), [str(stale)])

    def test_finalize_replace_requires_passed_checks(self):
        temp = self.source.with_name(".Spectre.truehd-partial-x.mkv")
        temp.write_bytes(b"new")
        result = tw.TrueHDResult(str(self.source), str(temp), None,
                                 checks=[tw.Check("x", False, "")])
        with self.assertRaises(te.TrueHDError):
            tw.TrueHDConverter.finalize_replace(result)
        self.assertEqual(self.source.read_bytes(), b"original")
        clip = tw.TrueHDResult(str(self.source), str(temp), None, clip=True)
        with self.assertRaises(te.TrueHDError):
            tw.TrueHDConverter.finalize_replace(clip)

    def test_disk_space_estimate(self):
        stream = dts_stream()
        full = tw.estimate_required_bytes(35_700_000_000, 8880.0, stream, None)
        clip = tw.estimate_required_bytes(35_700_000_000, 8880.0, stream, 60)
        self.assertGreater(full, 35_700_000_000)
        self.assertLess(clip, 1_000_000_000)

    def test_g_failed_encoding_leaves_source(self):
        """G: encoder failure -> error with stderr detail, temp removed, source untouched."""
        probe = {"format": {"duration": "10"}, "streams": [
            {"index": 0, "codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD MA",
             "channels": 8, "channel_layout": "7.1", "sample_rate": "48000",
             "sample_fmt": "s32p", "bits_per_raw_sample": "24"}]}

        def failing_runner(cmd, **kw):
            Path(cmd[-1]).write_bytes(b"partial")
            return FFmpegResult(1, ["Error while opening encoder", "Conversion failed!"])

        conv = tw.TrueHDConverter("enc", "probe", None, report_with(["7.1"]),
                                  log=lambda m: None, runner=failing_runner)
        with mock.patch.object(tw, "run_ffprobe", return_value=probe):
            with self.assertRaises(te.TrueHDError) as ctx:
                conv.convert(str(self.source), 0, tw.TrueHDOptions())
        self.assertIn("Error while opening encoder", str(ctx.exception))
        self.assertEqual(self.source.read_bytes(), b"original")
        self.assertEqual(sorted(os.listdir(self.dir)), ["Spectre.mkv"])

    def test_insufficient_disk_space(self):
        probe = {"format": {"duration": "8880"}, "streams": [
            {"index": 0, "codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD MA",
             "channels": 8, "channel_layout": "7.1", "sample_rate": "48000",
             "sample_fmt": "s32p", "bits_per_raw_sample": "24"}]}
        conv = tw.TrueHDConverter("enc", "probe", None, report_with(["7.1"]), log=lambda m: None)
        usage = shutil._ntuple_diskusage(100, 99, 1)
        with mock.patch.object(tw, "run_ffprobe", return_value=probe), \
                mock.patch.object(tw.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(te.TrueHDError) as ctx:
                conv.convert(str(self.source), 0, tw.TrueHDOptions())
        self.assertIn("Not enough free disk space", str(ctx.exception))
        self.assertEqual(sorted(os.listdir(self.dir)), ["Spectre.mkv"])


class InstalledLayoutTests(unittest.TestCase):
    """Installed (frozen) app: bundled tools win, nothing is written beside the exe."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.app = self.dir / "Programs" / "DTS2DD"
        exe = app_paths.exe_name
        for folder in ("ffmpeg", "librempeg"):
            (self.app / folder).mkdir(parents=True)
            for tool in ("ffmpeg", "ffprobe"):
                (self.app / folder / exe(tool)).write_bytes(b"")
        (self.app / exe("DTS to AC-3 Converter")).write_bytes(b"")
        self.patches = [
            mock.patch.object(app_paths.sys, "frozen", True, create=True),
            mock.patch.object(app_paths.sys, "executable", str(self.app / exe("DTS to AC-3 Converter"))),
            mock.patch.dict(os.environ, {"DTS2DD_DATA_DIR": str(self.dir / "LocalAppData" / "DTS2DD")}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.dir)

    def test_bundled_ffmpeg_preferred_over_path(self):
        with mock.patch.object(ffmpeg_downloader.shutil, "which", return_value="/usr/bin/ffmpeg"):
            found = ffmpeg_downloader.get_ffmpeg_path()
        self.assertEqual(Path(found), self.app / "ffmpeg" / app_paths.exe_name("ffmpeg"))
        self.assertEqual(Path(media_probe.find_ffprobe(found)),
                         self.app / "ffmpeg" / app_paths.exe_name("ffprobe"))

    def test_bundled_librempeg_discovered(self):
        path, origin = te.discover_encoder(None)
        self.assertEqual((Path(path), origin), (self.app / "librempeg" / app_paths.exe_name("ffmpeg"), "bundled"))

    def test_downloads_go_to_user_folder(self):
        target = ffmpeg_downloader.download_dir()
        self.assertEqual(target, self.dir / "LocalAppData" / "DTS2DD" / "ffmpeg")
        self.assertNotIn(str(self.app), str(target))


class SettingsTests(unittest.TestCase):
    def test_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"DTS2DD_SETTINGS_DIR": d}):
            self.assertEqual(app_settings.load_settings(), {})
            app_settings.set_setting("a", 1)
            self.assertEqual(app_settings.get_setting("a"), 1)
            Path(d, "settings.json").write_text("{corrupt")
            self.assertEqual(app_settings.load_settings(), {})


if __name__ == "__main__":
    unittest.main()
