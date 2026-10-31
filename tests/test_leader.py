"""Read-only boundary, encoder mapping and software calibration regressions."""

import math
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, MagicMock, patch
from types import SimpleNamespace, ModuleType

from openarm_leader.mapping import Mapping, SOURCE_IDS, delta
from openarm_leader.calibration import collect_stable
from openarm_leader.transport import LeaderBus, Reader, Sample, validate_read_packet


def packet(mid=1, instruction=2, params=(56, 2)):
    body = bytes((mid, len(params) + 2, instruction, *params))
    return b"\xff\xff" + body + bytes((~sum(body) & 255,))


class LeaderTests(unittest.TestCase):
    def capture_fixture(self, position, kind="zero", frozen=False, torque=0):
        now = [10.0]
        def snapshot():
            t = now[0]
            return Sample(tuple(position(t)), (torque,) * 8, 10 if frozen else t, 0.01), "", 1
        reader = SimpleNamespace(snapshot=snapshot)
        def sleep(dt):
            now[0] += dt
        return collect_stable({"left": reader}, kind, clock=lambda: now[0], sleep=sleep)

    def test_stable_capture_handles_encoder_wrap(self):
        captured = self.capture_fixture(lambda t: ((4095 if int(t * 50) % 2 else 0),) * 8)
        self.assertIn(captured["left"].positions[0], (4095, 0))

    def test_capture_rejects_movement_frozen_feedback_and_enabled_servos(self):
        for kwargs in ({"position": lambda t: (int((t - 10) * 100),) * 8},
                       {"position": lambda t: (2048,) * 8, "frozen": True},
                       {"position": lambda t: (2048,) * 8, "torque": 1}):
            with self.assertRaises(ValueError):
                self.capture_fixture(**kwargs)

    def test_endpoint_capture_checks_gripper_not_arm_motion(self):
        captured = self.capture_fixture(lambda t: (int((t - 10) * 100),) * 7 + (100,), kind="open")
        self.assertEqual(captured["left"].positions[7], 100)

    def test_viewer_follows_changed_samples_without_a_freeze_preview_control(self):
        from openarm_leader.app import serve
        server = MagicMock()
        server.get_port.return_value = 8890
        viser, numpy = ModuleType("viser"), ModuleType("numpy")
        viser.ViserServer = Mock(return_value=server)
        numpy.asarray = lambda values: values
        views = {side: Mock() for side in ("left", "right")}
        roots = {side: Mock() for side in views}
        joint_map = {}
        for side, view in views.items():
            names = [f"openarm_{side}_joint{i}" for i in range(1, 8)] + [f"openarm_{side}_finger_joint1"]
            view.get_actuated_joint_names.return_value = names
            joint_map.update({n: SimpleNamespace(limit=SimpleNamespace(lower=-math.pi, upper=math.pi)) for n in names})
        readers = {path: Mock() for path in ("a", "b")}
        now = time.monotonic()
        for reader in readers.values():
            reader.snapshot.side_effect = [
                (Sample((2048,) * 8, (0,) * 8, now, 0.01), "", 1),
                (Sample((2148,) * 8, (0,) * 8, now, 0.01), "", 2),
            ]
        with tempfile.TemporaryDirectory() as root, \
             patch.dict(sys.modules, {"viser": viser, "numpy": numpy}), \
             patch("openarm_leader.app.check_port"), \
             patch("openarm_leader.app.load_model", return_value=SimpleNamespace(joint_map=joint_map)), \
             patch("openarm_leader.app.create_scene", return_value=(roots, views)), \
             patch("openarm_leader.app.Reader", side_effect=lambda path: readers[path]), \
             patch("openarm_leader.app.time.sleep", side_effect=[None, KeyboardInterrupt]):
            serve(Path(root), "a", "b", Path(root) / "cal.json")
        for side, view in views.items():
            first, second = view.update_cfg.call_args_list
            self.assertEqual(first.args[0][0], 0)
            self.assertNotEqual(second.args[0][0], 0)
            self.assertTrue(roots[side].visible)
        self.assertFalse(any("预览" in c.args[0] or "零姿态" in c.args[0]
                             for c in server.gui.add_checkbox.call_args_list))
        for reader in readers.values():
            reader.close.assert_called_once()
        server.stop.assert_called_once()

    def test_wire_boundary_rejects_writes_broadcast_and_unapproved_reads(self):
        for mid in range(1, 9):
            for params in ((3, 2), (40, 24)):
                validate_read_packet(packet(mid, params=params))
        for data in (packet(instruction=3, params=(40, 1)), packet(254, 0x83),
                     packet(params=(42, 2)), packet(9, params=(3, 2)),
                     packet(params=(3, 2))[:-1] + b"\x00"):
            with self.assertRaises(ValueError):
                validate_read_packet(data)

    def test_sample_decodes_signed_position_and_torque_without_commands(self):
        bus = LeaderBus.__new__(LeaderBus)
        bus.errors = {}
        data = bytearray(24)
        data[0] = 1
        data[22] = 49
        data[16:18] = (0x8005).to_bytes(2, "little")
        bus.read = Mock(return_value=bytes(data))
        sample = bus.sample()
        self.assertEqual(sample.positions, (-5,) * 8)
        self.assertEqual(sample.torque, (1,) * 8)
        self.assertEqual(sample.voltages, (4.9,) * 8)
        self.assertTrue(sample.fresh(time.monotonic()))
        self.assertEqual(bus.read.call_count, 8)
        self.assertTrue(all(call.args[1:] == (40, 24) for call in bus.read.call_args_list))

    def test_voltage_only_alarm_preserves_data_but_other_faults_reject(self):
        bus = LeaderBus.__new__(LeaderBus)
        bus.errors = {}
        bus.port, bus.handler = Mock(), Mock()
        data = bytearray(24)
        data[22] = 39
        bus.handler.readTxRx.return_value = (data, 0, 1)
        bus.handler.getRxPacketError.return_value = "Input voltage error"
        self.assertEqual(bus.read(7, 40, 24), bytes(data))
        self.assertEqual(bus.errors[7], 1)
        for result, error, payload in ((-6, 1, data), (0, 3, data), (0, 1, data[:12])):
            bus.handler.readTxRx.return_value = (payload, result, error)
            with self.assertRaises(OSError):
                bus.read(7, 40, 24)

    def test_remap_signs_wrap_and_per_device_alignment_survive_swap_save(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "calibration.json"
            m = Mapping("/dev/left", "/dev/right", path)
            origin = Sample((4090,) * 7 + (100,), (0,) * 8, 10, 0.01)
            m.capture("left", origin, "zero", 10.1)
            positions = [((4090 + mid * 10) % 4096) for mid in range(1, 8)] + [100]
            angles, opening = m.angles("left", positions)
            self.assertIsNone(opening)
            for index, mid in enumerate(SOURCE_IDS):
                expected = mid * 10 * math.tau / 4096 * m.data["signs"]["left"][index]
                self.assertAlmostEqual(angles[index], expected)
            m.swap()
            self.assertEqual(m.device("right")["zero"], [4090] * 7)
            self.assertIsNone(m.device("left")["zero"])
            m.save()
            loaded = Mapping("/dev/left", "/dev/right", path)
            self.assertEqual(loaded.data, m.data)
            with self.assertRaises(ValueError):
                Mapping("/dev/other", "/dev/right", path)
            self.assertEqual(delta(3, 4090), 9)

    def test_gripper_reverse_wrap_endpoints_and_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            m = Mapping("a", "b", Path(root) / "cal.json")
            def sample(pos, torque=0):
                return Sample((2048,) * 7 + (pos,), (torque,) * 8, 10, 0.01)
            m.capture("right", sample(50), "closed", 10.1)
            m.capture("right", sample(3950), "open", 10.1)
            self.assertEqual(m.angles("right", sample(50).positions)[1], 0)
            self.assertEqual(m.angles("right", sample(3950).positions)[1], 1)
            self.assertAlmostEqual(m.angles("right", sample(4048).positions)[1], 0.5)
            with self.assertRaises(ValueError):
                m.capture("right", sample(51), "open", 10.1)
            for invalid, now in ((sample(200), 11), (sample(200, 1), 10.1)):
                with self.assertRaises(ValueError):
                    m.capture("right", invalid, "zero", now)
            self.assertIsNone(m.device("right")["zero"])

    def test_reader_failure_marks_last_good_sample_invalid_and_closes_port(self):
        reader = Reader("unused")
        sample = Sample((0,) * 8, (0,) * 8, time.monotonic(), 0.01)
        bus = Mock()
        def read_sample():
            if reader.count == 0:
                return sample
            reader.stop.set()
            raise OSError("unplugged")
        bus.sample.side_effect = read_sample
        with patch("openarm_leader.transport.LeaderBus", return_value=bus):
            reader.run()
        actual, error, count = reader.snapshot()
        self.assertIs(actual, sample)
        self.assertIn("unplugged", error)
        self.assertEqual(count, 1)
        bus.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
