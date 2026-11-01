"""Dependency-free plugin loading and a model-output chunk adapter."""
import importlib

from .contracts import ActionChunk


class ImportedPolicy:
    """Resolve module:factory in the inference process, never on the control thread.

    Factory receives JSON-compatible kwargs and returns a Policy. It can own a
    local model or a remote RPC client with its own serialization/timeouts.
    """
    def __init__(self, factory, kwargs=None):
        if ":" not in factory:
            raise ValueError("Policy factory must be module:function")
        self.factory, self.kwargs = factory, dict(kwargs or {})
        self.inner = None

    def reset(self, context):
        module, name = self.factory.split(":", 1)
        self.inner = getattr(importlib.import_module(module), name)(**self.kwargs)
        self.inner.reset(context)

    def infer(self, observation):
        return self.inner.infer(observation)

    def close(self):
        if self.inner is not None:
            self.inner.close()


class ChunkPolicyAdapter:
    """Model-specific transforms surround an arbitrary predict callable.

    load(context) returns the predictor; encode(observation, context) prepares
    model input; decode(output, context) returns H x A absolute joint radians.
    Dependencies/normalization/remote transport remain entirely in these hooks.
    """
    def __init__(self, load, encode, decode, period_s=0.05, lead_s=0.0):
        self.load, self.encode, self.decode = load, encode, decode
        self.period_s, self.lead_s = period_s, lead_s
        self.model = None

    def reset(self, context):
        self.context = context
        self.model = self.load(context)

    def infer(self, observation):
        output = self.model(self.encode(observation, self.context))
        steps = tuple(tuple(float(v) for v in row) for row in self.decode(output, self.context))
        return ActionChunk(observation.sequence, observation.captured_at + self.lead_s,
                           self.period_s, steps, self.context.action.names)

    def close(self):
        if self.model is not None and hasattr(self.model, "close"):
            self.model.close()


class HoldPolicy:
    """Smoke-test policy: predict the measured pose, never invent a motion target."""
    def reset(self, context):
        self.context = context

    def infer(self, observation):
        values = []
        for name in self.context.action.names:
            side, joint = name.split(".")
            index = 7 if joint == "gripper_motor" else int(joint[5:]) - 1
            values.append(observation.streams[f"robot/{side}"][-1].data[index][0])
        return ActionChunk(observation.sequence, observation.captured_at, 0.05,
                           (tuple(values),) * 20, self.context.action.names)

    def close(self):
        pass
