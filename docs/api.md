# Python API

All examples assume an installed package and Python 3.12. Optional dependencies
are loaded only by operations that need them.

## Leaders

`LeaderBus(port)` opens one 1 Mbps STS3215 serial bus with IDs1–8. Its `sample()`
returns a `Sample`: ordered position, voltage, torque-enable and error tuples,
a monotonic timestamp and read duration. Use it as a context manager to close
the port on exit. No servo-register writes are issued.

`LeaderReader(port)` runs reads in a background thread and reconnects after
transport failures. Use `with LeaderReader(port) as reader:` or explicitly call
`start()` and `close()`. `snapshot()` returns `(sample, error, count)`; a sample
may initially be `None`. Check `not error` and `sample.fresh(time.monotonic())`
before consuming it. A voltage-only alarm on otherwise valid feedback is
nonblocking; other faults and corrupt/stale reads are not accepted for display.

`Mapping(left_port, right_port, calibration_path)` binds software calibration to
device identity. `angles(side, positions)` returns seven joint angles in radians
and gripper opening in [0, 1], or `None` for an uncalibrated gripper. `capture(side,
sample, kind, now)` records `zero`, `open` or `closed`; `save()` persists the
calibration. Capture uses fresh, torque-disabled samples. These are software
origins, not writes to hardware encoder zero registers.

## Follower telemetry

`ArmConnection(interface, "classic")` is a read-only background connection.
Call `restart()` to start, `shared.snapshot()` to retrieve
`(states, error, message, replies, missed)`, and `close()` on exit.
Each `MotorState` has a motor ID, position in radians, velocity in rad/s, torque
in Nm, temperatures, status, frame format and a monotonic receive timestamp.
Use `state.fresh(time.monotonic(), max_age)` to check freshness.

`MotorRange` describes firmware encoding ranges, not mechanical collision limits.
The [read-only example](../examples/read_follower.py) handles the lifecycle.

## Follower control

`Profile.load(path)` loads an external JSON file; see
[classic.example.json](../classic.example.json). Physical mapping must be confirmed
before control. Stop any telemetry reader on the selected bus before entering
`hold_arm`: the controller acquires an exclusive bus lease.

The following loop enables one arm, holds its current position and attempts to
disable it when interrupted. Run it only when you intend to enable that arm:

```python
import signal
import threading
import time
from openarm_runtime import Profile, hold_arm

profile = Profile.load("/path/to/classic.json")
stop = threading.Event()
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stop.set())

period = 1.0 / profile.rate_hz
offsets = dict.fromkeys(range(1, 9), 0.0)
try:
    with hold_arm(profile, "right", cancelled=stop.is_set) as arm:
        while not stop.is_set():
            start = time.monotonic()
            arm.tick(offsets, speed=1.0, period=period)
            stop.wait(max(0.0, period - (time.monotonic() - start)))
except InterruptedError:
    pass  # The context manager has already attempted disable and readback.
```

`offsets` must contain all IDs1–8, in degrees relative to the freshly captured
enable pose. Limits: ±30° per offset, speed0.1–5°/s, period0.02–0.1 s. The default
profile targets20 Hz. The caller owns scheduling and cancellation; no hidden
control thread is started. This is a best-effort Python loop, not hard real-time.

Optional `record(event)` and `publish(states)` callbacks receive lifecycle events
and feedback. Keep them fast and nonblocking. Exceptions exit the context and
attempt disable; failure to confirm disable raises an error. Do not suppress that
error in an application.

`allow_unreadable_mode=True` explicitly permits a known RID10 timeout for MIT
operation; a readable non-MIT value is always rejected. Keep the default unless
your hardware has been separately verified for this behavior.

## Compatibility

The names documented here are the supported integration surface. Other modules
are implementation details. During0.x, pin a release tag or commit; incompatible
API changes will be called out in the changelog and receive a minor-version bump.
