# Video setup

The optional `openarm_video` package separates device access, background capture,
encoding and web presentation. It follows the runtime's passive imports, explicit
configuration and context-managed lifecycle. Python3.12 is required.

## Install

```sh
# Pinned release:
python -m pip install "openarm-runtime[video] @ git+https://github.com/TCXM/openarm-runtime.git@v0.6.0"
# Or develop this checkout:
python -m pip install -e ".[video]"
# Alternatively, for this repository's uv workflow:
uv sync --locked --extra video
```

The `video` extra pins OpenCV headless, NumPy and Flask. No display server, Viser,
serial SDK or CAN SDK is needed. Device access is first attempted by constructing
a synchronous camera, or starting/entering a reader/capture group.

## Profiles

Copy `video.example.json` to a caller-owned configuration directory and set real
paths. Each entry has a unique `name`, `kind` (`rgb` or `depth`), resolution and
FPS. RGB uses `device`; optional paired `preview_width`/`preview_height` resize
only HTTP previews, never Python samples. Depth uses `depth_helper` and optionally
an OpenNI `uri` when multiple devices are connected. The first depth is used by
`/depth.png`; all depths can be requested by name.

Relative device/helper paths resolve relative to the profile file. `~` expands
to the current user's home. Unknown fields and duplicate camera names are rejected;
duplicate RGB devices are rejected by the capture group. Examples contain only
placeholder paths: machine profiles, firmware, logs and SDK builds stay outside Git.

## Camera paths after USB changes

A reachable video page does not mean its device paths still exist. If only the
wrist views remain disconnected, compare the external profile with:

```sh
v4l2-ctl --list-devices
ls -l /dev/v4l/by-id /dev/v4l/by-path
```

Check `/status` (or `/apps/vision/status` through the platform), including each
camera's `details.error`, frame count and frame age. Missing `/dev/camera_*`
aliases usually require correcting their udev rules or selecting an existing
stable path explicitly in the external video profile.

Two identical cameras can report the same serial identifier and therefore
collide under `by-id`. Use distinct `by-path` entries ending in `video-index0`
for these cameras, and confirm physical left/right assignment from their images.
USB topology paths survive changing `/dev/videoN` numbers but change when the
camera or hub moves to a different port; recheck the mapping after such a move.
Do not silently assign left/right from enumeration order.

The platform's vision service should launch the package's `video --profile`
command. When replacing the old RGB/depth relay scripts, stop their capture
processes normally before starting the new owner, then remove obsolete `support`
entries from the external platform configuration. Keep a copy of both profiles
for rollback. No machine-specific paths or calibration belong in the repository.

## Astra Pro depth helper

Older Astra Pro depth data is read through the matched Orbbec OpenNI2 library and
`liborbbec.so`, separately from RGB UVC/V4L2. Use headers and runtime libraries from
one vendor distribution, not a mixture of the system OpenNI package and vendor SDK.
Prepare a directory with:

```text
/path/to/openni/include/OpenNI.h
/path/to/openni/lib/libOpenNI2.so
/path/to/openni/lib/OpenNI.ini
/path/to/openni/lib/OpenNI2/Drivers/liborbbec.so
/path/to/openni/lib/OpenNI2/Drivers/orbbec.ini
```

Compile this package's adapter on Linux (a C++ compiler is required):

```python
from openarm_video import build_depth_helper
build_depth_helper(
    include_dir="/path/to/openni/include",
    library_dir="/path/to/openni/lib",
    output="~/.cache/openarm-video/astra-depth-capture",
)
```

The build opens no camera and performs no downloads or firmware changes. The
helper embeds the external SDK's absolute library path; moving the SDK requires
rebuilding. Its source is included in wheels/sdists, but the compiled helper and
SDK are not. Set `depth_helper` to the resulting executable in your video profile.
The old standalone helper has a different argument interface; build this adapter
rather than reusing that executable.

The process requests depth in millimetres, disables stream mirroring, discards
warmup frames and atomically publishes 16-bit PGM samples. Python validates uint16
images and exposes them as `Frame(pixel_format="depth_mm")`. A zero pixel is invalid
measurement. Web download is lossless 16-bit PNG; colour preview is visualization.
A child USB hang is bounded by startup/frame deadlines and terminate/kill cleanup.

## Run and forward

```sh
# Explicit synthetic images, never open hardware:
openarm-runtime video --simulate --port 5004
# Or use your devices after other owners have released them:
openarm-runtime video --profile ~/.config/openarm/video.json --port 5003
```

Forward the selected port from the client computer:

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 5003:127.0.0.1:5003 your-workstation
```

Open `http://127.0.0.1:5003` in Safari, Chrome or Firefox. The server binds loopback,
uses one process and independent capture workers, and closes on Ctrl+C/SIGTERM.
Use one hardware capture group; do not run multiple Flask workers or a reloader
against the same devices. Device leases coordinate this library's instances;
legacy standalone services do not share those leases and must be stopped separately
when the user chooses to migrate. Nothing here stops them automatically.

The API supports multiple cameras/depth sources. Software simulation is clearly
marked. Camera frames are not synchronized or RGB/depth registered. Native OpenCV
read calls can still be blocked by a faulty driver; shutdown reports an error if
it cannot safely release a source rather than claiming it stopped.

## HTTP integration

`create_app(capture)` borrows the group without starting or closing it. Your
application owns its lifecycle. `serve(capture, port=...)` binds first, then starts
and always closes it. Neither mode needs the old5000/5002 servers.

- `/config`: camera names/modes and simulation flag; no device paths.
- `/status`: per-camera status, error, frame age, count and depth statistics.
- `/frame/<name>.jpg`: finite RGB preview, with `X-Frame-Sequence`/`X-Frame-Age-Ms`.
- `/depth/<name>.png?near=0.3&far=4`: finite colourized depth, bounds in metres.
- `/depth/<name>.png?raw=1`: lossless uint16 millimetre PNG.

No frame or a stale/error frame returns503; invalid names return404; invalid depth
bounds return400. Browsers make sequential requests per visible camera, avoiding
unbounded queues and Safari's MJPEG Fetch failure. Pause/cancellation and transient
status errors do not clear other working images.
