# Changelog

## Unreleased

- Add independent `openarm_video` package and `video` extra, with passive imports,
  configurable RGB/OpenNI2 sources, timestamped frames, device leases, reconnect
  lifecycle, and explicit software-only simulation.
- Add `openarm-runtime video`, packaged four-view assets, Safari-compatible finite
  JPEG/PNG requests and raw millimetre-depth download. The vendor SDK and machine
  configuration remain outside the package; no existing service is migrated.

## 0.2.0 — 2026-10-31

- Final whole-arm zero convergence corrects drift after intermediate zero moves;
  all eight joints must settle together before disabled-pose checks and writes.

- Calibration moves now use slow, bounded target correction for static position
  error; actual-zero tolerance and manual-hold gains are unchanged. Failure reports
  include target/goal/feedback history captured before disable.
- Calibration move/seek speeds are now 20/10 degrees per second, with ramped
  upstream per-action Kp/Kd settings. Inactive-joint failures include the offending
   feedback.

- Single-arm v2 automatic calibration over Classic CAN, available through the
  Python API, CLI and a simplified arm/action selector in the follower panel.
- Panel assumes MIT when RID10 times out, without an advanced checkbox; explicit
  non-MIT readback is still rejected. CLI/API retain the explicit timeout option.
- Bounded stop seeking, computed-zero positioning, cancellation/disable cleanup,
  and disabled-pose checks with per-motor hardware zero write reports.
- Offline virtual-stop and failure-path tests. Both arms completed zero writes
  (right 139 s, left 126 s); all 16 angle readbacks were unchanged after one user
  power cycle. Absolute accuracy and repeatability remain unquantified.
- Existing hold/leader APIs remain compatible; native SDK and CAN bitrate are
  unchanged. See `docs/calibration.md` for operation and recovery.

## 0.1.0 — 2026-10-31

Initial public release.

- Installable core with composable `leader`, `follower` and `viewer` extras.
- Official OpenArm CAN1.4.1 encoding, Classic CAN telemetry, whole-arm hold and
  bounded relative targets, shared bus ownership and verified disable cleanup.
- Read-only STS3215 leader transport, background reconnect and device-bound
  software pose/gripper calibration.
- Optional Viser follower panel and live leader-to-virtual-model display.
- Public Python APIs, read-only examples, wheel-based offline CI and MIT license.

Known boundaries: Python3.12; follower hardware requires Linux and the native SDK;
GUI labels are Chinese. Physical leader-to-follower teleoperation is not included.
