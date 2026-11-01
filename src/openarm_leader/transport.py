"""Feetech SDK read-only boundary; never opens a follower CAN connection."""

import threading
import time
from dataclasses import dataclass


def validate_read_packet(packet):
    data = bytes(packet)
    if (len(data) != 8 or data[:2] != b"\xff\xff" or data[2] not in range(1, 9)
            or data[3:5] != b"\x04\x02" or tuple(data[5:7]) not in ((3, 2), (40, 24))
            or sum(data[2:]) & 255 != 255):
        raise ValueError("Leader transport permits only model and feedback register reads")


class LeaderBus:
    def __init__(self, path):
        import scservo_sdk as sdk

        class ReadPort(sdk.PortHandler):
            def writePort(self, packet):
                validate_read_packet(packet)
                return super().writePort(packet)

            def getCurrentTime(self):
                return time.monotonic() * 1000

        self.port = ReadPort(path)
        self.handler = sdk.PacketHandler(0)
        self.errors = {}
        try:
            if not self.port.setBaudRate(1_000_000):
                raise RuntimeError("Cannot open leader at 1 Mbps")
            self.port.ser.exclusive = True
            self.port.ser.write_timeout = 0.1
            for mid in range(1, 9):
                model = int.from_bytes(self.read(mid, 3, 2), "little")
                if model != 777:
                    raise RuntimeError(f"ID{mid}: expected STS3215 (777), received {model}")
        except BaseException:
            self.close()
            raise

    def read(self, mid, address, length):
        self.port.ser.reset_input_buffer()
        data, result, error = self.handler.readTxRx(self.port, mid, address, length)
        # A voltage-only alarm does not invalidate a complete, checksummed read
        # for display or operator-approved following. Other faults/transport failures reject.
        if result != 0 or error & ~1 or len(data) != length:
            detail = self.handler.getRxPacketError(error) if error else ""
            voltage = f", {data[22] / 10:.1f} V" if address == 40 and len(data) >= 23 else ""
            raise OSError(f"ID{mid}: communication={result}, servo_error={error} {detail}{voltage}")
        self.errors[mid] = error
        return bytes(data)

    def sample(self):
        positions, torque, voltages = [], [], []
        started = time.monotonic()
        for mid in range(1, 9):
            data = self.read(mid, 40, 24)
            torque.append(data[0])
            voltages.append(data[22] / 10)
            raw = int.from_bytes(data[16:18], "little")
            positions.append(-(raw & 0x7fff) if raw & 0x8000 else raw)
        return Sample(tuple(positions), tuple(torque), started, time.monotonic() - started,
                      tuple(voltages), tuple(self.errors.get(mid, 0) for mid in range(1, 9)))

    def close(self):
        if self.port.is_open:
            self.port.closePort()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


@dataclass(frozen=True)
class Sample:
    positions: tuple
    torque: tuple
    received_at: float
    duration: float
    voltages: tuple = ()
    errors: tuple = ()

    def fresh(self, now):
        return 0 <= now - self.received_at < 0.4


class Reader:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.sample = None
        self.error = "等待连接"
        self.count = 0
        self.on_sample = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.close()

    def snapshot(self):
        with self.lock:
            return self.sample, self.error, self.count

    def run(self):
        while not self.stop.is_set():
            bus = None
            try:
                bus = LeaderBus(self.path)
                while not self.stop.is_set():
                    started = time.monotonic()
                    sample = bus.sample()
                    with self.lock:
                        self.sample, self.error = sample, ""
                        self.count += 1
                    if self.on_sample is not None:
                        self.on_sample(sample, self.count)
                    self.stop.wait(max(0, 1 / 30 - (time.monotonic() - started)))
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
            finally:
                if bus is not None:
                    bus.close()
            self.stop.wait(1)

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                raise RuntimeError(f"Leader reader did not stop: {self.path}")
