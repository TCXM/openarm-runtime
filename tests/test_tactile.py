"""Offline tactile adaptation tests; no SDK, GUI or serial hardware required."""
from contextlib import contextmanager
from importlib.util import find_spec
import math
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from openarm_tactile.source import (
    POINT_COUNT, SENSOR, Sample, TactileReader, open_board, read_sample, simulation_sample, validate_reply,
)


def fake_board(count=68, resultant=(-2, 3, 255), point=(-128, 127, 255)):
    return SimpleNamespace(
        read_resultant_force=lambda: resultant,
        read_distributed_force=lambda: [SimpleNamespace(as_tuple=lambda: point)
                                        for _ in range(count)],
    )


def wire_pair(size=3):
    # Same read-only wire layout as the vendor example; no SDK dependency.
    request = bytearray.fromhex("55 aa 09 00 02 00 fb f0 03 00 00 03 00 00")
    request[11:13] = size.to_bytes(2, "little")
    request[-1] = -sum(request[:-1]) & 255
    response = bytearray(b"\xaa\x55")
    response.extend((size + 10).to_bytes(2, "little"))
    response.extend(request[4:13])
    response.extend(b"\x00" + b"\x00" * size)
    response.append(-sum(response) & 255)
    return bytes(request), bytes(response)


class TactileSampleTests(unittest.TestCase):
    def test_preserves_signed_counts_and_unsigned_fz(self):
        sample = read_sample(fake_board())
        self.assertEqual(sample.resultant, (-2, 3, 255))
        self.assertEqual(sample.forces, ((-128, 127, 255),) * 68)

    def test_rejects_wrong_sensor_length(self):
        for count in (0, 52, 69):
            with self.subTest(count=count), self.assertRaises(ValueError):
                read_sample(fake_board(count))

    def test_rejects_invalid_resultant_and_point_data(self):
        for bad in ((0, 0), (-129, 0, 0), (0, 128, 0), (0, 0, -1),
                    (0, 0, 256), (float("nan"), 0, 0), (0, float("inf"), 0)):
            for field in ("resultant", "point"):
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    read_sample(fake_board(**{field: bad}))

    def test_simulation_tracks_coordinate_origin(self):
        coords = [(x, y, 2) for x in range(4) for y in range(17)]
        original = simulation_sample(coords, 0.5)
        translated = simulation_sample([(x + 100, y - 80, z) for x, y, z in coords], 0.5)
        self.assertEqual(len(original.forces), 68)
        for a, b in zip(original.forces, translated.forces):
            for first, second in zip(a, b):
                self.assertAlmostEqual(first, second)


class TactileReplyTests(unittest.TestCase):
    def test_accepts_resultant_and_complete_distribution(self):
        for size in (3, 204):
            request, response = wire_pair(size)
            self.assertEqual(validate_reply(request, response), response)

    def test_rejects_reply_to_other_device_function_or_register(self):
        request, response = wire_pair()
        for offset in (4, 6, 7, 11):
            other = bytearray(response)
            other[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, "match requested"):
                validate_reply(request, bytes(other))

    def test_rejects_incomplete_extra_or_falsely_declared_payload(self):
        request, response = wire_pair()
        for malformed in (response[:-1], response + b"\x00"):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(ValueError, "length"):
                validate_reply(request, malformed)
        truncated = bytearray(response[:-1])
        truncated[2:4] = (len(truncated) - 5).to_bytes(2, "little")
        with self.assertRaisesRegex(ValueError, "payload length"):
            validate_reply(request, bytes(truncated))

    def test_rejects_missing_or_wrong_header(self):
        request, response = wire_pair()
        for malformed in (b"", response[:14], b"\x55\xaa" + response[2:]):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(ValueError, "header"):
                validate_reply(request, malformed)


