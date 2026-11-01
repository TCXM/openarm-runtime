"""Bounded asynchronous journal; an unfinished manifest is never training-ready."""
from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import uuid


def plain(value):
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def atomic_json(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as file:
        json.dump(plain(data), file, ensure_ascii=False, indent=2, allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Episode:
    """submit never writes to disk or waits for queue space.

    Producers transfer ownership of payloads: do not mutate submitted values.
    Arrays are persisted losslessly by the worker. Both item count and aggregate
    image bytes are bounded. A rejected sample invalidates the entire episode.
    """
    def __init__(self, root, metadata, streams, *, capacity=512, max_bytes=256 * 1024**2):
        if type(capacity) is not int or capacity < 1 or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("Queue limits must be positive integers")
        self.path = Path(root) / uuid.uuid4().hex
        self.path.mkdir(parents=True, exist_ok=False)
        self.queue = queue.Queue(capacity)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.error = ""
        self.bytes = 0
        self.max_bytes = max_bytes
        self.stats = {}
        self.manifest = dict(schema="openarm.collection/1", episode_id=self.path.name,
                             state="recording", started_at=time.monotonic(), wall_time_ns=time.time_ns(),
                             clock="host_monotonic_seconds", metadata=plain(metadata), streams=plain(streams))
        atomic_json(self.path / "manifest.json", self.manifest)
        self.thread = threading.Thread(target=self._write, name="episode-writer", daemon=True)
        self.thread.start()

    def fail(self, reason):
        with self.lock:
            self.error = self.error or str(reason)
            self.stop.set()

    def submit(self, stream, sequence, timestamp, data):
        with self.lock:
            if self.stop.is_set():
                return False
            if (stream not in self.manifest["streams"] or type(sequence) is not int or sequence < 0
                    or not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp)
                    or timestamp > time.monotonic() + .001):
                self.error = "Invalid stream, sequence or timestamp"
                self.stop.set()
                return False
            size = int(getattr(data, "nbytes", 0))
            try:
                if self.bytes + size > self.max_bytes:
                    raise queue.Full
                self.queue.put_nowait((stream, sequence, timestamp, data, size))
                self.bytes += size
            except queue.Full:
                self.error = "Recorder queue overflow"
                self.stop.set()
                return False
            return True

    def _write(self):
        try:
            with (self.path / "samples.jsonl").open("x") as file:
                index = 0
                while not self.stop.is_set() or not self.queue.empty():
                    try:
                        stream, sequence, timestamp, data, size = self.queue.get(timeout=.05)
                    except queue.Empty:
                        continue
                    previous = self.stats.get(stream)
                    if previous and (sequence <= previous["last_sequence"] or timestamp < previous["last_at"]):
                        raise ValueError(f"Sequence/timestamp regression: {stream}")
                    if hasattr(data, "dtype"):
                        import numpy as np
                        if data.dtype.hasobject:
                            raise TypeError("Object arrays are forbidden")
                        spec = self.manifest["streams"][stream]
                        if "shape" in spec and tuple(data.shape) != tuple(spec["shape"]):
                            raise ValueError(f"Unexpected shape: {stream}")
                        expected = {"bgr8": "uint8", "depth_mm": "uint16"}.get(spec.get("encoding"))
                        if expected and str(data.dtype) != expected:
                            raise ValueError(f"Unexpected dtype: {stream}")
                        name = f"arrays/{index:09d}.npy"
                        (self.path / "arrays").mkdir(exist_ok=True)
                        np.save(self.path / name, data, allow_pickle=False)
                        data = {"array": name, "shape": list(data.shape), "dtype": str(data.dtype),
                                "sha256": hashlib.sha256((self.path / name).read_bytes()).hexdigest()}
                    record = dict(stream=stream, sequence=sequence, timestamp=timestamp, data=plain(data))
                    file.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
                    stat = self.stats.setdefault(stream, dict(count=0, gaps=0, first_at=timestamp, max_gap_s=0))
                    if previous:
                        stat["gaps"] += max(0, sequence - stat["last_sequence"] - 1)
                        stat["max_gap_s"] = max(stat["max_gap_s"], timestamp - stat["last_at"])
                    stat.update(count=stat["count"] + 1, last_at=timestamp, last_sequence=sequence)
                    with self.lock:
                        self.bytes -= size
                    index += 1
                file.flush()
                os.fsync(file.fileno())
        except Exception as exc:
            self.fail(f"{type(exc).__name__}: {exc}")

    def finish(self, reason="operator", timeout=30):
        with self.lock:
            self.stop.set()
            self.manifest.setdefault("ended_at", time.monotonic())
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise RuntimeError("Recorder is still draining; episode remains unfinished")
        problems = [self.error] if self.error else []
        for name, spec in self.manifest["streams"].items():
            stat = self.stats.get(name)
            if spec.get("required", True):
                if not stat:
                    problems.append(f"Missing required stream: {name}")
                elif stat["gaps"] or stat["max_gap_s"] > spec.get("max_age_s", .5):
                    problems.append(f"Discontinuous required stream: {name}")
                elif (stat["first_at"] - self.manifest["started_at"] > spec.get("max_age_s", .5)
                      or self.manifest["ended_at"] - stat["last_at"] > spec.get("max_age_s", .5)):
                    problems.append(f"Missing stream boundary coverage: {name}")
        self.manifest.update(state="incomplete" if problems else "review", reason=reason,
                             quality=dict(problems=problems, streams=self.stats))
        journal = self.path / "samples.jsonl"
        self.manifest["journal_sha256"] = file_hash(journal) if journal.exists() else None
        atomic_json(self.path / "manifest.json", self.manifest)
        return self.path


def inspect_episode(path):
    """Read-only inspection also works after a crash; never promotes partial data."""
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text())
    return manifest


def recover_episode(path):
    """Preserve a crash journal and report its readable prefix; never repair in place.

    The acquisition process must be stopped first. No manifest or raw file is
    rewritten, so this inspection cannot race a writer into accepting data.
    """
    path = Path(path)
    result = dict(manifest=inspect_episode(path), readable_records=0, problem="")
    try:
        with (path / "samples.jsonl").open() as file:
            for line in file:
                record = json.loads(line)
                data = record["data"]
                if isinstance(data, dict) and "array" in data:
                    array = (path / data["array"]).resolve()
                    if not array.is_relative_to(path.resolve()):
                        raise ValueError("Array path outside episode")
                    if hashlib.sha256(array.read_bytes()).hexdigest() != data["sha256"]:
                        raise ValueError("Array checksum mismatch")
                result["readable_records"] += 1
    except (OSError, ValueError, KeyError) as exc:
        result["problem"] = str(exc)
    return result


def review_episode(path, outcome, *, accepted=True, note=""):
    if outcome not in ("success", "failure", "interrupted"):
        raise ValueError("Outcome must be success, failure or interrupted")
    path = Path(path)
    manifest = inspect_episode(path)
    if manifest["state"] not in ("review", "incomplete"):
        raise ValueError("Only finalized episodes can be reviewed")
    if accepted and manifest["state"] == "incomplete":
        raise ValueError("Incomplete data cannot be accepted for training")
    atomic_json(path / "review.json", dict(outcome=outcome, accepted=bool(accepted), note=note,
                                          reviewed_at_ns=time.time_ns()))
