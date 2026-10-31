"""Bounded, operator-started OpenArm v2 stop-based calibration over Classic CAN.

Sequence informed by Enactic's openarm-can-zero-position-calibration at
bfd4e52194aefe38389fa6e670be3738136facb9. This is an independent executor using
the existing runtime lifecycle, not the upstream FD controller. See docs/calibration.md.
"""

from collections import deque
from dataclasses import dataclass
import math
import time

from .arm import ArmHold, FOLLOWER_KP, FOLLOWER_KD
from .config import bus_lease
from .control import check_modes, _ignore, _running
from .telemetry import IDS
from .transport import ClassicBus


# Upstream interpolate defaults, also used for joints waiting between stages.
CALIBRATION_KP = (52.0,) * 8
CALIBRATION_KD = (1.5,) * 8


@dataclass(frozen=True)
class Stage:
    action: str
    mid: int
    degrees: float
    direction: int = 0
    travel: float = 0


def calibration_plan(side):
    """Motor IDs are 1..8 throughout; ID8 can never alias J1."""
    if side not in ("left", "right"):
        raise ValueError("Select left or right")
    mirror = 1 if side == "right" else -1
    return (
        Stage("seek", 8, 0, mirror, 110),
        Stage("seek", 4, 0, -1, 60),
        Stage("relative", 2, 5 * mirror),
        Stage("seek", 3, 90, 1, 120), Stage("move", 3, 0),
        Stage("restore", 2, 0), Stage("move", 4, 90),
        Stage("seek", 5, 90, 1, 120), Stage("move", 5, 0),
        Stage("seek", 6, 45, 1, 75), Stage("move", 6, 0),
        Stage("seek", 7, 90, 1, 120), Stage("move", 7, 0),
        Stage("seek", 2, -10 * mirror, -mirror, 40), Stage("move", 2, 0),
        Stage("move", 4, 135),
        Stage("seek", 1, -80 * mirror, -mirror, 110), Stage("move", 1, 0),
        Stage("move", 4, 90), Stage("move", 4, 0),
    )


def stage_gains(stage):
    """Exact gains for the corresponding upstream v2 action."""
    if stage.action == "seek":
        return (280.0, 2.1) if stage.mid == 1 else (45.0, 1.2)
    if stage.action == "move" and stage.mid == 2:
        return 280.0, 2.0
    return 52.0, 1.5


def initial_pose_consistent(side, mid, angle):
    """Check inferred physical range, not an arbitrary distance from zero."""
    limits = {1: (-80, 200) if side == "right" else (-200, 80),
              2: (-10, 190) if side == "right" else (-190, 10),
              3: (-90, 90), 4: (0, 140), 5: (-90, 90),
              6: (-45, 45), 7: (-90, 90)}
    lower, upper = limits[mid]
    return math.radians(lower)-Calibration.tolerance <= angle <= math.radians(upper)+Calibration.tolerance


def seek_limits(bus, mid):
    """Validate stop loading against measured firmware ranges before enabling."""
    threshold = 0.3 if mid == 8 else 5.0 if mid == 1 else 2.0
    torque_range = bus.ranges[mid].torque
    if not math.isfinite(torque_range) or threshold >= 0.7 * torque_range:
        raise RuntimeError(f"ID{mid}: stop threshold incompatible with firmware torque range")
    kp, _ = stage_gains(Stage("seek", mid, 0))
    lead = min(math.radians(15), 0.8 * torque_range / kp, 1.5 * threshold / kp)
    if kp * lead < 1.1 * threshold:
        raise RuntimeError(f"ID{mid}: insufficient bounded stop-seeking effort")
    return threshold, lead