class TactileReaderTests(unittest.TestCase):
    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("capture worker did not reach the expected state")

    def test_optional_sdk_simulation_loads_coordinates_without_opening_serial(self):
        if find_spec("paxini_sdk") is None:
            self.skipTest("Optional tactile SDK is not installed")
        from paxini_sdk.sensor_registry import load_points

        reader = TactileReader(simulate=True)
        with patch("paxini_sdk.sensor_registry.load_points", wraps=load_points) as coordinates, \
                patch("openarm_tactile.source.open_board", side_effect=AssertionError("opened hardware")), \
                patch("serial.Serial", side_effect=AssertionError("opened serial port")):
            with reader:
                self.wait_for(lambda: reader.snapshot()["sequence"] >= 2)
                snapshot = reader.snapshot()
                self.assertEqual(snapshot["state"], "live")
                self.assertIsNone(snapshot["error"])
                self.assertIsInstance(snapshot["sample"], Sample)
                self.assertEqual(len(snapshot["sample"].forces), POINT_COUNT)
                for triple in (snapshot["sample"].resultant, *snapshot["sample"].forces):
                    self.assertEqual(len(triple), 3)
                    self.assertTrue(all(math.isfinite(value) for value in triple))
                coordinates.assert_called_once_with(SENSOR)
        self.assertEqual(reader.snapshot()["state"], "closed")
        self.assertFalse(reader._thread.is_alive())

    def test_rejects_ambiguous_sources_and_invalid_parameters(self):
        cases = ({}, {"device": "fake", "simulate": True},
                 {"simulate": True, "device_id": 0},
                 {"simulate": True, "device_id": 256},
                 {"simulate": True, "device_id": 1.5},
                 {"simulate": True, "device_id": True},
                 {"simulate": True, "baudrate": 0},
                 {"simulate": True, "baudrate": -1},
                 {"simulate": True, "baudrate": 921600.5},
                 {"simulate": True, "baudrate": True},
                 {"simulate": True, "baudrate": float("nan")},
                 {"simulate": True, "baudrate": float("inf")})
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                TactileReader(**args)

    def test_failure_closes_port_and_requires_explicit_restart(self):
        opens, closes = [], []

        @contextmanager
        def factory(device, baudrate, device_id):
            opens.append((device, baudrate, device_id))
            board = fake_board()
            if len(opens) == 1:
                def fail():
                    raise OSError("sensor disconnected")
                board.read_resultant_force = fail
            try:
                yield board
            finally:
                closes.append(True)

        with patch("openarm_tactile.source.open_board", factory):
            reader = TactileReader("fake", device_id=2)
            try:
                reader.start()
                self.wait_for(lambda: reader.snapshot()["state"] == "error")
                state = reader.snapshot()
                self.assertIn("sensor disconnected", state["error"])
                self.assertIsNone(state["sample"])
                self.assertEqual(len(closes), 1)
                time.sleep(0.02)
                self.assertEqual(len(opens), 1)
                reader.restart()
                self.wait_for(lambda: reader.snapshot()["sequence"] >= 1)
                state = reader.snapshot()
                self.assertEqual(state["state"], "live")
                self.assertIsNone(state["error"])
                self.assertEqual(state["sample"].resultant, (-2, 3, 255))
                self.assertEqual(opens, [("fake", 921600, 2)] * 2)
            finally:
                reader.close()
            self.assertEqual(len(closes), 2)
            self.assertEqual(reader.snapshot()["state"], "closed")
            self.assertFalse(reader._thread.is_alive())
            reader.close()  # Idempotent cleanup.
            with self.assertRaises(RuntimeError):
                reader.restart()

    def test_stale_state_preserves_sample_without_claiming_live(self):
        blocked = threading.Event()
        release = threading.Event()
        calls = 0
        board = fake_board()
        normal = board.read_resultant_force

        def read_resultant():
            nonlocal calls
            calls += 1
            if calls == 2:
                blocked.set()
                if not release.wait(2):
                    raise TimeoutError("test reader was not released")
            return normal()

        board.read_resultant_force = read_resultant

        @contextmanager
        def factory(*_):
            yield board

        with patch("openarm_tactile.source.open_board", factory):
            reader = TactileReader("fake").start()
            try:
                self.assertTrue(blocked.wait(2))
                initial = reader.snapshot()
                self.assertEqual(initial["sequence"], 1)
                self.assertEqual(initial["state"], "live")
                clock = SimpleNamespace(monotonic=lambda: reader._received + 0.6)
                with patch("openarm_tactile.source.time", clock):
                    stale = reader.snapshot()
                self.assertEqual(stale["state"], "stale")
                self.assertIs(stale["sample"], initial["sample"])
                self.assertAlmostEqual(stale["age_s"], 0.6)
            finally:
                release.set()
                reader.close()

    def test_close_before_start_prevents_later_start(self):
        reader = TactileReader("fake")
        reader.close()
        self.assertEqual(reader.snapshot()["state"], "closed")
        self.assertIsNone(reader._thread)
        with self.assertRaises(RuntimeError):
            reader.start()

    def test_duplicate_start_and_restart_before_start_are_rejected(self):
        entered = threading.Event()
        release = threading.Event()

        @contextmanager
        def factory(*_):
            entered.set()
            release.wait(2)
            yield fake_board()

        reader = TactileReader("fake")
        with self.assertRaises(RuntimeError):
            reader.restart()
        with patch("openarm_tactile.source.open_board", factory):
            try:
                reader.start()
                self.assertTrue(entered.wait(2))
                with self.assertRaises(RuntimeError):
                    reader.start()
            finally:
                release.set()
                reader.close()


class TactileBoardAdapterTests(unittest.TestCase):
    def sdk_modules(self, *, opening_error=False):
        state = SimpleNamespace(closed=0, board=None)
        request, response = wire_pair()

        class Transport:
            def __init__(self, device, baudrate, **kwargs):
                self.device, self.baudrate, self._kwargs = device, baudrate, kwargs

            def fin_transaction(self, request, *, timeout):
                return response

        class Board:
            def __init__(self, device, **kwargs):
                state.board = self
                self.device, self.kwargs = device, kwargs

            def open(self):
                if opening_error:
                    raise OSError("cannot open")

            def close(self):
                state.closed += 1

        sdk = ModuleType("paxini_sdk")
        sdk.SingleSensorBoard = Board
        transport = ModuleType("paxini_sdk.transport")
        transport.SerialTransport = Transport
        return {"paxini_sdk": sdk, "paxini_sdk.transport": transport}, state, request

    def test_sets_explicit_device_id_exclusive_and_short_read_timeout(self):
        modules, state, request = self.sdk_modules()
        with patch.dict(sys.modules, modules):
            with open_board("fake", 921600, 2) as board:
                self.assertEqual(board.kwargs["device_id"], 2)
                self.assertEqual(board.kwargs["sensor"], "PXSR-STDIP03A")
                self.assertEqual(board.transport._kwargs["timeout"], 0.05)
                self.assertTrue(board.transport._kwargs["exclusive"])
                self.assertEqual(len(board.transport.fin_transaction(request)), 18)
                other = bytearray(request)
                other[4] = 1
                with self.assertRaisesRegex(ValueError, "match requested"):
                    board.transport.fin_transaction(bytes(other))
            self.assertEqual(state.closed, 1)

    def test_close_after_open_failure(self):
        modules, state, _ = self.sdk_modules(opening_error=True)
        with patch.dict(sys.modules, modules), self.assertRaisesRegex(OSError, "cannot open"):
            with open_board("fake", 921600, 2):
                self.fail("should not yield a board after open failure")
        self.assertEqual(state.closed, 1)
