"""External policy plugin: oscillate joint1 around the initial pose.

Use the simulated backend first. This demonstrates the contract, not a trained
manipulation policy, and imports no device/control implementation.
"""
import math

from openarm_policy import ActionChunk


class SinePolicy:
    def __init__(self, amplitude_deg=3.0, period_s=4.0):
        self.amplitude = math.radians(float(amplitude_deg))
        self.period = float(period_s)
        if not math.isfinite(self.amplitude) or not 0 < self.amplitude <= math.radians(5):
            raise ValueError("Demo amplitude must be in (0, 5] degrees")
        if not math.isfinite(self.period) or self.period < 2:
            raise ValueError("Demo period must be at least 2 seconds")

    def reset(self, context):
        self.context = context
        self.base = None
        self.origin = None

    def infer(self, observation):
        if self.base is None:
            self.base = []
            for name in self.context.action.names:
                side, joint = name.split(".")
                index = 7 if joint == "gripper_motor" else int(joint[5:]) - 1
                self.base.append(observation.streams[f"robot/{side}"][-1].data[index][0])
            self.origin = observation.captured_at
            for sign in (-1, 1):
                extrema = tuple(q + sign * self.amplitude if name.endswith(".joint1") else q
                                for name, q in zip(self.context.action.names, self.base))
                self.context.action.validate(extrema)
        steps = []
        for i in range(20):
            elapsed = observation.captured_at - self.origin + i * .05
            offset = self.amplitude * math.sin(2 * math.pi * elapsed / self.period)
            steps.append(tuple(q + offset if name.endswith(".joint1") else q
                               for name, q in zip(self.context.action.names, self.base)))
        return ActionChunk(observation.sequence, observation.captured_at, .05,
                           tuple(steps), self.context.action.names)

    def close(self):
        pass