class Calibration:
    """Single-threaded motion executor; creation is passive.

    All motion uses zero feedforward; proximal calibration gains ramp up. Seeking
    uses measured torque plus sustained tracking error/stall, never torque alone.
    A collision can resemble a stop: the operator must clear the entire sweep.
    """

    speed = math.radians(20)
    seek_speed = math.radians(10)
    tolerance = math.radians(0.5)
    period = 0.05
    trim_limit = math.radians(30)
    trim_speed = math.radians(1)
    move_acceleration = math.radians(40)
    hold_trim_speed = math.radians(3)

    def __init__(self, arm, side, record, progress, *, clock=None, sleep=None):
        self.arm, self.side = arm, side
        self.record, self.progress = record, progress
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.initial = {mid: s.position for mid, s in arm.states.items()}
        self.zeros = {}
        self.started = self.clock()
        self.last = self.started
        self.trace = deque(maxlen=200)
        self.stage = None
        self.kp, self.kd = FOLLOWER_KP, FOLLOWER_KD
        self.failed_feedback = None
        self.tracking_since = dict.fromkeys(IDS)
        self.hold_positions = dict(self.initial)

    def loading_limit(self, mid):
        """Position loading constrained by firmware effort, not a fixed 2.5°."""
        damping = abs(self.kd[mid-1]*self.arm.states[mid].velocity)
        return max(0, (0.7*self.arm.bus.ranges[mid].torque-damping)/self.kp[mid-1])

    def cycle(self, targets, active=None, seeking=False):
        self.arm.check_cancelled()
        now = self.clock()
        if now - self.started > 900 or now - self.last > 0.25:
            raise RuntimeError("Calibration control deadline missed")
        self.last = now
        targets = dict(targets)
        if self.stage != "final_zero":
            for mid in IDS:
                if mid == active:
                    continue
                state = self.arm.states[mid]
                error = self.hold_positions[mid]-state.position
                if abs(error) > self.tolerance:
                    step = max(-self.hold_trim_speed*self.period,
                               min(self.hold_trim_speed*self.period, error))
                    lead = self.loading_limit(mid)
                    if step > 0:
                        step = min(step, max(0, state.position+lead-targets[mid]))
                    else:
                        step = max(step, min(0, state.position-lead-targets[mid]))
                    corrected = targets[mid]+step
                    if abs(corrected-self.hold_positions[mid]) <= self.trim_limit:
                        targets[mid] = corrected
        for mid in IDS:
            state = self.arm.states[mid]
            self.arm.validate(state)
            q = targets[mid]
            if not math.isfinite(q) or abs(q) >= self.arm.bus.ranges[mid].position:
                raise ValueError(f"ID{mid}: calibration target outside firmware range")
            if abs(q - self.arm.targets[mid]) > self.speed * self.period + 1e-8:
                raise RuntimeError("Calibration target slew exceeded")
            effort = self.kp[mid-1] * (q-state.position) - self.kd[mid-1] * state.velocity
            if abs(effort) > 0.9 * self.arm.bus.ranges[mid].torque:
                raise RuntimeError(f"ID{mid}: calibration effort limit")
        states = self.arm.bus.exchange(targets, self.kp, self.kd)
        if set(states) != set(IDS):
            raise RuntimeError("Incomplete calibration feedback")
        for mid, state in states.items():
            self.failed_feedback = {"motor_id": mid, "target_rad": targets[mid],
                                    "position_rad": state.position,
                                    "velocity_rad_s": state.velocity, "torque_nm": state.torque}
            if state.motor_id != mid:
                raise RuntimeError("Calibration feedback ID mismatch")
            self.arm.validate(state)
            if abs(state.velocity) > 0.7:
                raise RuntimeError(f"ID{mid}: calibration velocity limit")
            if abs(state.position-self.arm.states[mid].position) > math.radians(5):
                raise RuntimeError(f"ID{mid}: calibration feedback discontinuity")
            if abs(state.torque) > 0.9 * self.arm.bus.ranges[mid].torque:
                raise RuntimeError(f"ID{mid}: measured calibration torque limit")
            reference = self.hold_positions[mid]
            if mid != active and abs(reference-state.position) > math.radians(3):
                if self.tracking_since[mid] is None:
                    self.tracking_since[mid] = self.clock()
                if self.clock()-self.tracking_since[mid] >= 1.0:
                    raise RuntimeError(
                        f"J{mid} 补偿后保持偏差仍超过3°：目标 {math.degrees(reference):+.2f}°，"
                        f"实际 {math.degrees(state.position):+.2f}°，"
                        f"偏差 {math.degrees(reference-state.position):+.2f}°")
            else:
                self.tracking_since[mid] = None
        self.failed_feedback = None
        self.arm.states, self.arm.targets = states, dict(targets)
        if active is not None:
            state = states[active]
            self.trace.append({"elapsed_s": self.clock()-self.started, "motor_id": active,
                               "target_rad": targets[active], "position_rad": state.position,
                               "velocity_rad_s": state.velocity, "torque_nm": state.torque})
        self.arm.publish(states)
        self.sleep(max(0, self.period-(self.clock()-now)))

    def move_raw(self, mid, goal):
        start = self.clock()
        timeout = abs(goal-self.arm.targets[mid])/self.speed + 20
        settled = 0
        trim = 0.0
        arrived = None
        velocity = 0.0
        while self.clock()-start < timeout:
            targets = dict(self.arm.targets)
            # First ramp to the nominal position. Only then slowly compensate
            # static load error, without changing the validated MIT gains.
            if arrived is not None and self.clock()-arrived >= 1:
                state = self.arm.states[mid]
                error = goal-state.position
                if abs(error) > self.tolerance and abs(state.velocity) < 0.08:
                    increment = max(-self.trim_speed*self.period,
                                    min(self.trim_speed*self.period, error))
                    limit = min(self.trim_limit, self.loading_limit(mid))
                    trim = max(-limit, min(limit, trim+increment))
            distance = goal+trim-targets[mid]
            velocity = min(self.speed, velocity+self.move_acceleration*self.period)
            step = max(-velocity*self.period, min(velocity*self.period, distance))
            current = self.arm.states[mid].position
            lead = self.loading_limit(mid)
            if step > 0:
                step = min(step, max(0, current+lead-targets[mid]))
            elif step < 0:
                step = max(step, min(0, current-lead-targets[mid]))
            targets[mid] += step
            self.cycle(targets, mid)
            state = self.arm.states[mid]
            self.trace[-1].update(goal_rad=goal, trim_rad=trim)
            if arrived is None and abs(targets[mid]-goal) < 1e-8:
                arrived = self.clock()
            settled = settled+1 if (abs(goal-state.position) <= self.tolerance
                                      and abs(state.velocity) < 0.08) else 0
            if settled >= 6 and arrived is not None:
                self.hold_positions[mid] = goal
                return
        state = self.arm.states[mid]
        raise TimeoutError(
            f"J{mid} 未到位：目标 {math.degrees(goal):+.2f}°，"
            f"实际 {math.degrees(state.position):+.2f}°，"
            f"误差 {math.degrees(goal-state.position):+.2f}°，"
            f"速度 {state.velocity:+.3f} rad/s")

    def seek(self, stage):
        mid, direction = stage.mid, stage.direction
        threshold, lead = seek_limits(self.arm.bus, mid)
        start = self.clock()
        q_start = self.arm.states[mid].position
        travel = math.radians(stage.travel)
        count, contacts = 0, []
        while self.clock()-start < travel/self.seek_speed+10:
            targets = dict(self.arm.targets)
            q = self.arm.states[mid].position
            # Never wind up a target far beyond the measured joint position.
            advance = min(self.seek_speed*self.period, max(0, lead-direction*(targets[mid]-q)))
            targets[mid] += direction*advance
            if max(abs(q-q_start), abs(targets[mid]-q_start)) > travel:
                raise RuntimeError(f"ID{mid}: stop not found within travel bound")
            self.cycle(targets, mid, seeking=True)
            state = self.arm.states[mid]
            loaded = direction*(targets[mid]-state.position) > 0.6*threshold/self.kp[mid-1]
            stalled = abs(state.velocity) < 0.08 and abs(state.torque) >= threshold and loaded
            count = count+1 if stalled else 0
            contacts = (contacts+[state.position])[-6:] if stalled else []
            if count >= 6 and max(contacts)-min(contacts) <= math.radians(0.2):
                hit = sum(contacts)/len(contacts)
                zero = hit-math.radians(stage.degrees)
                initial_model = self.initial[mid]-zero
                if mid != 8 and not initial_pose_consistent(self.side, mid, initial_model):
                    raise RuntimeError(
                        f"J{mid}: inferred initial angle {math.degrees(initial_model):+.2f}° "
                        "outside mechanical range")
                if mid == 8:
                    opening = (-1 if self.side == "right" else 1)*initial_model
                    if not -self.tolerance <= opening <= math.radians(100):
                        raise RuntimeError("Gripper stop inconsistent with closing direction")
                self.zeros[mid] = zero
                self.record({"event": "calibration_stop", "motor_id": mid,
                             "raw_stop_rad": hit, "model_stop_deg": stage.degrees,
                             "initial_model_deg": math.degrees(initial_model),
                             "raw_zero_rad": zero, "torque_nm": state.torque})
                # Unload the target gradually before moving another joint.
                self.release(mid, hit)
                self.hold_positions[mid] = hit
                return
        raise TimeoutError(f"ID{mid}: stop-seeking timeout")

    def release(self, mid, hit):
        # During spring unloading the tracking gap is intentionally larger than
        # normal movement tolerance. Keep all other joint guards active.
        start = self.clock()
        while abs(self.arm.targets[mid]-hit) > 1e-8:
            if self.clock()-start > 8:
                raise TimeoutError("Stop unloading timeout")
            targets = dict(self.arm.targets)
            gap = hit-targets[mid]
            targets[mid] += max(-self.speed*self.period, min(self.speed*self.period, gap))
            self.cycle(targets, mid, seeking=True)

    def settle_zeros(self):
        """Converge all eight measured zeros together after the last pose change."""
        self.stage = "final_zero"
        self.trace.clear()
        self.progress("最后一步 · 整臂零位微调")
        self.hold_positions = dict(self.zeros)
        trim = {mid: max(-self.trim_limit, min(self.trim_limit,
                    self.arm.targets[mid]-self.zeros[mid])) for mid in IDS}
        start = self.clock()
        stable = 0
        settle_tolerance = self.tolerance
        while self.clock()-start < 25:
            targets = dict(self.arm.targets)
            for mid in IDS:
                state = self.arm.states[mid]
                error = self.zeros[mid]-state.position
                if abs(error) > settle_tolerance and abs(state.velocity) < 0.08:
                    delta = max(-self.trim_speed*self.period, min(self.trim_speed*self.period, error))
                    limit = min(self.trim_limit, self.loading_limit(mid))
                    trim[mid] = max(-limit, min(limit, trim[mid]+delta))
                gap = self.zeros[mid]+trim[mid]-targets[mid]
                targets[mid] += max(-self.trim_speed*self.period, min(self.trim_speed*self.period, gap))
            self.cycle(targets)
            errors = {mid: self.zeros[mid]-self.arm.states[mid].position for mid in IDS}
            self.trace.append({"elapsed_s": self.clock()-self.started,
                               "zero_error_rad": errors, "trim_rad": dict(trim),
                               "positions_rad": {mid: s.position for mid, s in self.arm.states.items()}})
            good = all(abs(errors[mid]) <= settle_tolerance
                       and abs(self.arm.states[mid].velocity) < 0.08 for mid in IDS)
            stable = stable+1 if good else 0
            if stable >= 6:
                self.record({"event": "calibration_zero_settled", "zero_error_rad": errors,
                             "trim_rad": dict(trim), "stable_frames": stable})
                return
        detail = "，".join(f"ID{mid} {math.degrees(error):+.2f}°"
                           for mid, error in errors.items() if abs(error) > settle_tolerance)
        raise TimeoutError(f"整臂零位未稳定：{detail or '仍有运动'}")

    def run(self):
        plan = calibration_plan(self.side)
        self.progress("正在建立标定保持…")
        for step in range(1, 21):
            scale = step/20
            self.kp = tuple(a+(b-a)*scale for a, b in zip(FOLLOWER_KP, CALIBRATION_KP))
            self.kd = tuple(a+(b-a)*scale for a, b in zip(FOLLOWER_KD, CALIBRATION_KD))
            self.cycle(dict(self.arm.targets))
        self.record({"event": "calibration_start", "side": self.side,
                     "initial_raw_rad": self.initial, "speed_deg_s": math.degrees(self.speed),
                     "seek_speed_deg_s": math.degrees(self.seek_speed),
                     "kp": self.kp, "kd": self.kd})
        for index, stage in enumerate(plan, 1):
            self.stage = index
            self.trace.clear()
            self.arm.check_cancelled()
            joint = f"J{stage.mid}" if stage.mid < 8 else "夹爪"
            action = {"seek": "寻找止挡", "move": "移动", "relative": "避让", "restore": "恢复位置"}[stage.action]
            label = f"{index}/{len(plan)} · {joint} {action}"
            self.progress(label)
            self.record({"event": "calibration_stage", "stage": index, "label": label})
            kp, kd = stage_gains(stage)
            old_kp, old_kd = self.kp, self.kd
            # Only the active joint changes gains. Other joints retain their
            # last command gains, matching upstream staged commands.
            for step in range(1, 11):
                scale = step/10
                self.kp = tuple(a+(kp-a)*scale if mid == stage.mid else a
                                for mid, a in enumerate(old_kp, 1))
                self.kd = tuple(a+(kd-a)*scale if mid == stage.mid else a
                                for mid, a in enumerate(old_kd, 1))
                self.cycle(dict(self.arm.targets))
            self.record({"event": "calibration_gains", "stage": index,
                         "motor_id": stage.mid, "kp": kp, "kd": kd})
            if stage.action == "seek":
                self.seek(stage)
            elif stage.action == "move":
                self.move_raw(stage.mid, self.zeros[stage.mid]+math.radians(stage.degrees))
            elif stage.action == "relative":
                self.move_raw(stage.mid, self.arm.states[stage.mid].position+math.radians(stage.degrees))
            elif stage.action == "restore":
                self.move_raw(stage.mid, self.initial[stage.mid])
        if set(self.zeros) != set(IDS):
            raise RuntimeError("Incomplete zero solution")
        self.settle_zeros()
        self.record({"event": "calibration_solution", "raw_zero_rad": dict(self.zeros)})
        return dict(self.zeros)


