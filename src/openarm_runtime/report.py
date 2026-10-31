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
        self.data["events"].append({"elapsed_s": time.monotonic() - self.started, **event})
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w") as handle:
            json.dump(self.data, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
