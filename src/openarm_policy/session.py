"""Episode orchestration; policies never access the robot or own its control loop."""
from dataclasses import dataclass, asdict
import threading
import time

from .contracts import ActionChunk, Context, finite
from .inference import InferenceWorker
from .observations import ObservationBuffer


@dataclass(frozen=True)
class SessionConfig:
    control_hz: float = 20
    inference_hz: float = 10
    inference_timeout_s: float = 0.4
    reset_timeout_s: float = 60
    source_timeout_s: float = 5
    command_timeout_s: float = 0.5
    max_horizon_s: float = 2
    history: int = 8

    def __post_init__(self):
        for name in ("control_hz", "inference_hz", "inference_timeout_s", "reset_timeout_s",
                     "command_timeout_s", "max_horizon_s", "source_timeout_s"):
            v = getattr(self, name)
            if not finite(v) or v <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.control_hz > 200 or self.inference_hz > self.control_hz:
            raise ValueError("Expected inference_hz <= control_hz <= 200")
        if type(self.history) is not int or not 1 <= self.history <= 256:
            raise ValueError("history must be in 1..256")


class PolicySession:
    """One episode. stop/pause ends it and disables; resume is a new reset/episode.

    One inference request is in flight. A new chunk replaces the old chunk at its
    start time; expired prefixes are skipped, never replayed from index zero.
    Future chunks wait while the active chunk continues. A gap holds the last
    target only for command_timeout_s, then terminates the episode.
    """
    def __init__(self, robot, policy, *, sources=(), config=None, task="", recorder=None):
        self.robot, self.policy = robot, policy
        self.sources = tuple(sources)
        self.config = config or SessionConfig()
        self.context = Context(robot.spec, tuple(robot.streams) + tuple(
            s for source in sources for s in source.streams), self.config.control_hz, task)
        self.buffer = ObservationBuffer(self.context.streams, self.config.history)
        self.recorder = recorder
        self.cancel = threading.Event()
        self.state = "idle"

    def stop(self):
        self.cancel.set()

    pause = stop

    def _record(self, event, **data):
        if self.recorder is not None:
            self.recorder.record({"event": event, "time": time.monotonic(), **data})

    def _observe(self):
        samples = self.robot.snapshot()
        for source in self.sources:
            samples.update(source.snapshot())
        return self.buffer.collect(samples)

    def _wait_for_sources(self):
        specs = tuple(s for source in self.sources for s in source.streams)
        probe = ObservationBuffer(specs, 1)
        deadline = time.monotonic() + self.config.source_timeout_s
        while not self.cancel.is_set():
            samples = {}
            for source in self.sources:
                samples.update(source.snapshot())
            try:
                probe.collect(samples)
                return
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                self.cancel.wait(.02)

    def run(self, duration_s=None):
        if self.state != "idle":
            raise RuntimeError("Create a new session for each episode")
        if duration_s is not None and (not finite(duration_s) or duration_s <= 0):
            raise ValueError("duration_s must be positive")
        worker = None
        primary_error = None
        self.state = "starting"
        try:
            self._record("context", context=asdict(self.context), config=asdict(self.config))
            worker = InferenceWorker(self.policy, self.context)
            worker.ready(self.config.reset_timeout_s, self.cancel.is_set)
            self._wait_for_sources()
            if self.cancel.is_set():
                return
            self.robot.start()
            self.state = "running"
            started = last_good = last_tick = time.monotonic()
            active = pending = request = None
            sent_at = 0.0
            next_infer = started
            held = None
            period = 1 / self.config.control_hz
            while not self.cancel.is_set():
                tick = time.monotonic()
                if duration_s is not None and tick - started >= duration_s:
                    break
                if tick - last_tick > max(0.25, 3 * period):
                    raise TimeoutError("Session deadline missed")
                last_tick = tick
                observation = self._observe()
                self._record("observation", observation=observation)
                if request is not None:
                    chunk = worker.result()
                    if chunk is not None:
                        if not isinstance(chunk, ActionChunk):
                            raise TypeError("Policy must return ActionChunk")
                        chunk.validate(self.robot.spec, request, time.monotonic(), self.config.max_horizon_s)
                        pending = chunk
                        self._record("chunk", chunk=asdict(chunk), latency_s=time.monotonic() - sent_at)
                        request = None
                    elif tick - sent_at >= self.config.inference_timeout_s:
                        raise TimeoutError("Policy inference timed out")
                now = time.monotonic()
                if pending is not None and pending.start_at <= now:
                    active, pending = pending, None
                values = active.at(now) if active is not None else None
                if values is not None:
                    held, last_good = tuple(values), now
                elif now - last_good >= self.config.command_timeout_s:
                    raise TimeoutError("Action queue starved")
                if held is not None:
                    deadline = last_good + self.config.command_timeout_s
                    self.robot.submit(held, deadline)
                    self._record("command", values=held, valid_until=deadline,
                                 observation_sequence=active.observation_sequence)
                if request is None and now >= next_infer:
                    worker.submit(observation)
                    request, sent_at = observation, time.monotonic()
                    next_infer = sent_at + 1 / self.config.inference_hz
                self.cancel.wait(max(0, period - (time.monotonic() - tick)))
        except BaseException as exc:
            primary_error = exc
            self.state = "failed"
            raise
        finally:
            errors = []
            # Hardware cleanup comes first, even if model cleanup or recording fails.
            for close in (self.robot.close, worker.close if worker else lambda: None):
                try:
                    close()
                except BaseException as exc:
                    errors.append(exc)
            if self.state != "failed":
                self.state = "stopped" if not errors else "failed"
            self._record("ended", state=self.state, error=str(primary_error or ""),
                         cleanup_errors=[str(e) for e in errors])
            if errors:
                raise BaseExceptionGroup("Policy session cleanup failed", ([primary_error] if primary_error else []) + errors)
