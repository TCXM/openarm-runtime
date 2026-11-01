"""Deterministic causal alignment; learning dependencies are imported on demand."""
import hashlib
import json
import math
from pathlib import Path

from .recording import atomic_json, inspect_episode, recover_episode, file_hash


def records(path, stream):
    with (Path(path) / "samples.jsonl").open() as file:
        for line in file:
            record = json.loads(line)
            if record["stream"] == stream:
                yield record


def aligned(path, fps=20):
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError("fps must be an integer in 1..120")
    path = Path(path)
    manifest = inspect_episode(path)
    if manifest.get("schema") != "openarm.collection/1":
        raise ValueError("Unsupported collection schema")
    review = json.loads((path / "review.json").read_text())
    if manifest["state"] != "review" or not review["accepted"]:
        raise ValueError("Export requires finalized, accepted data")
    if file_hash(path / "samples.jsonl") != manifest.get("journal_sha256"):
        raise ValueError("Journal checksum mismatch")
    integrity = recover_episode(path)
    if integrity["problem"]:
        raise ValueError(f"Raw archive integrity failure: {integrity['problem']}")
    stats = manifest["quality"]["streams"]
    specs = manifest["streams"]
    required = [name for name, spec in specs.items() if spec["required"]]
    if not required or any(name not in stats for name in required):
        raise ValueError("Missing required streams")
    start = max(stats[name]["first_at"] for name in required)
    end = min(stats[name]["last_at"] for name in required)
    if end < start:
        raise ValueError("Streams have no common time interval")
    iterators = {name: iter(records(path, name)) for name in specs}
    upcoming = {name: next(iterator, None) for name, iterator in iterators.items()}
    latest = {}
    for index in range(math.floor((end - start) * fps) + 1):
        timestamp = start + index / fps
        selected = {}
        for name, spec in specs.items():
            while upcoming[name] is not None and upcoming[name]["timestamp"] <= timestamp:
                latest[name] = upcoming[name]
                upcoming[name] = next(iterators[name], None)
            sample = latest.get(name)
            if sample is None or timestamp - sample["timestamp"] > spec["max_age_s"]:
                if spec["required"]:
                    raise ValueError(f"No fresh causal sample: {name} at frame {index}")
                selected[name] = None
            else:
                selected[name] = dict(sample, age_s=timestamp - sample["timestamp"])
                if name.startswith("robot/"):
                    before = sample["data"]["observation_before"]
                    if any(not 0 <= timestamp - state["received_at"] <= spec["max_age_s"]
                           for state in before.values()):
                        raise ValueError(f"Stale/noncausal robot state: {name}")
        yield dict(frame_index=index, timestamp=index / fps, source_timestamp=timestamp, streams=selected)


def provenance(path, fps):
    path = Path(path).resolve()
    digest = hashlib.sha256()
    with (path / "samples.jsonl").open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return dict(source=str(path), source_episode=inspect_episode(path), fps=fps,
                review=json.loads((path / "review.json").read_text()),
                alignment="latest_at_or_before_grid_time; no_interpolation",
                robot_state="feedback_available_before_selected_command",
                action="dispatched_joint_position_rad; gripper_is_motor_angle",
                journal_sha256=digest.hexdigest())


def export_aligned(path, output, fps=20):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    # Incomplete exports retain no export.json completion marker.
    with (output / "frames.jsonl").open("x") as file:
        for row in aligned(path, fps):
            file.write(json.dumps(row, allow_nan=False) + "\n")
    atomic_json(output / "export.json", provenance(path, fps))


def lerobot_features(manifest):
    joints = manifest["metadata"]["joint_order"]
    features = {key: dict(dtype="float32", shape=(len(joints),), names=joints)
                for key in ("observation.state", "observation.velocity", "observation.effort", "action")}
    for name, spec in manifest["streams"].items():
        if name.startswith("camera/"):
            shape = tuple(spec["shape"])
            features[f"observation.images.{name.split('/', 1)[1]}"] = dict(
                dtype="image" if spec["encoding"] == "bgr8" else "float32", shape=shape,
                names=["height", "width", "channels"] if len(shape) == 3 else ["height", "width"])
        elif name.startswith("tactile/"):
            features[f"observation.tactile.{name.split('/', 1)[1]}"] = dict(
                dtype="float32", shape=(69, 3), names=["taxel", "axis"])
    return features


def training_frame(path, row, manifest):
    import numpy as np
    frame = {key: [] for key in ("observation.state", "observation.velocity", "observation.effort", "action")}
    for side in manifest["metadata"]["sides"]:
        exchange = row["streams"][f"robot/{side}"]["data"]
        for mid in range(1, 9):
            state = exchange["observation_before"][str(mid)]
            for key, field in (("observation.state", "position"), ("observation.velocity", "velocity"),
                               ("observation.effort", "torque")):
                frame[key].append(state[field])
            frame["action"].append(exchange["targets_rad"][str(mid)])
    frame = {key: np.asarray(value, dtype=np.float32) for key, value in frame.items()}
    for name, spec in manifest["streams"].items():
        if not name.startswith(("camera/", "tactile/")):
            continue
        sample = row["streams"][name]
        if sample is None:
            raise ValueError(f"LeRobot export needs complete selected modalities: {name}")
        data = sample["data"]
        if name.startswith("camera/"):
            source = (Path(path) / data["array"]).resolve()
            if not source.is_relative_to(Path(path).resolve()):
                raise ValueError("Array path outside episode")
            if hashlib.sha256(source.read_bytes()).hexdigest() != data["sha256"]:
                raise ValueError("Array checksum mismatch")
            array = np.load(source, allow_pickle=False)
            if list(array.shape) != data["shape"] or str(array.dtype) != data["dtype"]:
                raise ValueError("Array metadata mismatch")
            frame[f"observation.images.{name.split('/', 1)[1]}"] = (
                array[:, :, ::-1].copy() if spec["encoding"] == "bgr8" else array.astype(np.float32))
        else:
            frame[f"observation.tactile.{name.split('/', 1)[1]}"] = np.asarray(data, dtype=np.float32)
    frame["task"] = manifest["metadata"]["task"]
    return frame


def export_lerobot(paths, output, repo_id, fps=20):
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from importlib.metadata import version
    paths = [Path(p) for p in paths]
    if not paths or Path(output).exists():
        raise ValueError("Provide episodes and a new output directory")
    first = inspect_episode(paths[0])
    features = lerobot_features(first)
    manifests = [inspect_episode(p) for p in paths]
    for manifest in manifests:
        if lerobot_features(manifest) != features:
            raise ValueError("Episodes must have identical joint/modalities schema")
    # Validate alignment/acceptance before allocating the dataset.
    for path in paths:
        for _ in aligned(path, fps):
            pass
    dataset = LeRobotDataset.create(repo_id=repo_id, root=Path(output), fps=fps,
                                   robot_type="openarm", features=features, use_videos=False, video_backend="pyav")
    try:
        for path, manifest in zip(paths, manifests):
            for row in aligned(path, fps):
                dataset.add_frame(training_frame(path, row, manifest))
            dataset.save_episode()
    finally:
        dataset.finalize()
    atomic_json(Path(output) / "openarm-export.json", dict(
        lerobot_version=version("lerobot"), episodes=[provenance(p, fps) for p in paths]))
