"""Single-sensor transport and capture, independent of the optional viewer."""
from contextlib import contextmanager
from dataclasses import dataclass
import math
import threading
import time

SENSOR = "PXSR-STDIP03A"
POINT_COUNT = 68


@dataclass(frozen=True)
class Sample:
    resultant: tuple
    forces: tuple


def read_sample(board):
    """Return raw counts; resultant and distribution are separate transactions."""
    resultant = tuple(board.read_resultant_force())
    forces = tuple(p.as_tuple() for p in board.read_distributed_force())
    if len(forces) != POINT_COUNT:
        raise ValueError(f"Expected {POINT_COUNT} points, received {len(forces)}")
    for triple in (resultant, *forces):
        if (len(triple) != 3 or any(not math.isfinite(v) for v in triple)
                or not -128 <= triple[0] <= 127 or not -128 <= triple[1] <= 127
                or not 0 <= triple[2] <= 255):
            raise ValueError("Invalid XYZ raw counts in tactile frame")
    return Sample(resultant, forces)


def validate_reply(request, response):
    """Check wire length and correlation in addition to the SDK's checksum."""
    if len(response) < 15 or response[:2] != b"\xaa\x55":
        raise ValueError("Invalid tactile response header")
    if len(response) != int.from_bytes(response[2:4], "little") + 5:
        raise ValueError("Tactile response length mismatch")
    # ID, reserved byte, function, register address and requested data length.
    if response[4:13] != request[4:13]:
        raise ValueError("Tactile response does not match requested device/register")
    if len(response) != 15 + int.from_bytes(request[11:13], "little"):
        raise ValueError("Tactile payload length mismatch")
    return response


@contextmanager
def open_board(device, baudrate, device_id):
    """Use the external SDK, with bounded reads and exclusive Linux ownership."""
    from paxini_sdk import SingleSensorBoard
    from paxini_sdk.transport import SerialTransport

    class CheckedTransport(SerialTransport):
        def fin_transaction(self, request, *, timeout=1.0):
            response = super().fin_transaction(request, timeout=timeout)
            return validate_reply(request, response)

    board = SingleSensorBoard(device, baudrate=baudrate, sensor=SENSOR,
                              device_id=device_id, response_timeout=0.5)
    board.transport = CheckedTransport(device, baudrate, timeout=0.05)
    # SDK does not expose pyserial's exclusive option. Set it before opening;
    # keep this adapter isolated from core imports and the external SDK tree.
    board.transport._kwargs["exclusive"] = True
    try:
        board.open()
        yield board
    finally:
        board.close()


def simulation_sample(coords, elapsed):
    """A synthetic moving contact centered on the actual sensor coordinates."""
    cx = sum(p[0] for p in coords) / len(coords) + 6 * math.sin(elapsed)
    cy = sum(p[1] for p in coords) / len(coords) + 6 * math.cos(elapsed)
    points = tuple((8 * math.sin(elapsed + i / 12), 5 * math.cos(elapsed),
                    80 * math.exp(-((x - cx) ** 2 + (y - cy) ** 2) / 40))
                   for i, (x, y, z) in enumerate(coords))
    return Sample((8 * math.sin(elapsed), 5 * math.cos(elapsed),
                   40 + 20 * math.sin(elapsed / 2)), points)


class TactileReader:
    """One capture worker; failures require explicit restart, never simulation."""

    def __init__(self, device=None, *, simulate=False, baudrate=921600, device_id=1):
        if simulate == bool(device):
            raise ValueError("Choose exactly one of simulation or a serial device")
        if type(device_id) is not int or not 1 <= device_id <= 255:
            raise ValueError("device_id must be 1..255")
        if type(baudrate) is not int or baudrate <= 0:
            raise ValueError("baudrate must be a positive integer")
        self.device, self.simulate = device, simulate
        self.baudrate, self.device_id = baudrate, device_id
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread = None
        self._state, self._error = "waiting", None
        self._sample, self._received = None, None
        self._sequence, self._rate = 0, 0.0
        self.on_sample = None

    def snapshot(self):
        with self._lock:
            age = None if self._received is None else time.monotonic() - self._received
            state = self._state
            if state == "live" and age is not None and age > 0.5:
                state = "stale"
            return dict(state=state, error=self._error, sample=self._sample,
                        sequence=self._sequence, received_at=self._received,
                        age_s=age, rate_hz=self._rate)

    def start(self):
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError("Reader has already started or closed")
        self._thread = threading.Thread(target=self._run, name="tactile-capture", daemon=True)
        self._thread.start()
        return self

    def restart(self):
        if self._thread is None or self._stop.is_set():
            raise RuntimeError("Reader is not running")
        with self._lock:
            self._state, self._error = "waiting", None
            self._restart.set()

    def _capture(self, read):
        started = time.monotonic()
        count = 0
        while not self._stop.is_set() and not self._restart.is_set():
            tick = time.monotonic()
            sample = read(tick - started)
            count += 1
            with self._lock:
                if self._restart.is_set() or self._stop.is_set():
                    return
                self._sample, self._received = sample, time.monotonic()
                self._sequence += 1
                self._rate = (count - 1) / max(0.001, self._received - started)
                self._state, self._error = "live", None
            if self.on_sample is not None:
                self.on_sample(sample, self._sequence, self._received)
            self._stop.wait(max(0, 1 / 60 - (time.monotonic() - tick)))

    def _run(self):
        while not self._stop.is_set():
            self._restart.clear()
            try:
                if self.simulate:
                    from paxini_sdk.sensor_registry import load_points
                    coords = load_points(SENSOR)
                    self._capture(lambda t: simulation_sample(coords, t))
                else:
                    with open_board(self.device, self.baudrate, self.device_id) as board:
                        self._capture(lambda _: read_sample(board))
            except Exception as exc:
                with self._lock:
                    self._state = "error"
                    self._error = f"{type(exc).__name__}: {exc}"
                    self._rate = 0.0
                while not self._stop.is_set() and not self._restart.wait(0.1):
                    pass
        with self._lock:
            self._state = "closed"

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                raise RuntimeError("Tactile capture did not stop within 3 seconds")
        with self._lock:
            self._state = "closed"

    __enter__ = start

    def __exit__(self, *_):
        self.close()
