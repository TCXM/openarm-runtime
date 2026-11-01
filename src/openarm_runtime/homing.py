"""Synchronized, bounded return of J1..J7 to calibrated motor zero.

No transport or firmware zero writes. Each CAN worker keeps servicing its own
bus while waiting for its peer, and the gripper's current command is preserved.
"""
import math
import threading


class HomeCoordinator:
    speed_deg_s = 5.0
    tolerance_deg = 5.0
    settle_s = .5
    settle_timeout_s = 8.0

    def __init__(self, sides):
        self.sides = tuple(sides)
        self.lock = threading.RLock()
        self.phase = "following"
        self.epoch = 0
        self.origins = {}
        self.targets = {}
        self.since = {}
        self.started = None
        self.duration = 0
        self.progress = 0
        self.feedback = {}
        self.updated_at = {}

    def request(self):
        with self.lock:
            if self.phase not in ("following", "holding"):
                raise RuntimeError("Return to zero already in progress")
            self.epoch += 1
            self.phase = "collecting"
            self.origins, self.targets, self.since = {}, {}, {}
            self.started = None
            self.progress = 0
            self.feedback, self.updated_at = {}, {}

    def step(self, side, commanded, states, now):
        with self.lock:
            if side not in self.sides:
                raise RuntimeError("No home trajectory for this arm")
            if self.phase == "following":
                # Resume may arrive after a worker chose its home branch. Finish
                # this one hold exchange; switch to following on its next tick.
                return dict(commanded)
            self.feedback[side], self.updated_at[side] = states, now
            if self.phase == "collecting":
                self.origins.setdefault(side, dict(commanded))
                self.targets[side] = {mid: (commanded[8] if mid == 8 else 0.) for mid in range(1, 9)}
                if len(self.origins) == len(self.sides):
                    distance = max(abs(q[mid]) for q in self.origins.values() for mid in range(1, 8))
                    # Cubic smoothstep has peak normalized derivative 1.5.
                    self.duration = max(1., 1.5 * distance / math.radians(self.speed_deg_s))
                    self.started = now
                    self.phase = "moving"
                return dict(commanded)
            u = min(1., max(0., (now - self.started) / self.duration))
            self.progress = u
            blend = u * u * (3 - 2 * u)
            targets = {mid: self.origins[side][mid] * (1 - blend) if mid < 8 else self.targets[side][8]
                       for mid in range(1, 9)}
            if u == 1. and self.phase != "holding":
                self.phase = "settling"
                good = all(abs(states[mid].position) <= math.radians(self.tolerance_deg)
                           and abs(states[mid].velocity) <= .1 for mid in range(1, 8))
                if good:
                    self.since.setdefault(side, now)
                else:
                    self.since.pop(side, None)
                if len(self.since) == len(self.sides) and all(now - t >= self.settle_s for t in self.since.values()):
                    self.phase = "holding"
                elif now - self.started > self.duration + self.settle_timeout_s:
                    raise RuntimeError("回零未收敛：请检查姿态、负载及跟踪误差")
            return targets

    def settled(self, now):
        with self.lock:
            return self.phase == "holding" and all(
                side in self.feedback and 0 <= now - self.updated_at[side] <= .25
                and all(abs(self.feedback[side][mid].position) <= math.radians(self.tolerance_deg)
                        and abs(self.feedback[side][mid].velocity) <= .1 for mid in range(1, 8))
                for side in self.sides)


def alignment_errors(mapping, samples, targets):
    """Signed leader-to-home errors, degrees; gripper included without moving it."""
    errors = {}
    for side, sample in samples.items():
        angles, opening = mapping.angles(side, sample.positions)
        if opening is None:
            raise ValueError("Gripper calibration required")
        leader = [*angles, math.radians(opening * (65 if side == "left" else -65))]
        errors[side] = [math.degrees(q - targets[side][mid]) for mid, q in enumerate(leader, 1)]
    return errors
