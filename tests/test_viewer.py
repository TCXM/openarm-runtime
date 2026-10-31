import hashlib
import os
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree as ET


from openarm_viewer.model import (
    ARM,
    asset_path,
    joint_configuration,
    single_arm_xml,
    verify_blob,
)
from openarm_runtime.telemetry import (
    ALLOWED_FD_FRAMES,
    ALLOWED_FRAMES,
    ArmConnection,
    MotorRange,
    MotorState,
    ReadOnlyCAN,
    Telemetry,
    decode_state,
    model_ready,
    poll,
    read_frame,
    select_format,
)


class ProtocolTests(unittest.TestCase):
    def test_outbound_allowlist_contains_only_reads(self):
        self.assertEqual(len(ALLOWED_FRAMES), 40)
        self.assertEqual(len(ALLOWED_FD_FRAMES), 40)
        for frame in ALLOWED_FRAMES | ALLOWED_FD_FRAMES:
            if len(frame) == 16:
                can_id, size, data = struct.unpack("=IB3x8s", frame)
            else:
                can_id, size, flags, padded = struct.unpack("=IBB2x64s", frame)
                self.assertEqual(flags, 1)
                self.assertEqual(padded[8:], bytes(56))
                data = padded[:8]
            self.assertEqual((can_id, size), (0x7FF, 8))
            self.assertIn(data[0], range(1, 9))
            self.assertEqual(data[1], 0)
            self.assertEqual(data[4:], bytes(4))
            self.assertIn(
                (data[2], data[3]), ((0xCC, 0), (0x33, 21), (0x33, 22), (0x33, 23), (0x33, 35))
            )

    def test_motor_control_and_parameter_writes_cannot_be_sent(self):
        reader = ReadOnlyCAN.__new__(ReadOnlyCAN)
        reader.socket = Mock()
        candidates = [
            struct.pack("=IB3x8s", 1, 8, b"\xff" * 7 + bytes((op,))) for op in (0xFC, 0xFD, 0xFE)
        ]
        candidates.extend(
            (
                struct.pack("=IB3x8s", 0x7FF, 8, bytes((1, 0, 0x55, 9, 1, 0, 0, 0))),
                struct.pack("=IB3x8s", 1, 8, bytes(8)),
                read_frame(1) + bytes(56),  # CAN-FD frames are not allowed either.
                struct.pack("=IBB2x64s", 1, 8, 1, b"\xff" * 7 + b"\xfc"),
                struct.pack("=IBB2x64s", 0x7FF, 8, 1, bytes((1, 0, 0x55, 35, 9, 0, 0, 0))),
            )
        )
        for packet in candidates:
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                reader._send(packet)
        reader.socket.send.assert_not_called()
        reader.frame_format = "fd"
        for packet in candidates:
            with self.subTest(fd_packet=packet), self.assertRaises(ValueError):
                reader._send(packet)
        reader.socket.send.assert_not_called()

    def test_invalid_ids_and_parameters(self):
        for motor in (0, 9, -1, True, 1.0):
            with self.assertRaises(ValueError):
                read_frame(motor)
        for rid in (7, 9, 10, 36, True, 21.0):
            with self.assertRaises(ValueError):
                read_frame(1, rid)
        with self.assertRaises(ValueError):
            read_frame(1, frame_format="invalid")

    def test_decode_matches_firmware_ranges(self):
        state = decode_state(
            0x11, bytes.fromhex("017faa8018011818"), 1, MotorRange(12.5, 45, 54), 10
        )
        self.assertIsNotNone(state)
        self.assertAlmostEqual(state.position, (0x7FAA / 65535 * 2 - 1) * 12.5)
        self.assertEqual(
            (state.status, state.mos_temperature, state.rotor_temperature), (0, 24, 24)
        )
        self.assertTrue(state.fresh(10.2))
        self.assertFalse(state.fresh(10.5))
        self.assertFalse(state.fresh(9.9))

    def test_wrong_id_flags_short_and_parameter_packets_rejected(self):
        limits = MotorRange(12.5, 45, 54)
        valid = bytes.fromhex("017faa8018011818")
        for cid, data in (
            (0x12, valid),
            (0x80000011, valid),
            (0x40000011, valid),
            (0x11, valid[:7]),
            (0x11, bytes.fromhex("0100331500004841")),
            (0x11, b"\x02" + valid[1:]),
        ):
            self.assertIsNone(decode_state(cid, data, 1, limits, 0))

    def test_invalid_ranges_fail_instead_of_fake_units(self):
        for value in (0, -1, float("nan"), float("inf"), 100_000):
            with self.assertRaises(ValueError):
                MotorRange(value, 45, 54)

    def test_model_hidden_for_missing_stale_or_faulted_connection(self):
        states = {i: MotorState(i, 0.1, 0, 0, 24, 24, 0, 10) for i in range(1, 8)}
        self.assertTrue(model_ready(states, 10.1))
        self.assertFalse(model_ready(states, 10.5))
        self.assertFalse(model_ready(states, 10.1, "CAN disconnected"))
        for missing in range(1, 8):
            partial = {i: state for i, state in states.items() if i != missing}
            self.assertFalse(model_ready(partial, 10.1))
        self.assertFalse(model_ready({}, 10))
        self.assertFalse(model_ready(states, 10.1, gripper=True))
        states[8] = MotorState(8, 0.2, 0, 0, 24, 24, 0, 10)
        self.assertTrue(model_ready(states, 10.1, gripper=True))

    def test_transport_reads_and_shutdown_emit_no_control(self):
        reader = ReadOnlyCAN.__new__(ReadOnlyCAN)
        sock = reader.socket = Mock()
        responses = []

        def send(packet):
            _, _, data = struct.unpack("=IB3x8s", packet)
            if data[2] == 0x33:
                value = {21: 12.5, 22: 45.0, 23: 54.0}[data[3]]
                reply = data[:4] + struct.pack("<f", value)
            else:
                reply = bytes.fromhex("017faa8018011818")
            responses.append(struct.pack("=IB3x8s", 0x11, 8, reply))
            return 16

        sock.send.side_effect = send
        sock.recv.side_effect = lambda _: responses.pop(0)
        limits = reader.read_range(1)
        self.assertEqual(limits, MotorRange(12.5, 45, 54))
        self.assertEqual(reader.refresh(1, limits).status, 0)
        reader.close()
        self.assertEqual(sock.send.call_count, 4)
        self.assertTrue(all(call.args[0] in ALLOWED_FRAMES for call in sock.send.call_args_list))
        sock.close.assert_called_once()

    def test_timeout_and_bus_fault_do_not_return_zero_state(self):
        reader = ReadOnlyCAN.__new__(ReadOnlyCAN)
        reader.socket = Mock()
        reader.socket.send.return_value = 16
        reader.socket.recv.side_effect = socket.timeout
        self.assertIsNone(reader.refresh(1, MotorRange(12.5, 45, 54)))
        with self.assertRaises(RuntimeError):
            reader.read_range(1)
        reader.socket.recv.side_effect = None
        reader.socket.recv.return_value = struct.pack("=IB3x8s", 0x20000040, 8, bytes(8))
        with self.assertRaises(RuntimeError):
            reader.refresh(1, MotorRange(12.5, 45, 54))

    def test_poller_failure_retains_last_sample_and_closes(self):
        import threading
        from unittest.mock import patch

        shared, stop = Telemetry(), threading.Event()
        shared.states[1] = MotorState(1, 0.3, 0, 0, 24, 24, 0, 0)
        fake = Mock()
        fake.read_parameter.side_effect = RuntimeError("disconnected")
        with (
            patch("openarm_runtime.telemetry.ReadOnlyCAN", return_value=fake),
            patch("openarm_runtime.telemetry.inspect_link", return_value={}),
        ):
            poll("can1", shared, stop)
        self.assertIn("disconnected", shared.error)
        self.assertEqual(shared.states[1].position, 0.3)
        fake.close.assert_called_once()
        fake.refresh.assert_not_called()
        self.assertFalse(shared.states[1].fresh(1))

    def test_fd_reply_is_not_truncated_and_uint_baud_read(self):
        reader = ReadOnlyCAN.__new__(ReadOnlyCAN)
        reader.socket = Mock()
        reader.frame_format = "fd"
        reader.socket.send.return_value = 72
        reader.socket.recv.side_effect = [
            struct.pack("=IBB2x64s", 0x11, 8, 5, bytes.fromhex("0100332309000000")),
            struct.pack("=IBB2x64s", 0x11, 8, 5, bytes.fromhex("017faa8018011818")),
        ]
        self.assertEqual(reader.read_parameter(1, 35), 9)
        state = reader.refresh(1, MotorRange(12.5, 45, 54))
        self.assertEqual(state.frame_format, "fd")
        for call in reader.socket.send.call_args_list:
            self.assertIn(call.args[0], ALLOWED_FD_FRAMES)
        with self.assertRaises(ValueError):
            reader._send(read_frame(1))

    def test_baud_readback_does_not_accept_a_write_echo(self):
        reader = ReadOnlyCAN.__new__(ReadOnlyCAN)
        reader.socket = Mock()
        reader.socket.send.return_value = 16
        reader.socket.recv.side_effect = [
            struct.pack("=IB3x8s", 0x11, 8, bytes.fromhex("0100552309000000")),
            struct.pack("=IB3x8s", 0x11, 8, bytes.fromhex("0100332304000000")),
        ]
        self.assertEqual(reader.read_parameter(1, 35), 4)

    def test_parameter_retry_is_bounded_and_never_retries_bus_errors(self):
        reader = ReadOnlyCAN.__new__(ReadOnlyCAN)
        reader._read_parameter_once = Mock(side_effect=[None, 9])
        self.assertEqual(reader.read_parameter(1, 35), 9)
        self.assertEqual(reader._read_parameter_once.call_count, 2)
        reader._read_parameter_once = Mock(return_value=None)
        with self.assertRaisesRegex(RuntimeError, "no reply"):
            reader.read_parameter(1, 35)
        self.assertEqual(reader._read_parameter_once.call_count, 2)
        reader._read_parameter_once = Mock(side_effect=RuntimeError("CAN error"))
        with self.assertRaisesRegex(RuntimeError, "CAN error"):
            reader.read_parameter(1, 35)
        reader._read_parameter_once.assert_called_once()

    def test_refresh_retries_once_with_only_allowed_reads(self):
        reader = ReadOnlyCAN.__new__(ReadOnlyCAN)
        reader._send = Mock()
        data = bytes.fromhex("017faa8018011818")
        reader._responses = Mock(side_effect=[iter(()), iter(((0x11, data, "classic"),))])
        self.assertIsNotNone(reader.refresh(1, MotorRange(12.5, 45, 54)))
        self.assertEqual(reader._send.call_count, 2)
        for call in reader._send.call_args_list:
            self.assertIn(call.args[0], ALLOWED_FRAMES)
        reader._send.reset_mock()
        reader._responses = Mock(side_effect=RuntimeError("CAN error"))
        with self.assertRaisesRegex(RuntimeError, "CAN error"):
            reader.refresh(1, MotorRange(12.5, 45, 54))
        reader._send.assert_called_once()


