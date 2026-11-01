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

## Automatic follower calibration (v0.2.0)

`calibrate_arm(profile, side, *, cancelled, record, publish, progress,
allow_unreadable_mode=False, write_zero=True)` synchronously performs a complete single-arm v2
calibration and writes hardware zeros. `side` is `left` or `right`. It owns the
bus lease, preflight, enable, motion and disable; stop any reader first.

The retained local `write_zero=False` option runs the same physical trajectory,
then verifies the disabled end pose without writing any encoder zeros. It still
requires the calibration reference starting pose; the end pose may have nonzero
readings in the preserved encoder coordinates. CLI: `calibrate --no-write-zero`.

It returns `{motor_id: raw_zero_radians}` for IDs1–8 (the pre-write encoder frame).
`cancelled()` defaults to false; `record(event)`, `publish(states)` and
`progress(message)` default to no-ops. For real operation, supply a durable
`record`, such as `openarm_runtime.report.Report(...).record`, and a cancellation
callback. The CLI and panel already provide both. Callback exceptions abort the
operation; callbacks must not block the control loop. Errors may occur after
partial hardware writes. No automatic rollback is attempted.

See [calibration](calibration.md) before calling: this is a motion API, not a
passive estimator. No GUI, native SDK or bus is loaded/opened merely by import.

## Compatibility

The names documented here are the supported integration surface. Other modules
are implementation details. During0.x, pin a release tag or commit; incompatible
API changes will be called out in the changelog and receive a minor-version bump.

## Video

`CameraConfig(name, device="", kind="rgb", width=640, height=480, fps=30, ...)`
contains only configuration. RGB uses an explicit device path; depth uses
`depth_helper` and optional OpenNI `uri`. Paired `preview_width`/`preview_height`
affect JPEG previews only. `load_cameras(path)` loads a tuple from an external
`{"cameras": [...]}` JSON profile. See [video setup](video.md).

`Frame(image, received_at, sequence=0, pixel_format="bgr8")` contains a NumPy image
and monotonic receive time. RGB is H×W×3 uint8 BGR (`bgr8`); depth is H×W uint16
millimetres (`depth_mm`), where0 means invalid. `fresh(now, max_age=3)` rejects stale
or future timestamps. Images are owned by the source and shared by consumers;
treat them as read-only and copy before changing them.

`OpenCVCamera(config)` and `OpenNIDepthCamera(config)` open synchronous sources.
`read()` returns a `Frame`; use a context manager or `close()` for cleanup. Depth
requires the caller's Linux OpenNI2 helper. `build_depth_helper(include_dir,
library_dir, output)` compiles our bundled source against an external matched SDK,
without opening hardware. Missing video dependencies produce an actionable error.

`CameraReader(config, stale_after=3, retry_interval=1)` is passive until `start()` or
context entry, then reads/reconnects in a background worker. `snapshot()` returns
`(frame_or_none, error, count)`. A retained frame after an error is not a live sample:
check both `not error` and `frame.fresh(time.monotonic())`. `status()` provides state,
error, count, dimensions, kind, simulation flag and frame age. `close()` is
idempotent after successful cleanup; failed cleanup is reported. A closed reader
cannot be restarted; create another instance.

`VideoCapture(configs, simulate=False, stale_after=3)` owns named readers.
Constructing opens nothing. `start()`/context entry starts them; `close()` attempts
to close all readers even if one fails. `snapshot(name)` returns the reader tuple;
`status()` maps names to status dictionaries. `simulate=True` explicitly generates
software RGB/depth frames and never accesses devices. An optional
`source_factory(config)` supplies a custom source implementing `read()` and
idempotent, bounded `close()` (useful for offline tests). The reader owns and
closes the returned source.

`encode_frame(frame, raw=False, near=0.3, far=4)` returns `(bytes, MIME type)` for
JPEG RGB or PNG depth. Raw depth is unchanged; preview bounds are metres and invalid
pixels remain black. This function does not start capture or require a web server.

`create_app(capture)` returns a Flask app without starting/closing its capture group.
`serve(capture, host="127.0.0.1", port=5003)` binds first, starts the group, blocks
serving HTTP and always closes it on exit. Use `create_app` for an existing application
that already owns capture; use `serve` for a standalone single-process server.


## Tactile

`openarm_tactile` exports `TactileReader`, `Sample`, `SENSOR` (PXSR-STDIP03A) and
`POINT_COUNT` (68). Importing these names loads no serial SDK, GUI or hardware.
Install the `tactile` extra for acquisition; the `viewer` extra is only needed
for `openarm-runtime tactile` visualization.

`TactileReader(device=None, simulate=False, baudrate=921600, device_id=1)` is
passive until `start()` or context entry. Supply exactly one of a serial path
or `simulate=True`. The supported board is a single-channel serial converter;
module ID is independent of the Linux serial-port number. The validated devices
use ID 2. Hardware reads are bounded and use exclusive POSIX serial ownership.

`snapshot()` returns `state`, `error`, `sample`, `sequence`, `age_s` and `rate_hz`.
Only consume a sample as live when `state == "live"`; states also include
`waiting`, `stale` (older than 0.5 s), `error` and `closed`. The frozen `Sample`
contains an XYZ `resultant` tuple and 68 XYZ `forces` tuples. Values are raw counts:
Fx/Fy are signed and Fz is unsigned. Resultant and distribution are separate
transactions, not a synchronized sensor frame. No calibration or hardware writes
are performed; acquisition is limited to 60 Hz.

Failures retain the previous sample as invalid and close the port. `restart()`
explicitly reconnects an already-started reader; no failure falls back to
simulation. `close()` joins the worker with a 3 s bound, reports failure and is
idempotent after cleanup. A closed reader cannot be started again.
