"""Thin composition root; machine/policy choices live in caller-owned JSON."""
from contextlib import ExitStack
import json
import signal

from .adapters import ImportedPolicy
from .observations import VideoObservation, TactileObservation
from .recording import EpisodeRecorder
from .robots import ClassicRobot, SimulatedRobot
from .session import PolicySession, SessionConfig


def run(args):
    data = json.loads(args.profile.read_text()) if args.profile else {}
    unknown = set(data) - {"session", "policy_kwargs", "task", "video_profile", "tactile"}
    if unknown:
        raise ValueError(f"Unknown policy profile keys: {sorted(unknown)}")
    config = SessionConfig(**data.get("session", {}))
    if args.robot == "classic":
        from openarm_runtime.config import Profile
        profile = Profile.load(args.config)
        if profile.rate_hz != config.control_hz:
            raise ValueError("Policy control_hz must match Classic profile rate_hz")
        robot = ClassicRobot(profile, tuple(args.sides), watchdog_s=config.command_timeout_s)
    else:
        robot = SimulatedRobot(tuple(args.sides))
    with ExitStack() as stack:
        recorder = stack.enter_context(EpisodeRecorder(args.record)) if args.record else None
        if recorder is not None and isinstance(robot, ClassicRobot):
            robot.record = recorder.record
        sources = []
        if data.get("video_profile"):
            from openarm_video import VideoCapture, load_cameras
            capture = stack.enter_context(VideoCapture(load_cameras(data["video_profile"]), simulate=args.robot == "simulate"))
            sources.append(VideoObservation(capture))
        if data.get("tactile"):
            from openarm_tactile import TactileReader
            readers = {}
            for name, options in data["tactile"].items():
                if args.robot == "simulate":
                    options = {"simulate": True}
                readers[name] = stack.enter_context(TactileReader(**options))
            sources.append(TactileObservation(readers))
        session = PolicySession(robot, ImportedPolicy(args.policy, data.get("policy_kwargs")),
                                sources=sources, config=config, task=data.get("task", ""), recorder=recorder)
        previous = {sig: signal.signal(sig, lambda *_: session.stop()) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            session.run(args.duration)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
