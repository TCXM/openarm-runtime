"""Bounded histories and adapters borrowing existing capture owners."""
from collections import deque
import copy
import time

from .contracts import Observation, SensorSample, StreamSpec, finite


class ObservationBuffer:
    def __init__(self, specs, history=8):
        self.specs = tuple(specs)
        if type(history) is not int or history < 1 or len({s.name for s in specs}) != len(specs):
            raise ValueError("History must be positive and stream names unique")
        self.buffers = {s.name: deque(maxlen=history) for s in specs}
        self.sequence = 0

    def collect(self, samples, now=None):
        now = time.monotonic() if now is None else now
        for spec in self.specs:
            sample = samples.get(spec.name)
            good = (sample is not None and sample.valid and not sample.error
                    and finite(sample.received_at) and 0 <= now - sample.received_at <= spec.max_age_s)
            if not good:
                if spec.required:
                    raise RuntimeError(f"Required stream unavailable/stale: {spec.name}")
                self.buffers[spec.name].clear()
                continue
            if type(sample.sequence) is not int or sample.sequence < 0:
                raise ValueError(f"Invalid source sequence: {spec.name}")
            buf = self.buffers[spec.name]
            if buf and (sample.sequence < buf[-1].sequence or
                        (sample.sequence != buf[-1].sequence and sample.received_at < buf[-1].received_at)):
                raise RuntimeError(f"Stream restarted or timestamp regressed: {spec.name}")
            if not buf or sample.sequence != buf[-1].sequence:
                shape = getattr(sample.data, "shape", None)
                if shape is None:
                    def matches(value, dimensions):
                        if not dimensions:
                            return finite(value)
                        return isinstance(value, (tuple, list)) and len(value) == dimensions[0] and all(
                            matches(v, dimensions[1:]) for v in value)
                    if not matches(sample.data, spec.shape):
                        raise ValueError(f"Invalid payload shape/values: {spec.name}")
                elif tuple(shape) != spec.shape:
                    raise ValueError(f"Invalid payload shape: {spec.name}")
                if shape is not None and spec.encoding in ("bgr8", "depth_mm"):
                    expected = "uint8" if spec.encoding == "bgr8" else "uint16"
                    if getattr(getattr(sample.data, "dtype", None), "name", None) != expected:
                        raise ValueError(f"Invalid image dtype: {spec.name}")
                buf.append(copy.deepcopy(sample))
        self.sequence += 1
        # History contains only causal samples; consumers choose alignment/interpolation.
        return Observation(self.sequence, now, {k: tuple(v) for k, v in self.buffers.items()})


class VideoObservation:
    """Borrow a started VideoCapture. No open/close, resize or color conversion."""
    def __init__(self, capture, prefix="camera", required=True, max_age_s=0.5):
        self.capture = capture
        self.names = {c.name: f"{prefix}/{c.name}" for c in capture.configs}
        self.streams = tuple(StreamSpec(self.names[c.name], "depth_mm" if c.kind == "depth" else "bgr8",
                                       (c.height, c.width) if c.kind == "depth" else (c.height, c.width, 3),
                                       "mm" if c.kind == "depth" else "uint8", c.name,
                                       max_age_s, required) for c in capture.configs)

    def snapshot(self):
        result = {}
        for name, key in self.names.items():
            frame, error, count = self.capture.snapshot(name)
            if frame is not None:
                result[key] = SensorSample(count, frame.received_at, frame.image, not bool(error), str(error or ""))
        return result


class TactileObservation:
    """Borrow named TactileReaders. Counts remain raw; never implicitly zero."""
    def __init__(self, readers, required=True, max_age_s=0.5):
        self.readers = dict(readers)
        self.streams = tuple(StreamSpec(f"tactile/{name}", "xyz_counts", (69, 3), "counts",
                                       name, max_age_s, required) for name in readers)

    def snapshot(self):
        result = {}
        for name, reader in self.readers.items():
            state = reader.snapshot()
            if state["sample"] is not None:
                sample = state["sample"]
                result[f"tactile/{name}"] = SensorSample(
                    state["sequence"], state["received_at"],
                    (sample.resultant, *sample.forces), state["state"] == "live", state["error"] or "")
        return result
