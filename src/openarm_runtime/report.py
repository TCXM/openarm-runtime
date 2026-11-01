"""Operation evidence written outside the source tree; no hardware dependencies."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import queue
import threading


class Report:
    def __init__(self, path, data):
        self.path = Path(path)
        self.data = {**data, "started_utc": datetime.now(timezone.utc).isoformat(), "events": []}
        self.started = time.monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("x") as handle:
            json.dump(self.data, handle, indent=2)

    def record(self, event):
        self.data["events"].append({"elapsed_s": time.monotonic() - self.started, **event})
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w") as handle:
            json.dump(self.data, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)


class AsyncReport:
    """Bounded teleoperation diagnostic journal; legacy JSON is finalized on close.

    The append journal survives a process interruption. Control producers neither
    serialize nor wait for storage. Failure remains visible to the caller.
    """
    def __init__(self, path, data, capacity=1024):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.header = {**data, "started_utc": datetime.now(timezone.utc).isoformat()}
        self.started = time.monotonic()
        self.journal = self.path.with_suffix(".events.jsonl")
        self.queue = queue.Queue(capacity)
        self.stop = threading.Event()
        self.error = None
        with self.path.open("x") as file:
            json.dump({**self.header, "state": "recording", "events": []}, file)
        self.thread = threading.Thread(target=self._run, name="teleop-report", daemon=True)
        self.thread.start()

    def record(self, event):
        if self.error:
            raise RuntimeError(f"Diagnostic writer failed: {self.error}")
        if self.stop.is_set():
            raise RuntimeError("Diagnostic report closed")
        try:
            self.queue.put_nowait({"elapsed_s": time.monotonic() - self.started, **event})
        except queue.Full as exc:
            self.error = RuntimeError("Diagnostic report queue overflow")
            raise self.error from exc

    def _run(self):
        try:
            with self.journal.open("x") as file:
                while not self.stop.is_set() or not self.queue.empty():
                    try:
                        event = self.queue.get(timeout=.05)
                    except queue.Empty:
                        file.flush()
                        continue
                    file.write(json.dumps(event, allow_nan=False) + "\n")
                file.flush()
                os.fsync(file.fileno())
        except Exception as exc:
            self.error = exc

    def close(self):
        self.stop.set()
        self.thread.join(timeout=30)
        if self.thread.is_alive() or self.error:
            raise RuntimeError(f"Diagnostic report incomplete: {self.error or 'drain timeout'}")
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as output, self.journal.open() as source:
            output.write(json.dumps(self.header, allow_nan=False)[:-1] + ', "events": [\n')
            separator = ""
            for line in source:
                output.write(separator + line.rstrip())
                separator = ",\n"
            output.write("\n]}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)
