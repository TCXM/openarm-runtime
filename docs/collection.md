# Teleoperation demonstration collection

The operator keeps following active between episodes, records an explicit segment,
then labels its outcome and accepts or rejects it. Ending a recording does not
stop following. Stopping following/disconnecting retains the existing disable
behavior and invalidates any episode still being recorded.

## Operator workflow

1. Start the leader page with `--follow --collection-profile /path/to/collection.json`.
   This only opens the passive page and acquisition devices, not motor motion.
2. Select the participating arm(s), click **① 从臂缓慢回零**, wait for hold, then
   click **② 开始相对跟随**. The current leader pose becomes the relative origin;
   precise leader alignment is not required.
3. Enter a task description and operator. All required streams must be fresh.
4. Click **开始录制**, demonstrate, then **结束录制**. Recording is independent of
   following; repositioning between demonstrations is not part of an episode.
5. Choose success, failure or interrupted; optionally add a note. **保存 · 下一段**
   accepts a complete episode. **弃用 · 重采** rejects it without deleting anything.
   The next episode references the preceding episode ID.

Controls live in the existing Viser teleoperation page, including camera previews,
stream health, elapsed duration and queue load. Controls use the current following
browser owner. Standard browser focus/Tab/Space navigation works; global keyboard
bindings are not installed. A finalized episode must be reviewed before another
can begin. A failed task may be accepted; corrupted/incomplete acquisition cannot.

## Sensor ownership

Copy [collection.json](../examples/collection.json) outside Git and replace paths
and device identities. Paths for `root` and `video_profile` are relative to that
profile. Tactile identity includes the actual serial and physical mount, not an
assumed one-sensor-per-arm layout. Optional unused video/tactile entries may be
omitted; all configured sensors are required in the first CLI profile version.

In collection mode the **teleoperation process owns configured cameras and tactile
readers**. Stop separate vision/tactile services that own those same devices before
starting collection mode, and configure the platform not to auto-start competing
services. This does not silently stop or migrate running platform services. Camera
previews appear on the teleoperation page, backed by the same capture owners.
The existing platform can launch this command as its `teleop` service; keep its
`motion` exclusive group. There is no second CAN owner for collection.

Embedded applications can instead call `bind_video(collection, camera_reader)`
and `bind_tactile(collection, tactile_reader, name, identity)` with already-owned
local readers. These bindings return detach callbacks and do not open or close
devices. Capture sinks receive each sample directly from the acquisition worker;
they do not poll a latest-frame preview. Cross-process service streaming is not
implemented; HTTP preview frames are not a raw acquisition source.

## Raw schema: `openarm.collection/1`

Each random-ID episode directory contains:

```text
manifest.json       schema, clocks, configuration, quality and completion state
samples.jsonl       per-source native-time events, not resampled training rows
arrays/*.npy        lossless camera/depth frames with shape, dtype and SHA256
review.json         outcome, acceptance and operator note
```

Stream names and meanings:

- `leader/<side>`: every leader read, raw registers, voltage/error fields and read
  duration. Its timestamp is **read start**, as defined by the existing leader SDK
  adapter. Eight sequential register reads are not simultaneous joint measurements.
- `intent/<side>`: calibrated angles/opening, relative takeover offsets and clipped
  follower references used by that control cycle, plus the source leader timestamp.
- `robot/<side>`: command dispatch start, exchange completion, targets in radians,
  gains, feedforward Nm, gravity diagnostics, feedback available **before** the
  command and returned feedback. Every motor retains its own receive timestamp.
  Only completed, validated exchanges produce these events; a partial exchange
  terminates following and makes the open episode incomplete.
- `camera/<name>`: original BGR uint8 or depth uint16 millimetres, at the capture
  owner's host receive timestamp. No resizing or lossy codec in the raw archive.
- `tactile/<name>`: raw device output, resultant followed by 68 taxels, each XYZ;
  counts are not silently converted to Newtons or baseline-corrected.

All times use this host's monotonic clock, with a wall-clock nanosecond anchor for
identification. Receive times are not exposure times or hardware synchronization.
Multiple hosts need an explicit clock-mapping design before combining streams.
Source sequences identify distinct reads; repeated samples in exported frames
retain their original sequence and age. Upstream device losses invisible to the
driver cannot be inferred from host sequence numbers.

Metadata captures the runtime version, follower profile, joint order, leader
mapping, takeover origins, preflight, gains, SDK version and gravity configuration
(including the model provenance exposed by the controller). J8 is the **gripper
motor angle**, not normalized opening or finger displacement. Configurations and
calibration belong to the episode, even if machine files subsequently change.

## Recording and failure semantics

The capture/control callback only submits to a bounded in-memory queue. It never
performs journal writes, image serialization or waits for queue space. One writer
persists raw samples; count and aggregate in-flight image byte limits are explicit.
Payload ownership transfers at submission: callers must not subsequently mutate
arrays/dictionaries. Built-in readers produce new immutable sample objects/arrays.

Teleoperation's existing diagnostic reports also use a bounded background append
journal (`*.events.jsonl`); after motor cleanup they finalize the legacy JSON
report once. They no longer rewrite/fsync the entire report on every control tick.

