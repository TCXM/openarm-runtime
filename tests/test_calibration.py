"""Virtual stop mechanics exercise offset recovery and failure cleanup offline."""

from contextlib import nullcontext
from dataclasses import replace
import math
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openarm_runtime.arm import ArmHold
from openarm_runtime.calibration import Calibration, Stage, stage_gains, initial_pose_consistent, calibrate_arm, calibration_plan, write_zeros
from openarm_runtime.config import Profile
from openarm_runtime.telemetry import IDS, MotorRange, MotorState
from openarm_runtime.transport import ClassicBus


class Clock:
    now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class MechanicalBus:
    def __init__(self, side, clock):
        self.clock = clock
        self.sdk = SimpleNamespace(__version__="1.4.1")
        self.ranges = {mid: MotorRange(12.5, 30, 28 if mid < 5 else 3) for mid in IDS}
        self.zeros = {mid: 0.1 * mid for mid in IDS}
        self.positions = dict(self.zeros)
        self.positions[4] += math.radians(10)
        self.positions[8] += math.radians(-30 if side == "right" else 30)
        self.stops = {s.mid: (s.direction, self.zeros[s.mid] + math.radians(s.degrees))
                      for s in calibration_plan(side) if s.action == "seek"}
        self.enabled, self.written, self.commands = set(), [], []
        self.torques = dict.fromkeys(IDS, 0)
        self.fault = None
        self.fail_write = None
        self.closed = False

    def prepare_encoder(self, ids):
        pass

    def preflight(self):
        return {}

    def parameter(self, *args):
        return 1

    def close(self):
        self.closed = True

    def command(self, mid, action, *args):
        self.commands.append((mid, action))
        if action == "enable":
            self.enabled.add(mid)
        elif action == "disable":
            self.enabled.discard(mid)

    def state(self, mid):
        return MotorState(mid, self.positions[mid], 0, self.torques[mid], 25, 25,
                          int(mid in self.enabled), self.clock.monotonic())

    def exchange(self, targets, kp, kd):
        for mid, goal in targets.items():
            direction, stop = self.stops[mid]
            q = min(goal, stop) if direction > 0 else max(goal, stop)
            self.positions[mid] = q
            self.torques[mid] = kp[mid-1] * (goal-q)
        states = {mid: self.state(mid) for mid in IDS}
        return self.fault(states) if self.fault else states

    def set_zero(self, mid, expected):
        assert not self.enabled
        assert abs(self.positions[mid]-expected) < math.radians(0.5)
        if mid == self.fail_write:
            raise OSError("write failed")
        self.written.append(mid)
        self.positions[mid] = 0


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.time = patch("time.monotonic", self.clock.monotonic)
        self.sleep = patch("time.sleep", self.clock.sleep)
        self.time.start()
        self.sleep.start()
        self.addCleanup(self.time.stop)
        self.addCleanup(self.sleep.stop)
        self.events = []

    def run_api(self, bus, cancelled=lambda: False):
        with patch("openarm_runtime.calibration.bus_lease", return_value=nullcontext()), \
             patch("openarm_runtime.calibration.ClassicBus", return_value=bus):
            return calibrate_arm(Profile("can1", "can0", mapping_verified=True), "right",
                                 record=self.events.append, cancelled=cancelled)

    def test_both_sides_recover_nonzero_offsets_and_write_only_after_disable(self):
        for side in ("left", "right"):
            with self.subTest(side=side):
                bus = MechanicalBus(side, self.clock)
                with ArmHold(bus, lambda: False, self.events.append, lambda _: None) as arm:
                    zeros = Calibration(arm, side, self.events.append, lambda _: None).run()
                for mid in IDS:
                    self.assertAlmostEqual(zeros[mid], bus.zeros[mid])
                    self.assertAlmostEqual(bus.positions[mid], bus.zeros[mid])
                write_zeros(bus, zeros, lambda: False, self.events.append, lambda _: None)
                self.assertEqual(bus.written, list(IDS))
                self.assertFalse(bus.enabled)
                self.assertLess(self.clock.now, 1000)

    def test_unsuccessful_seek_is_bounded_and_disables(self):
        bus = MechanicalBus("right", self.clock)
        bus.stops[8] = (1, 10)
        with self.assertRaisesRegex(RuntimeError, "travel bound"):
            self.run_api(bus)
        self.assertFalse(bus.enabled)
        self.assertFalse(bus.written)
        self.assertTrue(bus.closed)
        self.assertLess(self.clock.now, 150)

    def test_cancel_during_motion_disables_and_never_writes(self):
        bus = MechanicalBus("right", self.clock)
        with self.assertRaises(InterruptedError):
            self.run_api(bus, lambda: self.clock.now > 103)
        self.assertFalse(bus.enabled)
        self.assertFalse(bus.written)
        self.assertTrue(bus.closed)

    def test_invalid_torque_range_fails_before_enable(self):
        bus = MechanicalBus("right", self.clock)
        bus.ranges[5] = MotorRange(12.5, 30, 2)
        with self.assertRaisesRegex(RuntimeError, "threshold"):
            self.run_api(bus)
        self.assertFalse(bus.commands)
        self.assertTrue(bus.closed)

    def test_missing_stale_discontinuous_feedback_disables(self):
        faults = [lambda s: {k: v for k, v in s.items() if k != 3},
                  lambda s: {**s, 3: replace(s[3], received_at=0)},
                  lambda s: {**s, 3: replace(s[3], position=s[3].position+1)}]
        for fault in faults:
            bus = MechanicalBus("right", self.clock)
            with self.assertRaises(RuntimeError):
                with ArmHold(bus, lambda: False, self.events.append, lambda _: None) as arm:
                    bus.fault = fault
                    Calibration(arm, "right", self.events.append, lambda _: None).run()
            self.assertFalse(bus.enabled)
            self.assertFalse(bus.written)

    def test_drift_prevents_all_writes(self):
        bus = MechanicalBus("right", self.clock)
        bus.positions = dict(bus.zeros)
        bus.positions[7] += 0.02
        with self.assertRaisesRegex(RuntimeError, "pose moved"):
            write_zeros(bus, bus.zeros, lambda: False, self.events.append, lambda _: None)
        self.assertFalse(bus.written)

    def test_partial_writes_are_recorded_and_never_rolled_back(self):
        bus = MechanicalBus("right", self.clock)
        bus.positions = dict(bus.zeros)
        bus.fail_write = 4
        with self.assertRaises(OSError):
            write_zeros(bus, bus.zeros, lambda: False, self.events.append, lambda _: None)
        self.assertEqual(bus.written, [1, 2, 3])
        self.assertEqual(self.events[-1]["sent_ids"], [1, 2, 3])
        self.assertEqual(self.events[-2]["motor_id"], 4)

    def test_public_lifecycle_succeeds_and_closes(self):
        bus = MechanicalBus("right", self.clock)
        zeros = self.run_api(bus)
        for mid in IDS:
            self.assertAlmostEqual(zeros[mid], bus.zeros[mid])
        self.assertEqual(bus.written, list(IDS))
        self.assertTrue(bus.closed)
        self.assertFalse(bus.enabled)

    def test_elbow_load_uses_ramped_proximal_gains(self):
        bus = MechanicalBus("right", self.clock)
        original = bus.exchange
        gains = []
        def loaded(targets, kp, kd):
            gains.append(kp[0])
            states = original(targets, kp, kd)
            # A synthetic 1.7 Nm load would exceed 3 degrees at ordinary Kp30.
            if targets[4] > bus.zeros[4] + math.radians(30):
                states[1] = replace(states[1], position=states[1].position-1.7/kp[0])
            return states
        bus.exchange = loaded
        self.run_api(bus)
        self.assertEqual(max(gains), 280)
        self.assertTrue(all(abs(b-a) <= 22.8+1e-8 for a, b in zip(gains[10:], gains[11:])))

    def test_gains_match_upstream_actions(self):
        self.assertEqual(stage_gains(Stage("seek", 1, -80)), (280, 2.1))
        for mid in range(2, 9):
            self.assertEqual(stage_gains(Stage("seek", mid, 0)), (45, 1.2))
        self.assertEqual(stage_gains(Stage("move", 2, 0)), (280, 2))
        self.assertEqual(stage_gains(Stage("relative", 2, 5)), (52, 1.5))
        self.assertEqual(stage_gains(Stage("restore", 2, 0)), (52, 1.5))
        for mid in (1, 3, 4, 5, 6, 7):
            self.assertEqual(stage_gains(Stage("move", mid, 0)), (52, 1.5))

    def test_restart_with_j3_away_from_zero_still_solves_offsets(self):
        bus = MechanicalBus("right", self.clock)
        bus.positions[3] = bus.zeros[3]+math.radians(59)
        zeros = self.run_api(bus)
        self.assertAlmostEqual(zeros[3], bus.zeros[3])
        self.assertEqual(bus.written, list(IDS))

    def test_initial_consistency_uses_mirrored_physical_ranges(self):
        self.assertTrue(initial_pose_consistent("right", 3, math.radians(59)))
        self.assertFalse(initial_pose_consistent("right", 3, math.radians(95)))
        self.assertTrue(initial_pose_consistent("right", 1, math.radians(150)))
        self.assertFalse(initial_pose_consistent("left", 1, math.radians(150)))
        self.assertFalse(initial_pose_consistent("right", 4, math.radians(-5)))

    def test_inactive_joint_failure_records_offending_feedback(self):
        bus = MechanicalBus("right", self.clock)
        original = bus.exchange
        def failing(targets, kp, kd):
            states = original(targets, kp, kd)
            if targets[4] > bus.zeros[4]+math.radians(30):
                states[1] = replace(states[1], position=bus.zeros[1]-math.radians(4))
            return states
        bus.exchange = failing
        with self.assertRaisesRegex(RuntimeError, "J1 补偿后保持偏差"):
            self.run_api(bus)
        self.assertFalse(bus.enabled)
        trace = self.events[-1]
        self.assertEqual(trace["stage"], 7)
        self.assertEqual(trace["failed_feedback"]["motor_id"], 1)
        self.assertFalse(bus.written)

    def test_inactive_joint_compensates_changing_elbow_load(self):
        bus = MechanicalBus("right", self.clock)
        with ArmHold(bus, lambda: False, self.events.append, lambda _: None) as arm:
            executor = Calibration(arm, "right", self.events.append, lambda _: None)
            original = bus.exchange
            def loaded(targets, kp, kd):
                states = original(targets, kp, kd)
                states[1] = replace(states[1], position=targets[1]-math.radians(3.15))
                return states
            bus.exchange = loaded
            for _ in range(40):
                executor.cycle(dict(arm.targets), active=4)
            self.assertLessEqual(abs(arm.states[1].position-executor.hold_positions[1]), executor.tolerance)
            self.assertGreater(arm.targets[1]-executor.hold_positions[1], math.radians(2.5))

    def test_final_zero_reconverges_multiple_shifted_joints_together(self):
        bus = MechanicalBus("right", self.clock)
        with ArmHold(bus, lambda: False, self.events.append, lambda _: None) as arm:
            executor = Calibration(arm, "right", self.events.append, lambda _: None)
            executor.zeros = dict(bus.zeros)
            arm.targets = dict(bus.zeros)
            arm.states = {mid: replace(s, position=bus.zeros[mid]) for mid, s in arm.states.items()}
            original = bus.exchange
            def loaded(targets, kp, kd):
                states = original(targets, kp, kd)
                for mid, lag in ((1, 1.2), (6, -0.9)):
                    states[mid] = replace(states[mid], position=targets[mid]-math.radians(lag))
                return states
            bus.exchange = loaded
            executor.settle_zeros()
            self.assertGreater(self.clock.now-executor.started, 0.5)
            for mid in IDS:
                self.assertLessEqual(abs(arm.states[mid].position-bus.zeros[mid]), executor.tolerance)
            self.assertEqual(self.events[-1]["event"], "calibration_zero_settled")
            self.assertEqual(self.events[-1]["stable_frames"], 6)

    def test_brief_tracking_transient_does_not_abort(self):
        bus = MechanicalBus("right", self.clock)
        original = bus.exchange
        count = 0
        def transient(targets, kp, kd):
            nonlocal count
            states = original(targets, kp, kd)
            if targets[4] > bus.zeros[4]+math.radians(30):
                count += 1
                if count <= 3:
                    states[1] = replace(states[1], position=targets[1]-math.radians(3.2))
            return states
        bus.exchange = transient
        self.run_api(bus)
        self.assertEqual(bus.written, list(IDS))

    def test_final_zero_timeout_disables_without_writes(self):
        bus = MechanicalBus("right", self.clock)
        with self.assertRaisesRegex(TimeoutError, "整臂零位未稳定"):
            with ArmHold(bus, lambda: False, self.events.append, lambda _: None) as arm:
                executor = Calibration(arm, "right", self.events.append, lambda _: None)
                executor.zeros = dict(bus.zeros)
                arm.targets = dict(bus.zeros)
                arm.states = {mid: replace(s, position=bus.zeros[mid]) for mid, s in arm.states.items()}
                original = bus.exchange
                def unstable(targets, kp, kd):
                    states = original(targets, kp, kd)
                    states[1] = replace(states[1], velocity=0.1)
                    return states
                bus.exchange = unstable
                executor.settle_zeros()
        self.assertFalse(bus.enabled)
        self.assertFalse(bus.written)

    def test_unsettled_move_reports_tracking_history_after_disable(self):
        bus = MechanicalBus("right", self.clock)
        original = bus.exchange
        def exchange(targets, kp, kd):
            states = original(targets, kp, kd)
            if targets[2] > bus.zeros[2] + math.radians(1):
                states[2] = replace(states[2], position=targets[2]-math.radians(1))
            return states
        bus.exchange = exchange
        with patch.object(Calibration, "trim_limit", 0), self.assertRaisesRegex(TimeoutError, "J2 未到位.*误差"):
            self.run_api(bus)
        self.assertFalse(bus.enabled)
        self.assertFalse(bus.written)
        trace = self.events[-1]
        self.assertEqual(trace["event"], "calibration_failure_trace")
        self.assertEqual(self.events[-2]["event"], "arm_disable")
        self.assertEqual(trace["stage"], 3)
        self.assertLessEqual(len(trace["samples"]), 200)
        last = trace["samples"][-1]
        self.assertAlmostEqual(last["target_rad"]-last["position_rad"], math.radians(1))

    def test_bounded_trim_reaches_actual_goal_under_static_load(self):
        for direction in (-1, 1):
            bus = MechanicalBus("right", self.clock)
            with ArmHold(bus, lambda: False, self.events.append, lambda _: None) as arm:
                original = bus.exchange
                def loaded(targets, kp, kd):
                    states = original(targets, kp, kd)
                    states[2] = replace(states[2], position=targets[2]-direction*math.radians(2.11))
                    return states
                bus.exchange = loaded
                executor = Calibration(arm, "right", self.events.append, lambda _: None)
                goal = arm.states[2].position+direction*math.radians(5)
                executor.move_raw(2, goal)
                self.assertLessEqual(abs(arm.states[2].position-goal), executor.tolerance)
                self.assertGreater(abs(arm.targets[2]-goal), math.radians(1.5))
                self.assertLessEqual(abs(arm.targets[2]-goal), executor.trim_limit)
                samples = list(executor.trace)
                for a, b in zip(samples, samples[1:]):
                    self.assertLessEqual(abs(b["trim_rad"]-a["trim_rad"]),
                                         executor.trim_speed*executor.period+1e-10)
            self.assertFalse(bus.enabled)
            self.assertFalse(bus.written)

    def test_trim_does_not_push_through_a_blocked_joint(self):
        bus = MechanicalBus("right", self.clock)
        with self.assertRaisesRegex(TimeoutError, "未到位"):
            with ArmHold(bus, lambda: False, self.events.append, lambda _: None) as arm:
                original = bus.exchange
                fixed = arm.states[2].position
                def blocked(targets, kp, kd):
                    states = original(targets, kp, kd)
                    states[2] = replace(states[2], position=fixed)
                    return states
                bus.exchange = blocked
                executor = Calibration(arm, "right", self.events.append, lambda _: None)
                executor.move_raw(2, fixed+math.radians(2))
        self.assertFalse(bus.enabled)
        self.assertFalse(bus.written)

    def test_move_waits_for_breakaway_without_running_target_ahead(self):
        bus = MechanicalBus("right", self.clock)
        with ArmHold(bus, lambda: False, self.events.append, lambda _: None) as arm:
            executor = Calibration(arm, "right", self.events.append, lambda _: None)
            original = bus.exchange
            position = arm.states[3].position
            start = self.clock.now
            gaps = []
            def sluggish(targets, kp, kd):
                nonlocal position
                states = original(targets, kp, kd)
                gaps.append(abs(targets[3]-position))
                if self.clock.now-start >= 0.4:
                    delta = targets[3]-position
                    position += max(-math.radians(15)*executor.period,
                                    min(math.radians(15)*executor.period, delta))
                states[3] = replace(states[3], position=position)
                return states
            bus.exchange = sluggish
            goal = position-math.radians(10)
            executor.move_raw(3, goal)
            self.assertLessEqual(max(gaps), executor.loading_limit(3)+1e-10)
            self.assertLessEqual(abs(arm.states[3].position-goal), executor.tolerance)

    def test_j4_load_above_old_angle_cap_moves_and_holds_at_real_goal(self):
        bus = MechanicalBus("right", self.clock)
        with ArmHold(bus, lambda: False, self.events.append, lambda _: None) as arm:
            executor = Calibration(arm, "right", self.events.append, lambda _: None)
            executor.kp = (52.0,)*8
            executor.kd = (1.5,)*8
            original = bus.exchange
            position = arm.states[4].position
            def loaded(targets, kp, kd):
                nonlocal position
                states = original(targets, kp, kd)
                desired = targets[4]-math.radians(6)
                position += max(0, min(math.radians(10)*executor.period, desired-position))
                bus.positions[4] = position
                states[4] = replace(states[4], position=position,
                                    torque=kp[3]*(targets[4]-position))
                return states
            bus.exchange = loaded
            goal = position+math.radians(30)
            executor.move_raw(4, goal)
            self.assertLessEqual(abs(arm.states[4].position-goal), executor.tolerance)
            self.assertGreater(arm.targets[4]-goal, math.radians(3))
            for _ in range(10):
                executor.cycle(dict(arm.targets))
            self.assertEqual(executor.hold_positions[4], goal)

    def test_cancellation_between_writes_preserves_attempt_evidence(self):
        bus = MechanicalBus("right", self.clock)
        bus.positions = dict(bus.zeros)
        with self.assertRaises(InterruptedError):
            write_zeros(bus, bus.zeros, lambda: len(bus.written) == 2,
                        self.events.append, lambda _: None)
        self.assertEqual(bus.written, [1, 2])
        self.assertEqual(self.events[-1]["sent_ids"], [1, 2])

    def test_control_deadline_aborts_with_disable(self):
        bus = MechanicalBus("right", self.clock)
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            with ArmHold(bus, lambda: False, self.events.append, lambda _: None) as arm:
                executor = Calibration(arm, "right", self.events.append, lambda _: None)
                self.clock.sleep(0.3)
                executor.run()
        self.assertFalse(bus.enabled)

    def test_zero_transport_rejects_bad_state_before_sending(self):
        mechanical = MechanicalBus("right", self.clock)
        good = mechanical.state(1)
        bus = ClassicBus.__new__(ClassicBus)
        bus.motors = {1: object()}
        bus.send = Mock()
        packet = SimpleNamespace(send_can_id=1, data=b"\xff" * 7 + b"\xfe")
        encoder = Mock()
        encoder.create_set_zero_command.return_value = packet
        bus.sdk = SimpleNamespace(CanPacketEncoder=encoder)
        for state in (replace(good, status=1), replace(good, velocity=0.1),
                      replace(good, position=1), replace(good, position=float("nan")),
                      replace(good, received_at=0), replace(good, frame_format="fd")):
            bus.state = Mock(return_value=state)
            with self.assertRaises(RuntimeError):
                bus.set_zero(1, good.position)
        bus.send.assert_not_called()
        encoder.create_set_zero_command.assert_not_called()
        bus.state = Mock(return_value=good)
        bus.set_zero(1, good.position)
        encoder.create_set_zero_command.assert_called_once_with(bus.motors[1])
        bus.send.assert_called_once_with(1, packet.data)


if __name__ == "__main__":
    unittest.main()
