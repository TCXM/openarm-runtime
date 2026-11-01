"""Explicit composition: own sensors here OR bind already-owned local readers."""
from contextlib import contextmanager, ExitStack
from dataclasses import asdict
import json
from pathlib import Path

from .session import Collection


def bind_video(collection, reader):
    if reader.on_sample is not None:
        raise RuntimeError("Camera already has a recording sink")
    config = reader.config
    name = f"camera/{config.name}"
    collection.register(name, encoding="depth_mm" if config.kind == "depth" else "bgr8",
                        shape=[config.height, config.width] + ([] if config.kind == "depth" else [3]),
                        device=asdict(config), timestamp_semantics="host_received")
    def receive(frame, sequence):
        collection.previews[name] = (frame, sequence)
        collection.emit(name, frame.received_at, frame.image, sequence)
    reader.on_sample = receive
    return lambda: setattr(reader, "on_sample", None)


def bind_tactile(collection, reader, name, identity):
    if reader.on_sample is not None:
        raise RuntimeError("Tactile reader already has a recording sink")
    key = f"tactile/{name}"
    collection.register(key, encoding="xyz_counts", shape=[69, 3], identity=identity,
                        timestamp_semantics="host_received", baseline="raw_device_output_no_runtime_zero")
    def receive(sample, sequence, timestamp):
        collection.tactile_values[key] = sample.resultant
        collection.emit(key, timestamp, (sample.resultant, *sample.forces), sequence)
    reader.on_sample = receive
    return lambda: setattr(reader, "on_sample", None)


@contextmanager
def configured(path):
    """Collection-mode sensor ownership is explicit; never steals service devices."""
    path = Path(path).expanduser().resolve()
    data = json.loads(path.read_text())
    if set(data) - {"root", "video_profile", "tactile", "capacity", "max_bytes"}:
        raise ValueError("Unknown collection profile keys")
    resolve = lambda value: (path.parent / value).resolve()
    with ExitStack() as stack:
        collection = Collection(resolve(data["root"]), capacity=data.get("capacity", 512),
                                max_bytes=data.get("max_bytes", 256 * 1024**2))
        stack.callback(collection.close)
        collection.context["collection_profile"] = data
        if data.get("video_profile"):
            from openarm_video import VideoCapture, load_cameras
            capture = VideoCapture(load_cameras(resolve(data["video_profile"])))
            for reader in capture.readers.values():
                stack.callback(bind_video(collection, reader))
            stack.enter_context(capture)
        for name, options in data.get("tactile", {}).items():
            from openarm_tactile import TactileReader
            identity = options.get("identity")
            if not isinstance(identity, dict) or not identity.get("serial") or not identity.get("mount"):
                raise ValueError("Tactile identity requires serial and physical mount")
            reader = TactileReader(**{k: v for k, v in options.items() if k != "identity"})
            stack.callback(bind_tactile(collection, reader, name, identity))
            stack.enter_context(reader)
        yield collection