def host_link(fd=True):
    return {
        "flags": ["UP"],
        "linkinfo": {
            "info_data": {
                "state": "ERROR-ACTIVE",
                "ctrlmode": ["FD"] if fd else [],
                "bittiming": {"bitrate": 1_000_000},
                "data_bittiming": {"bitrate": 5_000_000},
            }
        },
    }


class ConfigurationTests(unittest.TestCase):
    def test_explicit_compat_keeps_classic_requests_with_mixed_replies(self):
        import threading
        from unittest.mock import patch

        codes = {i: 4 if i in (4, 6, 8) else 9 for i in range(1, 9)}
        self.assertEqual(select_format("compat", host_link(), codes), "classic")
        self.assertEqual(
            select_format("compat", host_link(), dict.fromkeys(range(1, 9), 9)), "classic"
        )
        for host, config in (
            (host_link(False), codes),
            (host_link(), {1: 9}),
            (host_link(), {**codes, 1: 10}),
        ):
            with self.assertRaises(ValueError):
                select_format("compat", host, config)
        fake = Mock()
        fake.frame_format = "classic"
        fake.read_parameter.side_effect = list(codes.values())
        fake.read_range.return_value = MotorRange(12.5, 45, 54)

        def refresh(motor, _limits):
            self.assertEqual(fake.frame_format, "classic")
            return MotorState(
                motor, 0.1, 0, 0, 24, 24, 0, 10, "classic" if codes[motor] == 4 else "fd"
            )

        fake.refresh.side_effect = refresh
        shared = Telemetry()
        with (
            patch("openarm_runtime.telemetry.ReadOnlyCAN", return_value=fake),
            patch("openarm_runtime.telemetry.inspect_link", return_value=host_link()),
        ):
            poll("can0", shared, threading.Event(), frame_format="compat", max_cycles=1)
        self.assertEqual(shared.error, "")
        self.assertEqual(shared.frame_format, "classic")
        self.assertEqual(shared.metadata()["requested_format"], "compat")
        self.assertEqual((shared.rx_classic, shared.rx_fd), (3, 5))
        self.assertIn("非全FD验收", shared.message)
        fake.close.assert_called_once()

    def test_bimanual_gui_updates_independent_arms_and_stops_both_readers(self):
        import time
        import types
        from unittest.mock import MagicMock, patch

        from openarm_viewer.app import serve

        for failed_interface in (None, "can0"):
            now = time.monotonic()
            connections = {}
            for iface in ("can1", "can0"):
                shared = Telemetry()
                shared.states = {
                    i: MotorState(i, 0.1 if iface == "can1" else 0.2, 0, 0, 24, 24, 0, now, "fd")
                    for i in range(1, 9)
                }
                shared.error = "disconnected" if iface == failed_interface else ""
                connections[iface] = types.SimpleNamespace(
                    shared=shared, restart=Mock(), close=Mock()
                )
            server = MagicMock()
            server.get_port.return_value = 8890
            roots, visuals, buttons = {}, {}, []

            def add_frame(name, **_kwargs):
                roots[name] = Mock()
                return roots[name]

            def add_dropdown(_label, options, **_kwargs):
                return types.SimpleNamespace(value=options[0])

            class Button:
                disabled = False

                def on_click(self, callback):
                    self.callback = callback
                    return callback

            def add_button(_label):
                button = Button()
                buttons.append(button)
                return button

            def visual(_server, side, **_kwargs):
                view = Mock()
                view.get_actuated_joint_names.return_value = tuple(
                    f"openarm_{side}_joint{i}" for i in range(1, 8)
                ) + (f"openarm_{side}_finger_joint1",)
                visuals[side] = view
                return view

            server.scene.add_frame.side_effect = add_frame
            server.gui.add_dropdown.side_effect = add_dropdown
            server.gui.add_button.side_effect = add_button
            viser = types.ModuleType("viser")
            viser.ViserServer = Mock(return_value=server)
            extras = types.ModuleType("viser.extras")
            extras.ViserUrdf = visual
            numpy = types.ModuleType("numpy")
            numpy.asarray = lambda values: values
            with (
                patch.dict(sys.modules, {"viser": viser, "viser.extras": extras, "numpy": numpy}),
                patch("openarm_viewer.app.check_port"),
                patch("openarm_viewer.app.load_model", side_effect=lambda _root, side, **_k: side),
                patch("openarm_viewer.app.ArmConnection", side_effect=lambda i, _f: connections[i]),
                patch("openarm_viewer.app.time.sleep", side_effect=KeyboardInterrupt),
            ):
                serve(Path("unused-test-assets"), "can1", "can0", 8890)
            self.assertTrue(roots["/robot_left"].visible)
            self.assertEqual(roots["/robot_right"].visible, failed_interface is None)
            self.assertEqual(visuals["left"].update_cfg.call_args.args[0][0], 0.1)
            if failed_interface is None:
                self.assertEqual(visuals["right"].update_cfg.call_args.args[0][0], 0.2)
            else:
                visuals["right"].update_cfg.assert_not_called()
            for connection in connections.values():
                connection.close.assert_called_once()
            buttons[1].callback(None)
            self.assertEqual(connections["can0"].restart.call_count, 2)
            self.assertEqual(connections["can1"].restart.call_count, 1)

    def test_poller_mixed_bauds_stops_before_state_or_fd_requests(self):
        import threading
        from unittest.mock import patch

        fake = Mock()
        fake.frame_format = "classic"
        fake.read_parameter.side_effect = [9, 9, 9, 4, 9, 4, 9, 9]
        shared = Telemetry()
        with (
            patch("openarm_runtime.telemetry.ReadOnlyCAN", return_value=fake),
            patch("openarm_runtime.telemetry.inspect_link", return_value=host_link()),
        ):
            poll("can0", shared, threading.Event(), max_cycles=1)
        self.assertIn("Mixed motor can_br", shared.error)
        self.assertEqual(fake.frame_format, "classic")
        self.assertEqual(shared.baud_codes[4], 4)
        fake.read_range.assert_not_called()
        fake.refresh.assert_not_called()
        fake.close.assert_called_once()

    def test_poller_selects_fd_only_after_all_eight_codes_and_host_match(self):
        import threading
        from unittest.mock import patch

        fake = Mock()
        fake.frame_format = "classic"
        fake.read_parameter.return_value = 9
        fake.read_range.return_value = MotorRange(12.5, 45, 54)
        fake.refresh.side_effect = lambda motor, _limits: MotorState(
            motor, 0.1, 0, 0, 24, 24, 0, 10, "fd"
        )
        shared = Telemetry()
        with (
            patch("openarm_runtime.telemetry.ReadOnlyCAN", return_value=fake),
            patch("openarm_runtime.telemetry.inspect_link", return_value=host_link()),
        ):
            poll("can1", shared, threading.Event(), max_cycles=1)
        self.assertEqual(shared.error, "")
        self.assertEqual(shared.frame_format, "fd")
        self.assertEqual(shared.rx_fd, 8)
        self.assertEqual(shared.rx_classic, 0)
        self.assertEqual(fake.read_parameter.call_count, 8)
        self.assertEqual(fake.refresh.call_count, 8)
        fake.close.assert_called_once()

    def test_auto_matches_motor_codes_not_just_host_fd_flag(self):
        self.assertEqual(
            select_format("auto", host_link(), dict.fromkeys(range(1, 9), 4)), "classic"
        )
        self.assertEqual(select_format("auto", host_link(), dict.fromkeys(range(1, 9), 9)), "fd")

    def test_mixed_incomplete_unknown_and_mismatched_profiles_refused(self):
        classic = dict.fromkeys(range(1, 9), 4)
        fd = dict.fromkeys(range(1, 9), 9)
        mixed = {**fd, 5: 4}
        cases = (
            ("auto", host_link(), mixed),
            ("auto", host_link(), {1: 9}),
            ("fd", host_link(), classic),
            ("classic", host_link(), fd),
            ("auto", host_link(False), fd),
            ("auto", host_link(), dict.fromkeys(range(1, 9), 10)),
        )
        for requested, host, codes in cases:
            with self.subTest(requested=requested, codes=codes), self.assertRaises(ValueError):
                select_format(requested, host, codes)
        host = host_link()
        host["linkinfo"]["info_data"]["data_bittiming"]["bitrate"] = 2_000_000
        with self.assertRaises(ValueError):
            select_format("fd", host, fd)

    def test_duplicate_interfaces_rejected(self):
        from openarm_viewer.app import validate_interfaces

        validate_interfaces("can1", "can0")
        with self.assertRaises(ValueError):
            validate_interfaces("can1", "can1")

    def test_two_workers_and_identical_ids_do_not_share_states(self):
        import threading
        from unittest.mock import patch

        ready = {"can0": threading.Event(), "can1": threading.Event()}

        def fake_poll(interface, shared, stop, **_kwargs):
            shared.states[1] = MotorState(
                1, 0.1 if interface == "can0" else 0.9, 0, 0, 24, 24, 0, 10
            )
            ready[interface].set()
            stop.wait(2)

        left, right = ArmConnection("can1"), ArmConnection("can0")
        with patch("openarm_runtime.telemetry.poll", side_effect=fake_poll):
            try:
                left.restart()
                right.restart()
                self.assertTrue(ready["can1"].wait(1))
                self.assertTrue(ready["can0"].wait(1))
                self.assertEqual(left.shared.states[1].position, 0.9)
                self.assertEqual(right.shared.states[1].position, 0.1)
                old_right = right.shared
                left.close()
                self.assertTrue(right.worker.is_alive())
                left.restart()
                self.assertIs(right.shared, old_right)
            finally:
                left.close()
                right.close()


