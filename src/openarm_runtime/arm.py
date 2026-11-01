"""Persistent whole-arm MIT hold, matching the reference follower gain layout."""

from dataclasses import asdict
import math
import time

from .telemetry import IDS
from .config import finite

# openarm_follower/config_openarm_follower.py: J1..J7, gripper.
# J1–J7: Enactic openarm_description OpenArm v2 control_gains.yaml.
# J8 is not specified upstream; retain the existing gripper gains.
FOLLOWER_KP = (70.0, 70.0, 70.0, 60.0, 10.0, 10.0, 10.0, 25.0)
FOLLOWER_KD = (2.75, 2.5, 2.0, 2.0, 0.7, 0.6, 0.5, 0.3)


class ArmHold:
    def __init__(self, bus, cancelled, record, publish, *, tracking=False, gravity=None, observe=None):
        self.bus, self.cancelled, self.record, self.publish = bus, cancelled, record, publish
        self.tracking = tracking
        self.gravity = gravity
        self.observe = observe
        self.attempted = []
        self.states = {}
        self.base = {}
        self.targets = {}
        self.cycles = 0

    def validate(self, state, enabled=True):
        if not state.fresh(time.monotonic(), 0.4) or state.frame_format != "classic":
            raise RuntimeError(f"ID{state.motor_id}: stale or non-Classic feedback")
        if state.status != int(enabled):
            raise RuntimeError(f"ID{state.motor_id}: status={state.status}, expected={int(enabled)}")
        if any(not math.isfinite(x) for x in (state.position, state.velocity, state.torque)):
            raise RuntimeError("Non-finite feedback")
        if max(state.mos_temperature, state.rotor_temperature) >= 75:
            raise RuntimeError(f"ID{state.motor_id}: temperature limit")
        if not self.tracking and abs(state.velocity) > 2:
            raise RuntimeError(f"ID{state.motor_id}: velocity limit")

    def check_cancelled(self):
        if self.cancelled():
            raise InterruptedError("User stop / browser disconnected")

    def __enter__(self):
        try:
            self.check_cancelled()
            self.bus.prepare_encoder(IDS)
            initial = {}
            for mid in IDS:
                self.check_cancelled()
                state = self.bus.state(mid)
                self.validate(state, enabled=False)
                initial[mid] = state
                self.bus.command(mid, "position", state.position, 0, 0)
            if self.gravity is not None:
                self.gravity.preflight(initial, self.bus.ranges)
                self.record({"event": "gravity_config", **self.gravity.metadata()})
            for mid in IDS:
                self.check_cancelled()
                self.attempted.append(mid)
                self.bus.command(mid, "enable")
                time.sleep(0.02)
            for mid in IDS:
                end = time.monotonic() + 0.3
                while True:
                    self.check_cancelled()
                    state = self.bus.state(mid)
                    if state.status != 0 or time.monotonic() >= end:
                        self.validate(state)
                        break
                    time.sleep(0.02)
                self.states[mid] = state
            self.base = {mid: state.position for mid, state in self.states.items()}
            self.targets = dict(self.base)
            self.record({"event": "arm_enabled", "base_rad": self.base,
                         "kp": FOLLOWER_KP, "kd": FOLLOWER_KD,
                         "sdk_version": self.bus.sdk.__version__})
            # Ramp all gains together while holding freshly read current angles.
            for step in range(1, 11):
                self.tick({mid: 0 for mid in IDS}, 0.5, 0.05, gain_scale=step / 10)
                time.sleep(0.05)
            return self
        except BaseException as exc:
            failures = self.disable()
            if failures:
                raise RuntimeError(f"{exc}; disable NOT confirmed: {failures}") from exc
            raise

    def __exit__(self, exc_type, exc, tb):
        failures = self.disable()
        if failures:
            raise RuntimeError(f"Disable NOT confirmed: {failures}; original={exc}") from exc

    def disable(self):
        errors = []
        # Attempt every disable before checking any reply, even after partial enable.
        for mid in self.attempted:
            try:
                self.bus.command(mid, "disable")
            except Exception as exc:
                errors.append(f"ID{mid} send: {exc}")
        for mid in self.attempted:
            try:
                end = time.monotonic() + 0.3
                while True:
                    state = self.bus.state(mid)
                    if state.status != 1 or time.monotonic() >= end:
                        if state.status != 0:
                            raise RuntimeError(f"status={state.status}")
                        break
                    time.sleep(0.02)
            except Exception as exc:
                errors.append(f"ID{mid} readback: {exc}")
        if self.attempted:
            self.record({"event": "arm_disable", "ids": list(self.attempted),
                         "verified": not errors, "errors": errors})
        self.attempted.clear()
        return errors

    def tick(self, offsets, speed, period, gain_scale=1):
        self.check_cancelled()
        finite(speed, 0.1, 5, "speed_deg_s")
        finite(period, 0.02, 0.1, "period_s")
        finite(gain_scale, 0, 1, "gain_scale")
        if set(offsets) != set(IDS):
            raise ValueError("All eight joint targets required")
        kp = [gain_scale * x for x in FOLLOWER_KP]
        kd = [gain_scale * x for x in FOLLOWER_KD]
        step = math.radians(speed) * period
        targets = {}
        for mid in IDS:
            self.validate(self.states[mid])
            goal = self.base[mid] + math.radians(finite(offsets[mid], -30, 30, "offset_deg"))
            if abs(goal) >= self.bus.ranges[mid].position:
                raise ValueError(f"ID{mid}: target outside firmware range")
            q = self.targets[mid] + max(-step, min(step, goal - self.targets[mid]))
            state = self.states[mid]
            effort = kp[mid - 1] * (q - state.position) - kd[mid - 1] * state.velocity
            if abs(effort) > 0.9 * self.bus.ranges[mid].torque:
                raise RuntimeError(f"ID{mid}: predicted effort near firmware range; error={math.degrees(q-state.position):.2f}°")
            targets[mid] = q
        self._exchange(targets, kp, kd)

    def track(self, targets):
        """Direct MIT position references; caller clips to robot joint limits.

        Unlike the manual-slider tick, this has no software slew/offset cap or
        inferred-effort rejection. The motor applies its MIT effort saturation.
        """
        if not self.tracking:
            raise RuntimeError("Direct targets require a tracking session")
        self.check_cancelled()
        if set(targets) != set(IDS):
            raise ValueError("All eight joint targets required")
        for mid, goal in targets.items():
            self.validate(self.states[mid])
            if not math.isfinite(goal) or abs(goal) >= self.bus.ranges[mid].position:
                raise ValueError(f"ID{mid}: target outside firmware range")
        self._exchange(dict(targets), FOLLOWER_KP, FOLLOWER_KD)

    def _exchange(self, targets, kp, kd):
        before = self.states
        feedforward = {mid: 0.0 for mid in IDS}
        if self.gravity is None:
            started = time.monotonic()
            states = self.bus.exchange(targets, kp, kd)
        else:
            feedforward = self.gravity.update(self.states)
            for mid in IDS:
                state = self.states[mid]
                effort = kp[mid-1] * (targets[mid] - state.position) - kd[mid-1] * state.velocity + feedforward[mid]
                if abs(effort) > .9 * self.bus.ranges[mid].torque:
                    raise RuntimeError(f"ID{mid}: combined PD/gravity effort near firmware range")
            started = time.monotonic()
            states = self.bus.exchange(targets, kp, kd, feedforward)
        if set(states) != set(IDS):
            raise RuntimeError("Incomplete arm feedback")
        for mid, state in states.items():
            if state.motor_id != mid:
                raise RuntimeError("Feedback ID mismatch")
            self.validate(state)
        self.states, self.targets = states, targets
        if self.observe is not None:
            self.observe({"dispatch_started_at": started, "exchange_completed_at": time.monotonic(),
                          "targets_rad": dict(targets), "kp": tuple(kp), "kd": tuple(kd),
                          "feedforward_nm": feedforward,
                          "observation_before": {mid: asdict(s) for mid, s in before.items()},
                          "feedback": {mid: asdict(s) for mid, s in states.items()},
                          "gravity": self.gravity.last if self.gravity else None})
        self.publish(states)
        self.cycles += 1
        if self.gravity is not None:
            self.record({"event": "gravity_sample", "time": time.monotonic(),
                         "targets_rad": dict(targets), "gravity": self.gravity.last,
                         "states": {mid: asdict(s) for mid, s in states.items()}})
        if self.cycles % 20 == 0:
            self.record({"event": "hold_sample", "targets_rad": targets,
                         "states": {mid: asdict(s) for mid, s in states.items()}})
