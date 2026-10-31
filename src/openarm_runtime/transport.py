"""Bounded Classic SocketCAN transport; official SDK supplies command encoding.

Unlike OpenArm.init_arm_motors this preflight never writes CTRL_MODE. Cached SDK
positions are not used: every state sample must arrive during a bounded request.
"""

import math
import socket
import struct
import sys
import time
from dataclasses import asdict

from .telemetry import IDS, MotorRange, decode_state, inspect_link, read_frame
from .config import require_classic_link

MODELS = ("DM8009", "DM8009", "DM4340", "DM4340", "DM4310", "DM4310", "DM4310", "DM4310")


class ClassicBus:
    def __init__(self, interface: str, timeout: float = 0.04):
        if sys.platform != "linux":
            raise RuntimeError("Linux SocketCAN required")
        self.interface, self.timeout = interface, timeout
        require_classic_link(inspect_link(interface))
        self.socket = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            self.socket.setsockopt(socket.SOL_CAN_RAW, 1, b"".join(
                struct.pack("=II", i + 16, 0xC00007FF) for i in IDS))
            self.socket.setsockopt(socket.SOL_CAN_RAW, 2, struct.pack("=I", 0x1FFFFFFF))
            self.socket.bind((interface,))
            self.socket.settimeout(timeout)
        except BaseException:
            self.socket.close()
            raise
        self.ranges = {}
        self.sdk = None
        self.motors = {}

    def close(self):
        self.socket.close()

    def send(self, can_id, data):
        if len(data) != 8 or can_id not in (*IDS, 0x7FF):
            raise ValueError("Invalid Classic motor packet")
        frame = struct.pack("=IB3x8s", can_id, 8, bytes(data))
        self.socket.settimeout(self.timeout)
        if self.socket.send(frame) != 16:
            raise OSError("Short CAN write")
        self.last_send = time.monotonic()

    def _decode_frame(self, frame):
        if len(frame) != 16:
            raise RuntimeError("Non-Classic frame")
        can_id, size = struct.unpack_from("=IB", frame)
        data = frame[8:8 + size]
        if can_id & 0x20000000:
            if can_id == 0x20000004 and len(data) >= 2 and data[1] == 0x40:
                return None
            raise RuntimeError(f"CAN fault {can_id:#x}: {data.hex()}")
        return can_id, data

    def drain(self):
        self.socket.setblocking(False)
        try:
            for _ in range(256):
                try:
                    self._decode_frame(self.socket.recv(16))
                except BlockingIOError:
                    return
            raise RuntimeError("Receive queue never drained; another sender may be active")
        finally:
            self.socket.settimeout(self.timeout)

    def receive(self, predicate, timeout=None):
        end = time.monotonic() + (self.timeout if timeout is None else timeout)
        for _ in range(256):
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            self.socket.settimeout(remaining)
            try:
                result = self._decode_frame(self.socket.recv(16))
            except socket.timeout:
                break
            if result is not None:
                value = predicate(*result)
                if value is not None:
                    return value
        raise TimeoutError("No matching fresh CAN reply within deadline")

    def parameter(self, mid, rid):
        for attempt in range(2):
            # Parameter processing needs spacing even when a reply arrives quickly.
            time.sleep(0.02)
            try:
                return self._parameter_once(mid, rid)
            except TimeoutError:
                if attempt:
                    raise TimeoutError(f"ID{mid}: no reply for RID{rid}") from None

    def _parameter_once(self, mid, rid):
        self.drain()
        self.send(0x7FF, bytes((mid, 0, 0x33, rid, 0, 0, 0, 0)))

        def match(cid, data):
            if cid == mid + 16 and len(data) == 8 and data[:4] == bytes((mid, 0, 0x33, rid)):
                return struct.unpack("<f" if rid in (21, 22, 23) else "<I", data[4:])[0]
        return self.receive(match, 0.35)

    def state(self, mid):
        # Control commands also produce state replies. Let their reply arrive and
        # discard it before a new refresh, rather than accepting a prior status as
        # the result of the enable/disable or position command just sent.
        time.sleep(max(0, 0.02 - (time.monotonic() - getattr(self, "last_send", 0))))
        self.drain()
        self.send(0x7FF, read_frame(mid)[8:])
        return self.receive(lambda cid, data: decode_state(cid, data, mid, self.ranges[mid], time.monotonic()))

    def preflight(self):
        report = {}
        for mid in IDS:
            code = self.parameter(mid, 35)
            if code != 4:
                raise RuntimeError(f"ID{mid} can_br={code}; expected 4")
            self.ranges[mid] = MotorRange(*(self.parameter(mid, rid) for rid in (21, 22, 23)))
            state = self.state(mid)
            if state.status != 0:
                raise RuntimeError(f"ID{mid} must be disabled; status={state.status}")
            report[mid] = {"baud_code": code, "range": asdict(self.ranges[mid]), "state": asdict(state)}
        require_classic_link(inspect_link(self.interface))
        return report

    def exchange(self, targets, kp, kd):
        """Batch MIT controls followed by their eight state replies, as in follower."""
        if set(targets) != set(IDS):
            raise ValueError("Whole-arm exchange requires IDs 1..8")
        time.sleep(max(0, 0.02 - (time.monotonic() - getattr(self, "last_send", 0))))
        self.drain()
        for mid in IDS:
            self.command(mid, "position", targets[mid], kp[mid - 1], kd[mid - 1])
        states = {}
        def collect(cid, data):
            mid = cid - 16
            if mid not in IDS:
                return None
            state = decode_state(cid, data, mid, self.ranges[mid], time.monotonic())
            if state is not None:
                states[mid] = state
            return states if len(states) == len(IDS) else None
        try:
            return self.receive(collect)
        except TimeoutError:
            raise TimeoutError(f"Missing control feedback IDs {sorted(set(IDS) - set(states))}") from None

    def prepare_encoder(self, ids):
        import openarm_can as oa

        if oa.__version__ != "1.4.1":
            raise RuntimeError("Validated encoder requires SDK 1.4.1")
        for mid in ids:
            if mid not in IDS:
                raise ValueError("Motor ID must be 1..8")
            kind = getattr(oa.MotorType, MODELS[mid - 1])
            expected = oa.Motor.get_limit_param(kind)
            actual = self.ranges[mid]
            if not all(math.isclose(a, b, rel_tol=1e-5) for a, b in zip(
                (actual.position, actual.velocity),
                (expected.pMax, expected.vMax))):
                raise RuntimeError(f"ID{mid} firmware ranges differ from SDK; refusing incorrectly scaled commands")
            self.motors[mid] = oa.Motor(kind, mid, mid + 16)
            # Only zero feedforward is supported. Its 12-bit midpoint is independent
            # of TMAX; feedback and effort guards still use the firmware range.
            packet = oa.CanPacketEncoder.create_mit_control_command(
                self.motors[mid], oa.MITParam(0, 0, 0, 0, 0))
            self._require_zero_feedforward(packet.data)
        self.sdk = oa

    @staticmethod
    def _require_zero_feedforward(data):
        if len(data) != 8 or ((data[6] & 15) << 8 | data[7]) != 2047:
            raise RuntimeError("Expected SDK zero-feedforward midpoint; refusing command")

    def command(self, mid, action, position=0.0, kp=0.0, kd=0.0):
        encoder = self.sdk.CanPacketEncoder
        motor = self.motors[mid]
        if action == "position":
            if not math.isfinite(position) or abs(position) >= self.ranges[mid].position:
                raise ValueError("Target outside firmware position range")
            packet = encoder.create_mit_control_command(motor, self.sdk.MITParam(kp, kd, position, 0.0, 0.0))
            self._require_zero_feedforward(packet.data)
        elif action in ("enable", "disable"):
            packet = getattr(encoder, f"create_{action}_command")(motor)
        else:
            raise ValueError("Unknown motor command")
        self.send(packet.send_can_id, packet.data)