class ModelTests(unittest.TestCase):
    def test_occupied_port_is_not_reused_or_killed(self):
        from openarm_viewer.app import check_port

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaises(OSError):
                check_port(listener.getsockname()[1])
            self.assertGreater(listener.fileno(), -1)

    @unittest.skipUnless(
        os.environ.get("OPENARM_VIEWER_ASSET_ROOT"), "Requires pinned visual assets"
    )
    def test_official_meshes_and_live_kinematics(self):
        import numpy as np
        from openarm_viewer.model import load_model

        for side in ("left", "right"):
            model = load_model(Path(os.environ["OPENARM_VIEWER_ASSET_ROOT"]), side)
            self.assertEqual(len(model.actuated_joint_names), 8)
            self.assertGreaterEqual(len(model.scene.geometry), 10)
            self.assertTrue(np.isfinite(model.scene.bounds).all())
            child = f"openarm_{side}_link1"
            parent = f"openarm_{side}_base_link"
            before = model.get_transform(child, parent).copy()
            cfg = np.zeros(8)
            cfg[model.actuated_joint_names.index(f"openarm_{side}_joint1")] = 0.2
            model.update_cfg(cfg)
            self.assertFalse(np.allclose(before, model.get_transform(child, parent)))
        body = load_model(Path(os.environ["OPENARM_VIEWER_ASSET_ROOT"]), "body", component=True)
        self.assertEqual(len(body.actuated_joint_names), 0)
        for side in ("left", "right"):
            arm = load_model(Path(os.environ["OPENARM_VIEWER_ASSET_ROOT"]), side, component=True)
            self.assertEqual(len(arm.actuated_joint_names), 8)
            self.assertFalse(any("body_link0" in key for key in arm.scene.geometry))

    def test_extract_one_arm_without_ros_control_or_other_geometry(self):
        data = b"""<robot name="test"><link name="world"/>
        <link name="openarm_body_link0"/><link name="openarm_left_link1"><collision/></link>
        <link name="openarm_right_link1"/>
        <joint name="left" type="fixed"><parent link="world"/><child link="openarm_left_link1"/></joint>
        <joint name="right" type="fixed"><parent link="world"/><child link="openarm_right_link1"/></joint>
        <ros2_control name="never_load"/></robot>"""
        root = ET.fromstring(single_arm_xml(data, "left"))
        self.assertNotIn("openarm_right_link1", [x.attrib["name"] for x in root.findall("link")])
        self.assertIsNone(root.find("ros2_control"))
        self.assertIsNone(root.find(".//collision"))
        self.assertEqual([x.attrib["name"] for x in root.findall("joint")], ["left"])
        with self.assertRaises(ValueError):
            single_arm_xml(data, "unknown")

    def test_joint_order_mapping_and_unmapped_gripper(self):
        states = {i: MotorState(i, i / 10, 0, 0, 24, 24, 0, 0) for i in range(1, 9)}
        names = ("openarm_left_joint7", "openarm_left_joint1", "openarm_left_finger_joint1")
        self.assertEqual(joint_configuration(names, states, "left"), [0.7, 0.1, 0])
        for names in (("openarm_right_joint1",), ("openarm_left_joint8",)):
            with self.assertRaises(ValueError):
                joint_configuration(names, states, "left")
        with self.assertRaises(ValueError):
            joint_configuration(("openarm_left_joint1",), {}, "left")
        self.assertEqual(
            joint_configuration(("openarm_left_finger_joint1",), states, "left", "official-pinch"),
            [0.8],
        )
        with self.assertRaises(ValueError):
            joint_configuration(("openarm_left_finger_joint1",), {}, "left", "official-pinch")

    def test_asset_hash_and_path_escape(self):
        data = b"example"
        expected = hashlib.sha1(b"blob 7\0" + data).hexdigest()
        verify_blob(data, expected)
        with self.assertRaises(ValueError):
            verify_blob(data + b"!", expected)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            outside = Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "assets").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                asset_path(root, ARM + "link1.dae")
            with self.assertRaises(ValueError):
                asset_path(root, "../../secret")


if __name__ == "__main__":
    unittest.main()
