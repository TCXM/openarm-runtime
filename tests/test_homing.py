"""Offline trajectory bounds and convergence checks."""
import math
from types import SimpleNamespace
import unittest

from openarm_runtime.homing import HomeCoordinator


def states(position=0, velocity=0):
    return {j: SimpleNamespace(position=position, velocity=velocity) for j in range(1, 9)}


class HomingTests(unittest.TestCase):
    def test_synchronized_bounded_trajectory_preserves_grippers(self):
        home = HomeCoordinator(("left", "right"))
        home.request()
        origins = {"left": dict.fromkeys(range(1, 9), math.radians(30)),
                   "right": dict.fromkeys(range(1, 9), math.radians(-60))}
        self.assertEqual(home.step("left", origins["left"], states(), 0), origins["left"])
        self.assertEqual(home.phase, "collecting")
        home.step("right", origins["right"], states(), 1)
        previous = origins
        for tick in range(1, 1801):
            now = 1 + tick * .01
            current = {s: home.step(s, previous[s], states(), now) for s in origins}
            for side in origins:
                self.assertEqual(current[side][8], origins[side][8])
                for j in range(1, 8):
                    self.assertLessEqual(abs(current[side][j] - previous[side][j]) / .01,
                                         math.radians(5) + 1e-9)
            previous = current
        self.assertAlmostEqual(previous["right"][1], 0)
        for side in origins:
            home.step(side, previous[side], states(), 19.6)
        self.assertTrue(home.settled(19.6))
        self.assertFalse(home.settled(20))
        home.step("right", previous["right"], states(velocity=.2), 19.7)
        self.assertFalse(home.settled(19.7))

    def test_nonconvergence_times_out(self):
        home = HomeCoordinator(("left",))
        home.request()
        q = dict.fromkeys(range(1, 9), 0.)
        home.step("left", q, states(.2), 0)
        with self.assertRaisesRegex(RuntimeError, "未收敛"):
            home.step("left", q, states(.2), 10)

    def test_resume_race_holds_last_command(self):
        home = HomeCoordinator(("left",))
        q = dict.fromkeys(range(1, 9), .1)
        self.assertEqual(home.step("left", q, states(), 1), q)
