"""Conservative online trajectories and software supervision, no hardware I/O.

Rated torque: https://docs.openarm.dev/hardware/openarm-2.0/motor/
DM8009P J1/J2: 20 Nm; DM4340 J3/J4: 9 Nm; DM4310 J5..J8: 3 Nm.
These software checks are not a drive-level current/torque clamp. Initial
5 deg/s and 10 deg/s² limits are commissioning settings, not rated performance.
"""
import math
from .config import finite
from .telemetry import IDS
from .limits import MAX_MANUAL_SPEED_DEG_S

RATED_TORQUE_NM = (20., 20., 9., 9., 3., 3., 3., 3.)
MAX_SPEED_DEG_S = MAX_MANUAL_SPEED_DEG_S
ACCELERATION_DEG_S2 = 10.0


class SmoothMotion:
    def __init__(self, bounds_deg, *, relative_gripper=False):
        if set(bounds_deg) != set(IDS):
            raise ValueError('Eight joint bounds required')
        self.bounds = {mid: tuple(math.radians(v) for v in pair)
                       for mid, pair in bounds_deg.items()}
        self.relative_gripper = relative_gripper
        self.positions = {}
        self.velocities = dict.fromkeys(IDS, 0.0)
        self.stall_since = dict.fromkeys(IDS)

    def metadata(self):
        return {"speed_cap_deg_s": MAX_SPEED_DEG_S,
                "acceleration_deg_s2": ACCELERATION_DEG_S2,
                "rated_torque_nm": RATED_TORQUE_NM,
                "tracking_error_deg": 5, "stall_error_deg": 2,
                "stall_speed_deg_s": .5, "stall_duration_s": 1,
                "bounds_rad": self.bounds}

    def validate_position(self, mid, position):
        if self.relative_gripper and mid == 8 and not self.positions:
            finite(position, -12.5, 12.5, 'gripper_position')
            return
        lower, upper = self.bounds[mid]
        finite(position, lower-math.radians(.5), upper+math.radians(.5), f'J{mid} initial angle')

    def initialize(self, positions):
        for mid in IDS:
            self.validate_position(mid, positions[mid])
        if self.relative_gripper:
            self.bounds[8] = (positions[8]-math.radians(30), positions[8]+math.radians(30))
        # Retain the freshly captured pose within encoder/zero tolerance without
        # a step to a clipped bound at takeover. New goals use original limits.
        self.travel_bounds = {mid: (min(lo, positions[mid]), max(hi, positions[mid]))
                              for mid, (lo, hi) in self.bounds.items()}
        self.positions = dict(positions)
        self.last_goals = dict(positions)
        self.velocities = dict.fromkeys(IDS, 0.0)

    def step(self, goals, speed_deg_s, dt):
        finite(speed_deg_s, .1, MAX_SPEED_DEG_S, 'speed_deg_s')
        finite(dt, .02, .1, 'period_s')
        if set(goals) != set(IDS) or not self.positions:
            raise ValueError('Initialized eight-joint trajectory required')
        vmax = math.radians(speed_deg_s)
        acceleration = math.radians(ACCELERATION_DEG_S2)
        positions, velocities = {}, {}
        for mid in IDS:
            q, v = self.positions[mid], self.velocities[mid]
            lo, hi = self.travel_bounds[mid]
            goal = finite(goals[mid], lo, hi, f'J{mid} target')
            distance = goal-q
            # Discrete braking envelope leaves one acceleration step of margin.
            braking = max(0., math.sqrt((acceleration*dt)**2 + 2*acceleration*abs(distance)) - acceleration*dt)
            wanted = math.copysign(min(vmax, braking), distance)
            next_v = v + max(-acceleration*dt, min(acceleration*dt, wanted-v))
            next_q = q + next_v*dt
            # A sudden goal reversal may temporarily pass that goal while braking;
            # physical joint boundaries must never be crossed.
            if not lo-1e-10 <= next_q <= hi+1e-10:
                raise RuntimeError(f'J{mid}: trajectory cannot brake within joint bounds')
            positions[mid], velocities[mid] = next_q, next_v
        self.last_goals = dict(goals)
        self.positions, self.velocities = positions, velocities
        return dict(positions)

    def supervise(self, states, targets, ranges, kp, kd, now, *, after=False, feedforward=None):
        if set(states) != set(IDS):
            raise RuntimeError('Incomplete protected motion feedback')
        for mid in IDS:
            state = states[mid]
            self.validate_position(mid, state.position)
            limit = min(RATED_TORQUE_NM[mid-1], .9*ranges[mid].torque)
            error = targets[mid]-state.position
            predicted = kp[mid-1]*error-kd[mid-1]*state.velocity + (feedforward or {}).get(mid, 0.0)
            if abs(state.torque) > limit:
                raise RuntimeError(f'J{mid}: measured torque {state.torque:.2f} Nm exceeds {limit:g} Nm')
            if abs(predicted) > limit:
                raise RuntimeError(f'J{mid}: predicted torque {predicted:.2f} Nm exceeds {limit:g} Nm')
            if abs(state.velocity) > 2:
                raise RuntimeError(f'J{mid}: feedback velocity limit')
            if abs(error) > math.radians(5):
                raise RuntimeError(
                    f'J{mid}: tracking error exceeds 5 degrees; '
                    f'target={math.degrees(targets[mid]):.2f} deg, '
                    f'measured={math.degrees(state.position):.2f} deg, '
                    f'error={math.degrees(error):+.2f} deg, '
                    f'torque={state.torque:.2f} Nm, kp={kp[mid-1]:g}, kd={kd[mid-1]:g}')
            if after:
                stalled = abs(error) > math.radians(2) and abs(state.velocity) < math.radians(.5)
                if not stalled:
                    self.stall_since[mid] = None
                elif self.stall_since[mid] is None:
                    self.stall_since[mid] = now
                elif now-self.stall_since[mid] >= 1.0:
                    raise RuntimeError(f'J{mid}: stalled / sustained tracking error for 1 second')
