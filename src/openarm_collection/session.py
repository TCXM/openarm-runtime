"""Operator lifecycle borrowing native capture owners; no hardware control."""
import copy
import threading
import time

from .recording import Episode, plain, review_episode


class Collection:
    def __init__(self, root, *, capacity=512, max_bytes=256 * 1024**2):
        self.root, self.capacity, self.max_bytes = root, capacity, max_bytes
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.episode = None
        self.last = None
        self.streams = {}
        self.latest = {}
        self.sequence = {}
        self.context = {}
        self.previews = {}
        self.tactile_values = {}
        self.previous_episode = None
        self.ready = False
        self.message = "待机"
        self.closed = threading.Event()
        self.monitor = threading.Thread(target=self._monitor, name="collection-health", daemon=True)
        self.monitor.start()

    def register(self, name, **spec):
        with self.lock:
            if self.episode:
                raise RuntimeError("Cannot change streams during recording")
            self.streams[name] = {"required": True, "max_age_s": .5, **spec}

    def emit(self, name, timestamp, data, sequence=None):
        with self.lock:
            if name not in self.streams:
                return
            if sequence is None:
                sequence = self.sequence.get(name, 0) + 1
            self.sequence[name] = sequence
            self.latest[name] = timestamp
            if self.episode:
                self.episode.submit(name, sequence, timestamp, data)

    def start(self, task, operator):
        with self.operation:
            return self._start(task, operator)

    def _start(self, task, operator):
        with self.lock:
            if self.closed.is_set() or not self.ready:
                raise RuntimeError("Start following and wait for takeover before recording")
            if self.episode or self.last:
                raise RuntimeError("Finish and review the previous episode first")
            if not task.strip() or not operator.strip():
                raise ValueError("Task and operator are required")
            now = time.monotonic()
            for name, spec in self.streams.items():
                age = now - self.latest.get(name, float("-inf"))
                if spec["required"] and not 0 <= age <= spec["max_age_s"]:
                    raise RuntimeError(f"Required stream unavailable: {name}")
            metadata = dict(copy.deepcopy(self.context), task=task, operator=operator,
                            previous_episode=self.previous_episode)
            streams = copy.deepcopy(self.streams)
        episode = Episode(self.root, metadata, streams,
                          capacity=self.capacity, max_bytes=self.max_bytes)
        with self.lock:
            self.episode = episode
            self.message = f"录制中 · {self.episode.path.name}"
            return self.episode.path

    def finish(self, reason="operator", error=""):
        with self.operation:
            return self._finish(reason, error)

    def _finish(self, reason, error):
        with self.lock:
            episode = self.episode
            if episode is None:
                return
            # Keep episode attached until drain completes, preventing another start.
            if error:
                episode.fail(error)
            else:
                episode.stop.set()
        path = episode.finish(reason)
        with self.lock:
            self.last = path
            self.episode = None
            self.message = f"待验收 · {episode.manifest['state']} · {path.name}"
            if episode.manifest["quality"]["problems"]:
                self.message += "\n\n" + "；".join(episode.manifest["quality"]["problems"])
        return path

    def review(self, outcome, accepted=True, note=""):
        with self.operation:
            return self._review(outcome, accepted, note)

    def _review(self, outcome, accepted, note):
        with self.lock:
            if self.last is None:
                raise RuntimeError("No finalized episode to review")
            path = self.last
        review_episode(path, outcome, accepted=accepted, note=note)
        with self.lock:
            self.previous_episode = self.last.name
            self.last = None
            self.message = "已保存，准备下一段" if accepted else "已标记弃用，准备重采"

    def _monitor(self):
        while not self.closed.wait(.05):
            with self.lock:
                episode = self.episode
                if episode is None:
                    continue
                error = episode.error
                now = time.monotonic()
                for name, spec in self.streams.items():
                    if spec["required"] and now - self.latest.get(name, 0) > spec["max_age_s"]:
                        error = error or f"Required stream stale: {name}"
            if error:
                try:
                    self.finish("acquisition_error", error)
                except Exception as exc:
                    self.message = str(exc)

    def close(self):
        self.closed.set()
        self.monitor.join(timeout=2)
        self.finish("shutdown", "Collection closed while recording")
