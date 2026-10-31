# Changelog

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
