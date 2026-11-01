"""Operation evidence written outside the source tree; no hardware dependencies."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time


class Report:
    def __init__(self, path, data):
        self.path = Path(path)
        self.data = {**data, "started_utc": datetime.now(timezone.utc).isoformat(), "events": []}
        self.started = time.monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("x") as handle:
            json.dump(self.data, handle, indent=2)

    def record(self, event):
        self.record_at(event, time.monotonic() - self.started)

    def record_at(self, event, elapsed):
        self.data["events"].append({"elapsed_s": elapsed, **event})
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w") as handle:
            json.dump(self.data, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)


class QueuedReport:
    """Bounded, ordered reporting without disk I/O on a motion thread.

    Capture event time at enqueue, surface writer errors to the controller, and
    drain after disable. Queue saturation raises rather than losing evidence.
    """

    def __init__(self, path, data):
        import queue
        import threading
        self.report = Report(path, data)
        self.queue = queue.Queue(maxsize=128)
        self.stop = threading.Event()
        self.error = None
        self.worker = threading.Thread(target=self._write, daemon=True, name="openarm-report")
        self.worker.start()

    def check(self):
        if self.error is not None:
            raise RuntimeError(f"Report writer failed: {self.error}") from self.error

    def record(self, event):
        import copy
        import queue
        self.check()
        if self.stop.is_set():
            raise RuntimeError("Report is closed")
        elapsed = time.monotonic() - self.report.started
        try:
            self.queue.put_nowait((copy.deepcopy(event), elapsed))
        except queue.Full:
            raise RuntimeError("Report queue full; stopping instead of dropping evidence") from None

    def _write(self):
        import queue
        try:
            while not self.stop.is_set() or not self.queue.empty():
                try:
                    event, elapsed = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                self.report.record_at(event, elapsed)
        except BaseException as exc:
            self.error = exc

    def close(self):
        self.stop.set()
        self.worker.join(timeout=10)
        if self.worker.is_alive():
            raise RuntimeError("Report flush timed out")
        self.check()
