"""Software-only leader alignment, independent of serial and GUI dependencies."""

import json
import math
import os
from pathlib import Path

# Indexed by virtual follower J1..J7; signs follow the reference OpenArmMini.
SOURCE_IDS = (1, 2, 3, 4, 5, 7, 6)
SIGNS = {"left": (-1, 1, -1, -1, -1, -1, -1),
         "right": (-1, -1, -1, -1, -1, -1, 1)}


def delta(raw, origin):
    return (raw - origin + 2048) % 4096 - 2048


class Mapping:
    def __init__(self, left, right, path):
        if Path(left).resolve() == Path(right).resolve():
            raise ValueError("Leaders must use distinct serial devices")
        self.path = Path(path)
        self.data = {
            "version": 1, "ports": {"left": left, "right": right},
            "signs": {side: list(signs) for side, signs in SIGNS.items()},
            "devices": {port: {"zero": None, "open": None, "closed": None}
                        for port in (left, right)},
        }
        if self.path.exists():
            data = json.loads(self.path.read_text())
            if data.get("version") != 1 or set(data["ports"]) != {"left", "right"}:
                raise ValueError("Unsupported leader calibration")
            if set(data["ports"].values()) != {left, right} or set(data["devices"]) != {left, right}:
                raise ValueError("Calibration device identities differ from supplied ports")
            for side in SIGNS:
                signs = data["signs"][side]
                if len(signs) != 7 or any(s not in (-1, 1) for s in signs):
                    raise ValueError("Invalid joint signs")
            for device in data["devices"].values():
                zero = device["zero"]
                if zero is not None and (len(zero) != 7 or any(type(x) is not int for x in zero)):
                    raise ValueError("Invalid encoder origins")
                for key in ("open", "closed"):
                    if device[key] is not None and type(device[key]) is not int:
                        raise ValueError("Invalid gripper endpoint")
                if (device["open"] is not None and device["closed"] is not None
                        and abs(delta(device["open"], device["closed"])) < 20):
                    raise ValueError("Invalid gripper calibration span")
            self.data = data

    def device(self, side):
        return self.data["devices"][self.data["ports"][side]]

    def capture(self, side, sample, kind, now):
        if sample is None or not sample.fresh(now):
            raise ValueError("数据过期，无法标定")
        if any(sample.torque):
            raise ValueError("主臂舵机已使能，暂不采集手动标定")
        device = self.device(side)
        if kind == "zero":
            device["zero"] = list(sample.positions[:7])
        elif kind in ("open", "closed"):
            other = device["closed" if kind == "open" else "open"]
            raw = sample.positions[7]
            if other is not None and abs(delta(raw, other)) < 20:
                raise ValueError("夹爪两个端点过近，请明显张开／闭合后再采集")
            device[kind] = raw
        else:
            raise ValueError("Unknown calibration kind")

    def angles(self, side, positions):
        device = self.device(side)
        zero = device["zero"] or [2048] * 7
        angles = [sign * delta(positions[mid - 1], zero[mid - 1]) * math.tau / 4096
                  for mid, sign in zip(SOURCE_IDS, self.data["signs"][side])]
        opening = None
        if device["open"] is not None and device["closed"] is not None:
            span = delta(device["open"], device["closed"])
            if abs(span) < 20:
                raise ValueError("Invalid gripper calibration span")
            opening = min(1.0, max(0.0, delta(positions[7], device["closed"]) / span))
        return angles, opening

    def swap(self):
        ports = self.data["ports"]
        ports["left"], ports["right"] = ports["right"], ports["left"]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w") as handle:
            json.dump(self.data, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