def write_zeros(bus, zeros, cancelled, record, publish):
    """Only after disabled-pose verification; record each non-atomic write."""
    if set(zeros) != set(IDS) or not all(math.isfinite(v) for v in zeros.values()):
        raise ValueError("Eight finite measured zero positions required")
    written = []

    def check(mid, expected):
        if cancelled():
            raise InterruptedError("Calibration stopped before zero write")
        state = bus.state(mid)
        if (state.motor_id != mid or state.status != 0 or state.frame_format != "classic"
                or not state.fresh(time.monotonic()) or not math.isfinite(state.position)
                or not math.isfinite(state.velocity) or abs(state.velocity) > 0.08
                or abs(state.position-expected) > math.radians(0.5)):
            raise RuntimeError(f"ID{mid}: disabled zero pose moved or feedback invalid")
        return state

    try:
        for mid in IDS:
            check(mid, zeros[mid])
        for mid in IDS:
            check(mid, zeros[mid])
            record({"event": "zero_write_attempt", "motor_id": mid})
            if cancelled():
                raise InterruptedError("Calibration stopped before zero write")
            bus.set_zero(mid, zeros[mid])
            written.append(mid)
            state = check(mid, 0.0)
            record({"event": "zero_write_verified", "motor_id": mid, "position_rad": state.position})
        states = {mid: check(mid, 0.0) for mid in IDS}
        publish(states)
        record({"event": "calibration_complete", "zeroed_ids": written,
                "power_cycle_verified": False})
    except BaseException:
        record({"event": "calibration_zero_incomplete", "sent_ids": written,
                "note": "Inspect attempt events; a failed send may have reached the motor. No automatic rollback."})
        raise


