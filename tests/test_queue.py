"""Job queue tests: queue model and runner (no tools needed), plus real
multi-job runs on the same film with FFmpeg (and LibreMPEG for TrueHD)."""
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import ffmpeg_path, ffprobe_path, librempeg_path

import compatibility_rules as cr
import job_queue as jq
import library_repair as lr
import media_inspector as mi
import queue_jobs as qj
from audio_stream_info import AudioStreamInfo, SubtitleStreamInfo
from ffmpeg_helper import get_tracks
from library_scanner import LibraryScanner, ScanCache
from media_probe import run_ffprobe
from truehd_workflow import TrueHDOptions

FFMPEG, FFPROBE, LIBRE = ffmpeg_path(), ffprobe_path(), librempeg_path()


def job(name="Film.mkv", kind=jq.KIND_REMOVE, status=jq.QUEUED):
    return jq.QueueJob(kind, f"/films/{name}", f"job {name}", [name], {}, status=status)


def audio(index, codec="AC3", channels=6, lang="eng", title=None):
    return AudioStreamInfo(index, codec, "5.1(side)", None, lang, format_name=codec, channel_count=channels,
                           channel_layout="5.1(side)", sample_rate=48000, title=title)


class QueueModelTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dts2dd-queue-"))
        self.path = self.dir / "queue.json"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_add_move_remove_and_persist(self):
        q = jq.JobQueue(self.path)
        a, b, c = (q.add(job(n)) for n in ("A.mkv", "B.mkv", "C.mkv"))
        q.move([c.id], -1)
        self.assertEqual([j.name for j in q.jobs], ["A.mkv", "C.mkv", "B.mkv"])
        q.move([a.id, c.id], 1)                    # both down, order kept
        self.assertEqual([j.name for j in q.jobs], ["B.mkv", "A.mkv", "C.mkv"])
        q.move([b.id], -1)                         # already first: no change
        self.assertEqual(q.jobs[0].id, b.id)
        q.remove([a.id])
        again = jq.JobQueue(self.path)             # reloaded from disk
        self.assertEqual([j.name for j in again.jobs], ["B.mkv", "C.mkv"])
        self.assertEqual(again.jobs[0].id, b.id)

    def test_running_job_becomes_interrupted_after_restart(self):
        q = jq.JobQueue(self.path)
        j = q.add(job())
        q.set_status(j, jq.RUNNING)
        again = jq.JobQueue(self.path)
        self.assertEqual(again.jobs[0].status, jq.INTERRUPTED)
        self.assertIn("not changed", again.jobs[0].message)
        self.assertEqual(again.pending(), [])
        self.assertEqual(again.retry([again.jobs[0].id]), 1)
        self.assertEqual(len(again.pending()), 1)

    def test_running_job_cannot_be_removed(self):
        q = jq.JobQueue(self.path)
        j = q.add(job())
        q.set_status(j, jq.RUNNING)
        self.assertEqual(q.remove([j.id]), 0)

    def test_clear_finished_keeps_failures(self):
        q = jq.JobQueue(self.path)
        for status in (jq.DONE, jq.FAILED, jq.QUEUED, jq.PARTIAL):
            q.add(job(status=status))
        q.clear_finished()
        self.assertEqual([j.status for j in q.jobs], [jq.FAILED, jq.QUEUED])

    def test_corrupt_queue_file_ignored(self):
        self.path.write_text("{not json")
        self.assertEqual(jq.JobQueue(self.path).jobs, [])

    def test_jobs_for_same_film(self):
        q = jq.JobQueue(self.path)
        q.add(job("A.mkv"))
        q.add(job("A.mkv"))
        q.add(job("B.mkv"))
        self.assertEqual(len(q.jobs_for("/films/A.mkv")), 2)
        self.assertEqual(len(q.jobs_for("/films/sub/../A.mkv")), 2)
        if os.name == "nt":
            self.assertEqual(len(q.jobs_for("\\FILMS\\a.MKV")), 2)      # Windows: case-insensitive


class QueueRunnerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dts2dd-queue-"))
        self.queue = jq.JobQueue(self.dir / "q.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_runs_in_order_and_picks_up_new_jobs(self):
        order = []
        first = self.queue.add(job("1.mkv"))
        self.queue.add(job("2.mkv"))

        def executor(j):
            order.append(j.name)
            if j is first:
                self.queue.add(job("3.mkv"))      # added while the queue runs
            return jq.JobResult(jq.DONE, "ok")
        done = jq.QueueRunner(self.queue, {jq.KIND_REMOVE: executor}).run()
        self.assertEqual(order, ["1.mkv", "2.mkv", "3.mkv"])
        self.assertTrue(all(j.status == jq.DONE for j in done))

    def test_reorder_while_running_is_respected(self):
        order = []
        a = self.queue.add(job("a.mkv"))
        self.queue.add(job("b.mkv"))
        c = self.queue.add(job("c.mkv"))

        def executor(j):
            order.append(j.name)
            if j is a:
                self.queue.move([c.id], -1)
            return jq.JobResult(jq.DONE)
        jq.QueueRunner(self.queue, {jq.KIND_REMOVE: executor}).run()
        self.assertEqual(order, ["a.mkv", "c.mkv", "b.mkv"])

    def test_failure_does_not_stop_queue(self):
        self.queue.add(job("1.mkv"))
        self.queue.add(job("2.mkv"))
        results = iter([RuntimeError("boom"), jq.JobResult(jq.DONE)])

        def executor(j):
            r = next(results)
            if isinstance(r, Exception):
                raise r
            return r
        done = jq.QueueRunner(self.queue, {jq.KIND_REMOVE: executor}).run()
        self.assertEqual([j.status for j in done], [jq.FAILED, jq.DONE])
        self.assertIn("boom", done[0].message)

    def test_stop_after_current(self):
        for n in "abc":
            self.queue.add(job(f"{n}.mkv"))
        runner = jq.QueueRunner(self.queue, {})

        def executor(j):
            runner.stop_after_current.set()
            return jq.JobResult(jq.DONE)
        runner.executors = {jq.KIND_REMOVE: executor}
        done = runner.run()
        self.assertEqual(len(done), 1)
        self.assertEqual(len(self.queue.pending()), 2)

    def test_cancel_keeps_remaining_queued(self):
        for n in "abc":
            self.queue.add(job(f"{n}.mkv"))
        cancel = threading.Event()

        def executor(j):
            cancel.set()
            return jq.JobResult(jq.FAILED, "ffmpeg stopped")
        done = jq.QueueRunner(self.queue, {jq.KIND_REMOVE: executor}, cancelled=cancel).run()
        self.assertEqual([j.status for j in done], [jq.CANCELLED])
        self.assertEqual(len(self.queue.pending()), 2)

    def test_cancelled_env_pre_cancels_next_process(self):
        env = qj.QueueEnv("ffmpeg", "ffprobe", lambda: None, mi.ToolSet("f", "p"), cr.ScannerConfig(),
                          ScanCache(self.dir / "c.json"))
        env.cancel()
        self.assertTrue(env.new_handle().cancelled.is_set())

    def test_summary(self):
        a = self.queue.add(job("a.mkv"))
        self.queue.set_status(a, jq.FAILED, "disk full")
        text = jq.summary_text([a])
        self.assertIn("Failed: 1", text)
        self.assertIn("disk full", text)


class TrackRefTests(unittest.TestCase):
    def test_unchanged_file_uses_index(self):
        tracks = [audio(1), audio(2, "EAC3")]
        ref = jq.make_ref(tracks[1], tracks)
        self.assertEqual(jq.resolve_ref(ref, tracks, unchanged=True), 2)

    def test_index_shift_after_earlier_job(self):
        tracks = [audio(1, "DTS"), audio(2, "AC3", lang="fre"), audio(3, "EAC3")]
        ref = jq.make_ref(tracks[2], tracks)
        after = [audio(1, "AC3", lang="fre"), audio(2, "EAC3")]        # DTS removed by an earlier job
        self.assertEqual(jq.resolve_ref(ref, after, unchanged=False), 2)

    def test_identical_tracks_resolved_by_position(self):
        tracks = [audio(1), audio(2), audio(3, "DTS")]
        ref = jq.make_ref(tracks[1], tracks)
        after = [audio(1), audio(2), audio(3, "DTS"), audio(4, "TRUEHD")]   # earlier job appended a track
        self.assertEqual(jq.resolve_ref(ref, after, unchanged=False), 2)

    def test_ambiguous_or_missing_is_refused(self):
        tracks = [audio(1), audio(2)]
        ref = jq.make_ref(tracks[1], tracks)
        with self.assertRaises(jq.TrackNotFound):
            jq.resolve_ref(ref, [audio(2)], unchanged=False)            # one of the twins was removed
        with self.assertRaises(jq.TrackNotFound):
            jq.resolve_ref(ref, [audio(1, "DTS")], unchanged=False)

    def test_subtitle_refs(self):
        subs = [SubtitleStreamInfo(4, "hdmv_pgs_subtitle", "eng"), SubtitleStreamInfo(5, "hdmv_pgs_subtitle", "fre")]
        ref = jq.make_ref(subs[1], subs)
        shifted = [SubtitleStreamInfo(3, "hdmv_pgs_subtitle", "eng"), SubtitleStreamInfo(4, "hdmv_pgs_subtitle", "fre")]
        self.assertEqual(jq.resolve_ref(ref, shifted, unchanged=False), 4)


# --------------------------------------------------------------------------
# Real runs
# --------------------------------------------------------------------------

@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg / ffprobe not available")
class QueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test_scanner_integration import build_film, encode_eac3
        self.dir = Path(tempfile.mkdtemp(prefix="dts2dd-queue-run-"))
        plain = encode_eac3(self.dir, "plain")
        self.dts = self.dir / "core.dts"
        src = helpers.sample("core_51_24_48_768_1.dtshd")
        inputs = [["-f", "eac3", "-i", plain]]
        meta = [{"language": "eng", "title": "Commentary", "disposition": "default"}]
        if src:
            helpers.repeat_dts(FFMPEG, src, self.dts, 400)
            inputs.append(["-i", self.dts])
            meta.append({"language": "eng", "title": "DTS 5.1", "disposition": "0"})
        self.has_dts = bool(src)
        self.film = build_film(self.dir, "Film.mkv", inputs, meta)
        tools = mi.ToolSet(FFMPEG, FFPROBE)
        self.queue = jq.JobQueue(self.dir / "queue.json")
        self.logs = []
        self.env = qj.QueueEnv(FFMPEG, FFPROBE, lambda: None, tools, cr.ScannerConfig(),
                               ScanCache(self.dir / "cache.json"), log=self.logs.append)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def tracks(self):
        return get_tracks(str(self.film), FFMPEG, FFPROBE)

    def run_queue(self):
        return jq.QueueRunner(self.queue, qj.make_executors(self.env)).run()

    def test_several_jobs_on_one_film_in_order(self):
        if not self.has_dts:
            self.skipTest("FATE DTS sample missing")
        audio, subs = self.tracks()
        commentary, dts = audio
        # Job 1 (Convert File tab): remove the commentary track -> the DTS track moves from #2 to #1
        self.queue.add(qj.removal_job(str(self.film), audio, subs, [commentary]))
        # Job 2 (Convert File tab): DTS -> E-AC-3, queued while the DTS track was still #2
        self.queue.add(qj.convert_job(str(self.film), audio, subs, dts, "eac3", "448k", False, []))
        # Job 3 (Library Scanner): clean subtitles + fix default audio
        item = LibraryScanner(self.env.tools, cr.ScannerConfig(), self.env.cache).inspect_one(str(self.film))
        plan = lr.plan_repair(item, [cr.ACTION_CLEAN_SUBS, cr.ACTION_FIX_DEFAULT], lr.RepairOptions(),
                              cr.ScannerConfig(), None)
        self.queue.add(qj.repair_job(plan, lr.RepairOptions(), cr.ScannerConfig()))

        done = self.run_queue()
        self.assertEqual([j.status for j in done], [jq.DONE, jq.DONE, jq.DONE],
                         "\n".join(j.message for j in done))
        self.assertTrue(any("Track #2 is now #1" in line for line in self.logs))
        out = run_ffprobe(FFPROBE, str(self.film))
        a = [s for s in out["streams"] if s["codec_type"] == "audio"]
        self.assertEqual([s["codec_name"] for s in a], ["dts", "eac3"])
        self.assertEqual(sum(s["disposition"]["default"] for s in a), 1)        # fixed by job 3
        subs_after = [s for s in out["streams"] if s["codec_type"] == "subtitle"]
        self.assertEqual(len(subs_after), 4)                                     # cleaned by job 3

    def test_job_for_vanished_track_is_skipped_not_guessed(self):
        audio, subs = self.tracks()
        self.queue.add(qj.removal_job(str(self.film), audio, subs, [audio[0]]))
        self.queue.add(qj.removal_job(str(self.film), audio, subs, [audio[0]]))   # same track again
        done = self.run_queue()
        self.assertEqual(done[0].status, jq.DONE)
        self.assertEqual(done[1].status, jq.SKIPPED)
        self.assertIn("no longer present", done[1].message)

    def test_missing_film_is_skipped(self):
        audio, subs = self.tracks()
        self.queue.add(qj.removal_job(str(self.film), audio, subs, [subs[2]]))
        os.rename(self.film, self.dir / "moved.mkv")
        done = self.run_queue()
        self.assertEqual(done[0].status, jq.SKIPPED)

    def test_cancel_during_job_leaves_film_and_queue(self):
        audio, subs = self.tracks()
        self.queue.add(qj.removal_job(str(self.film), audio, subs, [subs[3]]))
        self.queue.add(qj.removal_job(str(self.film), audio, subs, [subs[4]]))
        digest = helpers.sha256(self.film)
        real = qj.remove_streams_in_place

        def cancelling(*a, **k):
            self.env.cancel()
            return real(*a, **k)
        with mock.patch.object(qj, "remove_streams_in_place", cancelling):
            done = jq.QueueRunner(self.queue, qj.make_executors(self.env), cancelled=self.env.cancel_event).run()
        self.assertEqual([j.status for j in done], [jq.CANCELLED])
        self.assertEqual(helpers.sha256(self.film), digest)
        self.assertEqual(len(self.queue.pending()), 1)

    def test_queue_survives_restart(self):
        audio, subs = self.tracks()
        self.queue.add(qj.removal_job(str(self.film), audio, subs, [subs[2]]))
        reloaded = jq.JobQueue(self.queue.path)
        done = jq.QueueRunner(reloaded, qj.make_executors(self.env)).run()
        self.assertEqual(done[0].status, jq.DONE, done[0].message)
        self.assertEqual(json.loads(self.queue.path.read_text())["jobs"][0]["status"], jq.DONE)

    @unittest.skipUnless(LIBRE, "LibreMPEG not available")
    def test_truehd_job_replaces_original_after_verification(self):
        import truehd_encoder as te
        from test_scanner_integration import build_film
        report = te.check_encoder(LIBRE, reference_decoder=FFMPEG)
        if not report.is_usable:
            self.skipTest(report.message)
        pcm = te.synth_labelled_pcm(6, 48000 * 4, 24)
        raw = self.dir / "51.raw"
        raw.write_bytes(te._to_little_endian_bytes(pcm))
        mka = self.dir / "51.mka"
        helpers.run([FFMPEG, "-v", "error", "-f", "s32le", "-ar", "48000", "-ch_layout", "5.1", "-i", raw,
                     "-c:a", "flac", "-sample_fmt", "s32", "-y", mka])
        film = build_film(self.dir, "Flac.mkv", [["-i", mka]], [{"language": "eng", "disposition": "default"}],
                          seconds=4.0)
        audio, subs = get_tracks(str(film), FFMPEG, FFPROBE)
        options = TrueHDOptions(make_default=True, replace_original=True)
        self.queue.add(qj.truehd_job(str(film), audio, subs, audio[0], options, subs[2:], "5.1"))
        self.env.encoder = lambda: report
        done = self.run_queue()
        self.assertEqual(done[0].status, jq.DONE, done[0].message)
        self.assertEqual(done[0].final_path, str(film))
        self.assertIn("bit-identical", done[0].message)
        out = run_ffprobe(FFPROBE, str(film))
        a = [s for s in out["streams"] if s["codec_type"] == "audio"]
        self.assertEqual([(s["codec_name"], s["disposition"]["default"]) for s in a], [("flac", 0), ("truehd", 1)])
        self.assertEqual(len([s for s in out["streams"] if s["codec_type"] == "subtitle"]), 2)


if __name__ == "__main__":
    unittest.main()
