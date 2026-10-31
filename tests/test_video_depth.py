"""Depth helper/process/profile tests without an SDK, NumPy or camera hardware."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openarm_video.camera import CameraConfig, _DeviceLease
from openarm_video.capture import VideoCapture
from openarm_video.depth import OpenNIDepthCamera, build_depth_helper
from openarm_video.profile import load_cameras, simulation_cameras


class Child:
    def __init__(self):
        self.returncode = None
        self.terminated = 0
        self.killed = 0
        self.waits = []
        self.stubborn = False
        self.needs_kill = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.stubborn or (self.needs_kill and not self.killed):
            raise subprocess.TimeoutExpired("fake-helper", timeout)
        self.returncode = 0
        return 0


class DepthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        helper = self.root / "helper"
        helper.write_text("not executed by these tests")
        helper.chmod(0o700)
        self.config = CameraConfig("depth", kind="depth", depth_helper=str(helper), uri="fake-depth-test")
        self.uint16 = object()
        self.image = SimpleNamespace(dtype=self.uint16, ndim=2, shape=(480, 640))
        self.cv2 = SimpleNamespace(IMREAD_UNCHANGED=-1, imread=Mock(return_value=self.image))
        self.numpy = SimpleNamespace(uint16=self.uint16)
        self.child = Child()
        self.modules = patch.dict(sys.modules, {"cv2": self.cv2, "numpy": self.numpy})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.platform = patch("openarm_video.depth.sys.platform", "linux")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.popen = patch("openarm_video.depth.subprocess.Popen", return_value=self.child)
        self.factory = self.popen.start()
        self.addCleanup(self.popen.stop)

    def camera(self):
        camera = OpenNIDepthCamera(self.config)
        self.addCleanup(camera.close)
        return camera

    def test_native_mode_bounds_are_validated_before_any_source_opens(self):
        for args in ({"width": 4097}, {"height": 5000}, {"fps": 121}, {"fps": 29.97}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                CameraConfig("depth", kind="depth", **args)
        self.assertEqual(CameraConfig("depth", kind="depth", fps=30.0).fps, 30)
        self.factory.assert_not_called()

    def test_process_command_and_raw_depth_units_are_preserved(self):
        camera = self.camera()
        command = self.factory.call_args.args[0]
        self.assertEqual(command, [str(Path(self.config.depth_helper).resolve()), str(camera._path),
                                   "640", "480", "30", self.config.uri])
        self.assertEqual(self.factory.call_args.kwargs["stdin"], subprocess.DEVNULL)
        camera._path.write_bytes(b"test frame")
        frame = camera.read()
        self.assertIs(frame.image, self.image)
        self.assertEqual(frame.pixel_format, "depth_mm")
        self.assertEqual(frame.sequence, 1)
        self.assertTrue(frame.fresh(time.monotonic()))
        stamp = camera._path.stat().st_mtime_ns
        os.utime(camera._path, ns=(stamp + 1_000_000, stamp + 1_000_000))
        self.assertEqual(camera.read().sequence, 2)

    def test_initialization_timeout_is_bounded_without_fabricating_frames(self):
        camera = self.camera()
        with patch("openarm_video.depth.time.monotonic", return_value=camera._deadline + 1):
            with self.assertRaisesRegex(TimeoutError, "No new OpenNI2"):
                camera.read()
        self.cv2.imread.assert_not_called()

    def test_same_file_is_not_returned_as_a_new_frame_and_later_deadline_is_short(self):
        camera = self.camera()
        camera._path.write_bytes(b"first frame")
        first = camera.read()
        self.assertAlmostEqual(camera._deadline - first.received_at, 3)
        with patch("openarm_video.depth.time.monotonic", return_value=camera._deadline + 1):
            with self.assertRaises(TimeoutError):
                camera.read()
        self.cv2.imread.assert_called_once()

    def test_wrong_depth_format_is_rejected(self):
        camera = self.camera()
        camera._path.write_bytes(b"wrong type")
        self.cv2.imread.return_value = SimpleNamespace(dtype="uint8", ndim=2)
        with self.assertRaisesRegex(OSError, "invalid uint16"):
            camera.read()

    def test_child_error_includes_bounded_log_tail(self):
        camera = self.camera()
        camera._log.write(b"x" * 3000 + b"\nUSB transfer timeout")
        camera._log.flush()
        self.child.returncode = 1
        with self.assertRaisesRegex(OSError, "USB transfer timeout") as error:
            camera.read()
        self.assertLessEqual(len(str(error.exception)), 2048)

    def test_close_terminates_then_kills_hung_child_and_is_idempotent(self):
        camera = self.camera()
        folder = camera._folder
        self.child.needs_kill = True
        camera.close()
        camera.close()
        self.assertEqual(self.child.terminated, 1)
        self.assertEqual(self.child.killed, 1)
        self.assertEqual(self.child.waits, [2, 2])
        self.assertFalse(folder.exists())
        self.assertTrue(camera._log.closed)
        lease = _DeviceLease("openni:" + self.config.uri)
        lease.close()
        with self.assertRaisesRegex(OSError, "closed"):
            camera.read()

    def test_failed_process_start_releases_lease_and_temporary_resources(self):
        folder = self.root / "runtime"
        folder.mkdir()
        self.factory.side_effect = OSError("cannot spawn")
        with patch("openarm_video.depth.tempfile.mkdtemp", return_value=str(folder)):
            with self.assertRaisesRegex(OSError, "cannot spawn"):
                OpenNIDepthCamera(self.config)
        self.assertFalse(folder.exists())
        lease = _DeviceLease("openni:" + self.config.uri)
        lease.close()

    def test_unreaped_child_keeps_lease_until_close_can_confirm_exit(self):
        camera = self.camera()
        self.child.stubborn = True
        try:
            with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                camera.close()
            self.assertTrue(camera._folder.exists(), "Do not erase files still owned by a running helper")
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                _DeviceLease("openni:" + self.config.uri)
        finally:
            self.child.stubborn = False
            camera.close()

    def test_child_exit_racing_with_terminate_is_not_cleanup_failure(self):
        camera = self.camera()
        def already_exited():
            self.child.returncode = 0
            raise ProcessLookupError("gone")
        self.child.terminate = already_exited
        camera.close()
        self.assertFalse(camera._folder.exists())


class DepthBuildTests(unittest.TestCase):
    def sdk(self, root):
        include, library = root / "include", root / "lib"
        include.mkdir()
        (library / "OpenNI2/Drivers").mkdir(parents=True)
        (include / "OpenNI.h").write_text("header placeholder")
        (library / "libOpenNI2.so").write_bytes(b"library placeholder")
        (library / "OpenNI2/Drivers/liborbbec.so").write_bytes(b"driver placeholder")
        return include, library

    def test_failed_build_preserves_previous_executable_and_removes_temporaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            include, library = self.sdk(root)
            output = root / "capture"
            output.write_bytes(b"known working helper")
            with patch("openarm_video.depth.sys.platform", "linux"), \
                 patch("openarm_video.depth.subprocess.run", side_effect=subprocess.CalledProcessError(1, "c++")):
                with self.assertRaises(subprocess.CalledProcessError):
                    build_depth_helper(include, library, output)
            self.assertEqual(output.read_bytes(), b"known working helper")
            self.assertFalse(list(root.glob(".openarm-depth-build-*")))

    def test_successful_build_uses_packaged_source_and_explicit_matching_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            include, library = self.sdk(root)
            output = root / "bin/capture"
            # macOS temporary directories may use /var -> /private/var aliases.
            resolved_include, resolved_library = include.resolve(), library.resolve()
            def compile(command, **kwargs):
                self.assertIn(str(resolved_library / "libOpenNI2.so"), command)
                self.assertIn("-I" + str(resolved_include), command)
                self.assertIn("-Wl,-rpath," + str(resolved_library), command)
                self.assertEqual(kwargs, {"check": True, "timeout": 120})
                source = Path(command[command.index("-I" + str(resolved_include)) + 1])
                self.assertIn("PIXEL_FORMAT_DEPTH_1_MM", source.read_text())
                target = Path(command[command.index("-o") + 1])
                target.write_bytes(b"compiled helper")
                target.chmod(0o700)
            with patch("openarm_video.depth.sys.platform", "linux"), \
                 patch("openarm_video.depth.subprocess.run", side_effect=compile):
                self.assertEqual(build_depth_helper(include, library, output), output.resolve())
            self.assertEqual(output.read_bytes(), b"compiled helper")
            self.assertTrue(os.access(output, os.X_OK))

    def test_missing_sdk_is_rejected_before_invoking_compiler(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("openarm_video.depth.sys.platform", "linux"), \
             patch("openarm_video.depth.subprocess.run") as compiler:
            with self.assertRaises(FileNotFoundError):
                build_depth_helper(directory, directory, Path(directory) / "capture")
            compiler.assert_not_called()


class VideoProfileTests(unittest.TestCase):
    def test_relative_paths_are_resolved_against_profile_not_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "video.json"
            profile.write_text(json.dumps({"cameras": [
                {"name": "head", "device": "devices/rgb"},
                {"name": "depth", "kind": "depth", "depth_helper": "bin/helper", "uri": "2bc5/0403@1/2"},
            ]}))
            rgb, depth = load_cameras(profile)
            self.assertEqual(rgb.device, str((root / "devices/rgb").resolve()))
            self.assertEqual(depth.depth_helper, str((root / "bin/helper").resolve()))
            self.assertEqual(depth.uri, "2bc5/0403@1/2")

    def test_invalid_schema_names_fields_and_path_types_raise_clear_value_error(self):
        cases = [[], {}, {"cameras": []}, {"cameras": [], "other": True},
                 {"cameras": [True]}, {"cameras": [{"name": "x", "driver": "bogus"}]},
                 {"cameras": [{"name": "x", "device": True}]},
                 {"cameras": [{"name": "x", "depth_helper": 42}]},
                 {"cameras": [{"device": "anything"}]},
                 {"cameras": [{"name": "x"}, {"name": "x"}]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.json"
            for value in cases:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    with self.assertRaises(ValueError):
                        load_cameras(path)

    def test_duplicate_rgb_device_paths_are_rejected_before_capture_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.json"
            path.write_text(json.dumps({"cameras": [{"name": "a", "device": "rgb"},
                                                    {"name": "b", "device": "./rgb"}]}))
            configs = load_cameras(path)
            with self.assertRaisesRegex(ValueError, "device"):
                VideoCapture(configs)

    def test_simulation_profile_contains_no_hardware_paths(self):
        configs = simulation_cameras()
        self.assertEqual([c.name for c in configs], ["head", "depth", "left", "right"])
        self.assertTrue(all(not c.device and c.depth_helper is None for c in configs))


if __name__ == "__main__":
    unittest.main()