def calibrate_arm(profile, side, *, cancelled=_running, record=_ignore,
                  publish=_ignore, progress=_ignore, allow_unreadable_mode=False):
    """Enable, seek stops, reach computed zero, disable, write/verify zeros.

    This call moves real hardware. The caller must stop its reader first and
    ensure the v2 stock arm starts approximately at the official reference pose.
    No communication baud, mode or persistence/flash parameters are written.
    """
    if not profile.mapping_verified:
        raise ValueError("Confirm physical left/right mapping before calibration")
    calibration_plan(side)
    interface = profile.interface(side)
    if cancelled():
        raise InterruptedError("Calibration cancelled")
    with bus_lease(interface, exclusive=True):
        bus = ClassicBus(interface, profile.feedback_timeout_s)
        try:
            record({"event": "preflight", "motors": bus.preflight()})
            record({"event": "modes", "readback": check_modes(bus, allow_unreadable_mode, cancelled)})
            for mid in IDS:
                seek_limits(bus, mid)
            executor = None
            try:
                with ArmHold(bus, cancelled, record, publish) as arm:
                    executor = Calibration(arm, side, record, progress)
                    zeros = executor.run()
            except BaseException:
                # Flush diagnostic history only AFTER the context attempts disable.
                # No disk writes are added to the time-sensitive control cycles.
                if executor is not None:
                    try:
                        record({"event": "calibration_failure_trace", "stage": executor.stage,
                                "failed_feedback": executor.failed_feedback,
                                "samples": list(executor.trace)})
                    except Exception:
                        pass  # Preserve the motion/disable error if report storage fails.
                raise
            progress("已失能，检查姿态并写入零位…")
            write_zeros(bus, zeros, cancelled, record, publish)
            return zeros
        finally:
            bus.close()
