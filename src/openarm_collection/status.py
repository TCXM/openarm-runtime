"""Best-effort cross-process preview status, separate from acquisition journals."""
from pathlib import Path
import os
import threading
import time
import uuid

from .recording import atomic_json


class StatusPublisher:
    def __init__(self, root, snapshot):
        self.identity = uuid.uuid4().hex
        self.path = Path(root) / '.runtime' / f'{self.identity}.json'
        self.snapshot = snapshot
        self.stop = threading.Event()
        self.error = ''
        self.thread = threading.Thread(target=self._run, name='collection-status', daemon=True)
        self.thread.start()

    def publish(self):
        try:
            data = self.snapshot()
            data.update(schema='openarm.collection-status/1', publisher=self.identity,
                        pid=os.getpid(), updated_wall_time_ns=time.time_ns())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.path, data)
            self.error = ''
        except Exception as exc:
            # Preview/storage failure must not stop native acquisition or CAN.
            self.error = str(exc)

    def _run(self):
        self.publish()
        while not self.stop.wait(.5):
            self.publish()
        self.publish()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