Overflow, serialization/disk failures, missing streams, stale required streams,
sequence regressions/gaps and excessive temporal gaps invalidate the episode.
The health worker ends recording on acquisition failure without changing the
robot's existing control lifecycle. Quality includes counts, gaps, first/last
timestamps and largest inter-sample interval per stream. A recording error cannot
turn into an accepted demonstration merely by selecting “success”.

`finish()` drains the writer before atomic manifest completion. The manifest stays
`recording` after a crash or drain timeout; only `review` plus an accepted review
can be exported. `incomplete` and rejected episodes remain on disk. Journal and
manifest are flushed/fsynced at completion; this is not a power-loss-proof database.
Raw NPY storage prioritizes fidelity and inspectability; provision SSD capacity
and validate sustained throughput at the actual camera resolutions before a long
collection session. A full queue is reported, never silently sampled down.

```sh
openarm-runtime collection inspect /data/raw/EPISODE_ID
openarm-runtime collection recover /data/raw/EPISODE_ID
openarm-runtime collection review /data/raw/EPISODE_ID --outcome failure --note "grasp missed"
```

`recover` reports the readable journal prefix and verifies referenced arrays,
without overwriting the journal or promoting a crash episode to training-ready.

## Reproducible training export

```sh
openarm-runtime collection export /data/raw/EPISODE_ID \
  --output /data/aligned/EPISODE_ID --fps 20
```

The aligned export stores source references, sequences and sample ages; keep its
raw source directory. It uses the common required-stream time interval and the
latest sample **at or before** each grid time, without interpolation or future
images. Missing/stale data rejects export. Robot state is the feedback available
before the selected command; action is the dispatched joint-position reference.
Post-command feedback remains in raw data for tracking analysis. Multi-arm
dispatch remains asynchronous; each arm's selected sequence and age stay visible.

For a self-contained LeRobot dataset use a **separate export environment**:
LeRobot's NumPy constraints differ from the runtime's pinned camera environment.
Install this runtime wheel/core package and `lerobot[dataset]==0.6.1` there.

```sh
openarm-runtime collection export /data/raw/EPISODE_A /data/raw/EPISODE_B \
  --output /data/training/reach --format lerobot --fps 20 --repo-id local/reach
```

This calls the LeRobot create/add_frame/save_episode/finalize API locally, never
uploads. It exports position/velocity/effort observations, target actions, RGB
images (explicit BGR→RGB), tactile counts, task text and numeric depth arrays in
millimetres when configured. Images are image-backed, not lossily encoded video.
All episodes must share the joint/modalities schema. Raw intent and controller
diagnostics remain in the source archive; `openarm-export.json` records source
manifests, journal hashes, alignment rules, FPS and the installed LeRobot version.
This final file is the export-completion marker; a failed export must not be used.

## Offline rehearsal and validation

```sh
python examples/collect_simulated.py --root /path/outside/git/demo --duration 2
python examples/collect_simulated.py --root /path/outside/git/demo --ui
```

The first needs NumPy, the second also Viser. Both synthesize data without opening
serial ports or CAN. Offline tests cover lifecycle, overflow, writer failure,
stale streams, read-only recovery, causal alignment and action/state separation.
LeRobot 0.6.1 roundtrip was checked locally with synthetic RGB, tactile and robot
data. Operator-run dual-arm trials on 2026-11-01 exercised three RGB cameras,
two tactile pads, homing, relative takeover and accepted episode recording.
This validates the collection workflow, not task success or workspace-wide accuracy.

## Operator return to zero

J1–J7 PD gains follow [Enactic OpenArm v2 control_gains.yaml](https://github.com/enactic/openarm_description/blob/main/assets/robot/openarm_v2.0/config/arm/control/control_gains.yaml):
Kp = 70, 70, 70, 60, 10, 10, 10; Kd = 2.75, 2.5, 2, 2, 0.7, 0.6, 0.5.
The shared follower hold/tracking controller uses these values. The upstream file
does not specify the gripper; J8 retains Kp 25 and Kd 0.3. Tracking performance
with these gains remains dependent on motion and load.

The collection page starts with **① 从臂缓慢回零**, followed by
**② 开始相对跟随**. Return moves selected follower joints J1–J7 to existing
calibrated motor zero with a synchronized smooth trajectory (peak 5 degrees/s).
It preserves the current gripper command and never writes firmware zero offsets.
The follower holds there until the operator resumes. Resume captures the current
leader pose (including gripper) as the relative origin, without leader alignment
requirements, and requires fresh, settled follower feedback.
Recording is unavailable during return/hold; end an active
recording before requesting another return. Stop/disconnect retains disable cleanup.
This joint-space return does not plan around obstacles: clear its path before
clicking the button. Real hardware return remains an operator-run acceptance check.

## Unified platform preview (v0.8.1)

Use the platform's **数采可视化** module to monitor the collecting process and
replay existing native archives. See [configuration and preview boundaries](platform.md#collection-visualization).
Status snapshots under `.runtime/` are diagnostic state, not dataset episodes;
raw manifests, samples and array payloads retain `openarm.collection/1` format.
