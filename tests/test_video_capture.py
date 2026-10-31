"""Offline lifecycle regressions: no physical cameras or optional packages."""

import builtins
import gc
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import Mock, patch

from openarm_video.camera import CameraConfig, Frame, OpenCVCamera, _DeviceLease
from openarm_video.capture import CameraReader, VideoCapture


def until(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Timed out waiting for reader")


class Source:
    def __init__(self):
        self.closed = threading.Event()
        self.count = 0

    def read(self):
        self.count += 1
        return Frame(SimpleNamespace(shape=(3, 4, 3)), time.monotonic())

    def close(self):
        self.closed.set()


class CameraConfigTests(unittest.TestCase):
    def test_invalid_names_dimensions_ranges_and_half_preview_rejected(self):
        for options in ({"name": "../head"}, {"name": ""}, {"width": 0},
                        {"height": True}, {"fps": float("nan")}, {"fps": 0},
                        {"kind": "infrared"}, {"device": 0}, {"preview_width": 320},
                        {"preview_width": 320, "preview_height": -1}, {"uri": ""}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                CameraConfig(**({"name": "head"} | options))

    def test_frame_freshness_rejects_future_and_expired_timestamps(self):
        frame = Frame(object(), 10)
        self.assertTrue(frame.fresh(10.5))
        self.assertFalse(frame.fresh(9))
        self.assertFalse(frame.fresh(13))

    def test_group_rejects_duplicate_names_and_duplicate_resolved_devices(self):
        with self.assertRaises(ValueError):
            VideoCapture([CameraConfig("head"), CameraConfig("head")])
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "camera"
            actual.touch()
            alias = Path(directory) / "alias"
            alias.symlink_to(actual)
            with self.assertRaises(ValueError):
                VideoCapture([CameraConfig("a", str(actual)), CameraConfig("b", str(alias))])

    def test_physical_group_requires_paths_but_simulation_accepts_empty_paths(self):
        for config in (CameraConfig("head"), CameraConfig("depth", kind="depth")):
            with self.subTest(kind=config.kind), self.assertRaises(ValueError):
                VideoCapture([config])
        capture = VideoCapture([CameraConfig("head")], simulate=True, stale_after=.5)
        self.assertEqual(capture.stale_after, .5)
        self.assertEqual(capture.readers["head"].stale_after, .5)
        capture.close()

    def test_device_lease_is_exclusive_and_released_idempotently(self):
        key = f"test-camera-{os.getpid()}"
        lease = _DeviceLease(key)
        try:
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                _DeviceLease(key)
        finally:
            lease.close()
            lease.close()
        again = _DeviceLease(key)
        again.close()

    @unittest.skipUnless(os.name == "posix", "POSIX advisory lock")
    def test_device_lease_protects_against_another_process(self):
        key = f"test-camera-cross-process-{os.getpid()}"
        lease = _DeviceLease(key)
        try:
            code = "from openarm_video.camera import _DeviceLease; _DeviceLease(" + repr(key) + ")"
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already in use by another process", result.stderr)
        finally:
            lease.close()


class OpenCVCameraTests(unittest.TestCase):
    def module(self, cap):
        return SimpleNamespace(VideoCapture=Mock(return_value=cap), CAP_V4L2=200, CAP_ANY=0,
                               CAP_PROP_FOURCC=6, CAP_PROP_FRAME_WIDTH=3,
                               CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_FPS=5,
                               VideoWriter_fourcc=Mock(return_value=123))

    def test_explicit_path_open_failed_closes_handle_and_releases_lease(self):
        cap = Mock()
        cap.isOpened.return_value = False
        module = self.module(cap)
        with patch.dict(sys.modules, {"cv2": module}):
            with self.assertRaises(OSError):
                OpenCVCamera(CameraConfig("head", "/not-a-real-device"))
        cap.release.assert_called_once()
        lease = _DeviceLease(str(Path("/not-a-real-device").resolve()))
        lease.close()

    def test_full_resolution_samples_are_owned_not_preview_resized(self):
        image = Mock()
        owned = image.copy.return_value
        cap = Mock()
        cap.read.return_value = (True, image)
        module = self.module(cap)
        config = CameraConfig("head", "/not-a-real-device", width=1280, height=720,
                              preview_width=640, preview_height=360)
        with patch.dict(sys.modules, {"cv2": module}):
            with OpenCVCamera(config) as camera:
                frame = camera.read()
                self.assertIs(frame.image, owned)
                self.assertEqual(frame.pixel_format, "bgr8")
                self.assertEqual(frame.sequence, 1)
                self.assertTrue(frame.fresh(time.monotonic()))
            camera.close()
        cap.release.assert_called_once()

    def test_missing_opencv_produces_install_guidance(self):
        imported = builtins.__import__
        def no_opencv(name, *args, **kwargs):
            if name == "cv2":
                raise ImportError("not installed")
            return imported(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=no_opencv):
            with self.assertRaisesRegex(RuntimeError, r"\.\[video\]"):
                OpenCVCamera(CameraConfig("head", "/not-a-real-device"))


class ReaderTests(unittest.TestCase):
    def test_construction_is_passive_start_idempotent_and_context_cleans_up(self):
        source = Source()
        factory = Mock(return_value=source)
        reader = CameraReader(CameraConfig("head", fps=100), source_factory=factory)
        factory.assert_not_called()
        self.assertEqual(reader.status()["status"], "Stopped")
        with reader:
            reader.start()
            until(lambda: reader.snapshot()[2] >= 2)
            frame, error, count = reader.snapshot()
            self.assertEqual(error, "")
            self.assertEqual(frame.sequence, count)
            self.assertEqual(reader.status()["status"], "Online")
            self.assertEqual(reader.status()["width"], 4)
        factory.assert_called_once()
        self.assertTrue(source.closed.is_set())
        self.assertEqual(reader.status()["status"], "Stopped")
        reader.close()
        with self.assertRaises(RuntimeError):
            reader.start()

    def test_transient_capture_failure_reconnects_and_closes_old_source(self):
        broken = Source()
        broken.read = Mock(side_effect=OSError("USB unplugged"))
        good = Source()
        factory = Mock(side_effect=[broken, good])
        reader = CameraReader(CameraConfig("head", fps=100), source_factory=factory, retry_interval=.01)
        with reader:
            until(lambda: reader.snapshot()[2] > 0)
            self.assertEqual(reader.snapshot()[1], "")
            self.assertEqual(reader.status()["status"], "Online")
        self.assertTrue(broken.closed.is_set())
        self.assertTrue(good.closed.is_set())
        self.assertEqual(factory.call_count, 2)

    def test_stale_frame_does_not_get_fresh_timestamp_from_reader(self):
        source = Source()
        source.read = lambda: Frame(object(), time.monotonic() - 10)
        with CameraReader(CameraConfig("head"), source_factory=lambda _: source) as reader:
            until(lambda: reader.snapshot()[2] > 0)
            self.assertEqual(reader.status()["status"], "Stale")
            self.assertGreater(reader.status()["frame_age_s"], 9)

    def test_close_interrupts_a_source_waiting_for_data(self):
        source = Source()
        entered = threading.Event()
        def wait_for_frame():
            entered.set()
            source.closed.wait(2)
            raise OSError("Closed")
        source.read = wait_for_frame
        reader = CameraReader(CameraConfig("head"), source_factory=lambda _: source).start()
        self.assertTrue(entered.wait(1))
        reader.close()
        self.assertEqual(reader.status()["status"], "Stopped")
        self.assertEqual(reader.snapshot()[2], 0)

    def test_cleanup_failure_is_reported_without_reopening_device(self):
        source = Source()
        source.read = Mock(side_effect=OSError("read failed"))
        source.close = Mock(side_effect=OSError("release failed"))
        factory = Mock(return_value=source)
        reader = CameraReader(CameraConfig("head"), source_factory=factory, retry_interval=.01).start()
        until(lambda: "cleanup failed" in reader.snapshot()[1])
        with self.assertRaisesRegex(RuntimeError, "release failed"):
            reader.close()
        self.assertEqual(factory.call_count, 1)

    def test_failed_cleanup_retains_source_and_lease_until_explicit_retry(self):
        key = f"test-retained-video-source-{os.getpid()}"
        allow_close = threading.Event()
        references = []

        class LeasedSource:
            def __init__(self):
                self.lease = _DeviceLease(key)

            def read(self):
                raise OSError("read failed")

            def close(self):
                if not allow_close.is_set():
                    raise OSError("native child still alive")
                self.lease.close()

        def factory(_config):
            source = LeasedSource()
            references.append(weakref.ref(source))
            return source

        reader = CameraReader(CameraConfig("depth", kind="depth"),
                              source_factory=factory, retry_interval=.01).start()
        try:
            until(lambda: "cleanup failed" in reader.snapshot()[1])
            reader._thread.join(timeout=1)
            self.assertFalse(reader._thread.is_alive())
            gc.collect()
            self.assertIsNotNone(references[0](), "Failed cleanup must not discard the owning source")
            with self.assertRaisesRegex(RuntimeError, "native child still alive"):
                reader.close()
            self.assertEqual(reader.status()["status"], "Disconnected")
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                _DeviceLease(key)
            if os.name == "posix":
                code = "from openarm_video.camera import _DeviceLease; _DeviceLease(" + repr(key) + ")"
                child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
                self.assertNotEqual(child.returncode, 0)
                self.assertIn("already in use by another process", child.stderr)
            self.assertEqual(len(references), 1, "Cleanup failure must stop automatic reconnects")
        finally:
            allow_close.set()
            reader.close()
        gc.collect()
        self.assertIsNone(references[0](), "A successful retry must release the source reference")
        self.assertEqual(reader.snapshot()[1], "")
        self.assertEqual(reader._cleanup_error, "")
        self.assertEqual(reader.status()["status"], "Stopped")
        lease = _DeviceLease(key)
        lease.close()

    def test_close_failure_racing_with_read_exit_is_visible_until_next_close(self):
        entered, read_exited = threading.Event(), threading.Event()
        source = Source()
        calls = []

        def read():
            entered.set()
            source.closed.wait(2)
            read_exited.set()
            raise OSError("read interrupted")

        def close():
            calls.append(len(calls) + 1)
            source.closed.set()
            if len(calls) == 1:
                self.assertTrue(read_exited.wait(1))
                raise OSError("first cleanup failed")

        source.read, source.close = read, close
        reader = CameraReader(CameraConfig("head"), source_factory=lambda _: source).start()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaisesRegex(RuntimeError, "first cleanup failed"):
                reader.close()
            self.assertEqual(calls, [1], "Worker cleanup must preserve the caller's first failure")
            self.assertFalse(reader._thread.is_alive())
            self.assertIs(reader._source, source)
            self.assertEqual(reader.snapshot()[2], 0)
        finally:
            reader.close()
        self.assertEqual(calls, [1, 2])
        self.assertIsNone(reader._source)
        self.assertEqual(reader.snapshot()[1], "")
        reader.close()
        self.assertEqual(calls, [1, 2], "Successful close remains idempotent")

    def test_group_cleanup_visits_all_readers_after_an_error(self):
        capture = VideoCapture([CameraConfig("a"), CameraConfig("b")], source_factory=lambda _: Source())
        capture.readers["b"].close = Mock(side_effect=RuntimeError("b failed"))
        capture.readers["a"].close = Mock()
        with self.assertRaisesRegex(RuntimeError, "b failed"):
            capture.close()
        capture.readers["a"].close.assert_called_once()

    def test_simulation_is_explicit_and_never_selects_hardware_factory(self):
        source = Source()
        with patch("openarm_video.capture.SimulationCamera", return_value=source) as factory, \
             patch("openarm_video.capture._source", side_effect=AssertionError("hardware")):
            with VideoCapture([CameraConfig("head")], simulate=True) as capture:
                until(lambda: capture.snapshot("head")[2] > 0)
                self.assertTrue(capture.status()["head"]["simulation"])
            factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
