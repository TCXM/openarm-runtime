"""Episode lifecycle, faults and training semantics without physical devices."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from openarm_collection import Collection, Episode, inspect_episode, review_episode
from openarm_collection.recording import recover_episode
from openarm_collection.export import aligned, export_aligned, training_frame

try:
    import numpy as np
except ImportError:
    np = None


def fixture(root, images=False):
    streams = {"robot/right": {"required": True, "max_age_s": .5, "encoding": "mit_exchange"}}
    if images:
        streams["camera/head"] = dict(required=True, max_age_s=.5, encoding="bgr8", shape=[2, 3, 3])
        streams["tactile/left_pad"] = dict(required=True, max_age_s=.5, encoding="xyz_counts", shape=[69, 3])
    episode = Episode(root, dict(sides=["right"], joint_order=[f"right/j{i}" for i in range(1, 9)],
                                task="synthetic test", operator="test"), streams)
    start = time.monotonic() - .25
    for i in range(6):
        t = start + i * .05
        state = {j: dict(position=float(i), velocity=0., torque=0., received_at=t - .01) for j in range(1, 9)}
        episode.submit("robot/right", i, t, dict(targets_rad={j: float(i + 10) for j in range(1, 9)},
                                                observation_before=state, feedback=state))
        if images:
            episode.submit("camera/head", i, t, np.full((2, 3, 3), [10, 20, 30], dtype=np.uint8))
            episode.submit("tactile/left_pad", i, t, [[1, 2, 3]] * 69)
    path = episode.finish()
    review_episode(path, "success")
    return path


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_accept_failed_task_and_repeatable_causal_export(self):
        path = fixture(self.root)
        review_episode(path, "failure", note="valid negative demonstration")
        rows = list(aligned(path, 40))
        self.assertGreater(len(rows), 6)
        for row in rows:
            sample = row["streams"]["robot/right"]
            self.assertLessEqual(sample["timestamp"], row["source_timestamp"])
        export_aligned(path, self.root / "one", 40)
        export_aligned(path, self.root / "two", 40)
        self.assertEqual((self.root / "one/frames.jsonl").read_bytes(),
                         (self.root / "two/frames.jsonl").read_bytes())

    def test_missing_or_regressing_stream_cannot_be_accepted(self):
        for sequences in ([], [1, 3], [2, 1]):
            episode = Episode(self.root, {}, {"x": {"required": True}})
            for seq in sequences:
                episode.submit("x", seq, time.monotonic(), [0])
            path = episode.finish()
            self.assertEqual(inspect_episode(path)["state"], "incomplete")
            with self.assertRaises(ValueError):
                review_episode(path, "success")
            review_episode(path, "interrupted", accepted=False)

    def test_writer_failure_is_visible_and_raw_data_retained(self):
        episode = Episode(self.root, {}, {"x": {"required": True}})
        episode.submit("x", 1, time.monotonic(), {"invalid": float("nan")})
        path = episode.finish()
        self.assertEqual(inspect_episode(path)["state"], "incomplete")
        self.assertIn("ValueError", episode.error)
        self.assertTrue((path / "samples.jsonl").exists())

    def test_stale_required_stream_ends_only_collection(self):
        collection = Collection(self.root)
        self.addCleanup(collection.close)
        collection.register("x", max_age_s=.08)
        collection.ready = True
        collection.emit("x", time.monotonic(), [1])
        path = collection.start("task", "operator")
        collection.emit("x", time.monotonic(), [2])
        # Wait for an observable lifecycle outcome, not an assumed scheduler speed.
        deadline = time.monotonic() + 3
        while collection.last is None and time.monotonic() < deadline:
            threading.Event().wait(.01)
        self.assertEqual(collection.last, path)
        self.assertTrue(collection.ready)
        self.assertEqual(inspect_episode(path)["state"], "incomplete")
        collection.review("interrupted", False)
        self.assertIsNone(collection.last)

    def test_start_requires_readiness_and_review_between_episodes(self):
        collection = Collection(self.root)
        self.addCleanup(collection.close)
        with self.assertRaises(RuntimeError):
            collection.start("task", "operator")
        collection.register("x")
        collection.ready = True
        collection.emit("x", time.monotonic(), [1])
        collection.start("task", "operator")
        collection.emit("x", time.monotonic(), [2])
        collection.finish()
        with self.assertRaises(RuntimeError):
            collection.start("task", "operator")
        collection.review("failure")
        collection.start("task", "operator")
        collection.emit("x", time.monotonic(), [3])
        collection.finish()

    def test_recovery_preserves_truncated_journal(self):
        path = fixture(self.root)
        journal = path / "samples.jsonl"
        with journal.open("a") as file:
            file.write('{"stream":')
        before = journal.read_bytes()
        result = recover_episode(path)
        self.assertEqual(result["readable_records"], 6)
        self.assertTrue(result["problem"])
        self.assertEqual(journal.read_bytes(), before)

    def test_teleop_diagnostics_finalize_legacy_json_without_losing_events(self):
        from openarm_runtime.report import AsyncReport
        path = self.root / "diagnostic.json"
        report = AsyncReport(path, {"action": "teleoperation"})
        for i in range(30):
            report.record({"event": "sample", "sequence": i})
        report.close()
        data = json.loads(path.read_text())
        self.assertEqual([event["sequence"] for event in data["events"]], list(range(30)))
        self.assertEqual(len(report.journal.read_text().splitlines()), 30)

    @unittest.skipIf(np is None, "numpy optional")
    def test_bounded_images_do_not_block_and_overflow_invalidates(self):
        entered, release = threading.Event(), threading.Event()
        save = np.save
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test release timeout")
            return save(*args, **kwargs)
        with patch("numpy.save", side_effect=blocked):
            episode = Episode(self.root, {}, {"camera/a": {"required": True}}, capacity=1)
            episode.submit("camera/a", 1, time.monotonic(), np.zeros((2, 2, 3), dtype=np.uint8))
            self.assertTrue(entered.wait(2))
            self.assertTrue(episode.submit("camera/a", 2, time.monotonic(), np.zeros((2, 2, 3), dtype=np.uint8)))
            self.assertFalse(episode.submit("camera/a", 3, time.monotonic(), np.zeros((2, 2, 3), dtype=np.uint8)))
            release.set()
            path = episode.finish()
        self.assertIn("overflow", episode.error)
        self.assertEqual(inspect_episode(path)["state"], "incomplete")

    @unittest.skipIf(np is None, "numpy optional")
    def test_training_state_is_feedback_action_is_command_and_rgb_is_converted(self):
        path = fixture(self.root, images=True)
        row = next(aligned(path))
        frame = training_frame(path, row, inspect_episode(path))
        np.testing.assert_equal(frame["observation.state"], [0] * 8)
        np.testing.assert_equal(frame["action"], [10] * 8)
        np.testing.assert_equal(frame["observation.images.head"][0, 0], [30, 20, 10])
        self.assertEqual(frame["observation.tactile.left_pad"].shape, (69, 3))
        array = path / row["streams"]["camera/head"]["data"]["array"]
        array.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum"):
            training_frame(path, row, inspect_episode(path))

    @unittest.skipIf(np is None, "numpy optional")
    def test_native_camera_sink_retains_every_frame_without_snapshot_polling(self):
        from openarm_video.camera import CameraConfig
        from openarm_video.capture import CameraReader
        from openarm_collection.sources import bind_video
        collection = Collection(self.root)
        self.addCleanup(collection.close)
        reader = CameraReader(CameraConfig("head", width=64, height=48, fps=30), simulate=True)
        detach = bind_video(collection, reader)
        self.addCleanup(detach)
        self.addCleanup(reader.close)
        # Bypass operator readiness only for this acquisition-boundary test.
        collection.episode = Episode(self.root, {}, collection.streams)
        seen = threading.Event()
        receive = reader.on_sample
        def sink(frame, sequence):
            receive(frame, sequence)
            if sequence == 4:
                seen.set()
        reader.on_sample = sink
        reader.start()
        self.assertTrue(seen.wait(3))
        reader.close()
        path = collection.finish()
        journal = [json.loads(line) for line in (path / "samples.jsonl").read_text().splitlines()]
        self.assertEqual([r["sequence"] for r in journal], list(range(1, len(journal) + 1)))
        self.assertGreaterEqual(len(journal), 4)
        self.assertEqual(inspect_episode(path)["state"], "review")

    @unittest.skipIf(np is None, "numpy optional")
    def test_image_byte_budget_includes_inflight_array(self):
        episode = Episode(self.root, {}, {"x": {}}, max_bytes=8)
        self.assertFalse(episode.submit("x", 0, time.monotonic(), np.zeros(9, dtype=np.uint8)))
        self.assertEqual(inspect_episode(episode.finish())["state"], "incomplete")
