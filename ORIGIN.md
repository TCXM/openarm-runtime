# Extraction provenance

Extracted on 2026-10-31 from the working tree of
`bimanual_tactile/code/hardware` at base commit `d458f39`.
The extraction includes the subsequently developed, not-yet-committed leader
reader/mapping/UI, simplified live-follow interface and nonblocking voltage-only
alarm behavior. It is not a claim that all extracted content was in that commit.

This repository is a separate local Git repository, not a nested repository or
submodule of the research project. After the initial extraction, the user approved
the consumer cutover: `bimanual_tactile/code/hardware` becomes an installation/config
entry point with a pinned dependency, and its duplicate source/tests are removed.
Both repositories use sibling checkouts locally and on the workstation. The consumer
records the required revision in `runtime-revision`; its installer rejects a different
or dirty runtime checkout. New bottom-layer development belongs here.

Pre-cutover source and machine configuration are archived outside both repositories.
The existing external SDK prefix, model assets, machine profile and calibration remain
the inputs of the installed package, rather than being moved into source control.

Extraction changes:
- Installable `src/` package with optional leader/follower/viewer dependencies.
- CAN telemetry moved from the viewer to the runtime; core imports no GUI package.
- Public exports, context-managed serial readers, and a GUI-independent whole-arm
  lifecycle using the established controller. Mode checks are shared with Viser.
- Tests import the installed package rather than inserting a checkout into sys.path.
- The historical CAN lock path remains shared with the original checkout, avoiding
  concurrent controllers during the migration period.

The original hardware gains, CAN frame behavior, signed encoder mapping, software
calibration JSON format and control limits are retained. Dependency/build metadata,
source layout and new public adapters are separately validated; no new physical
motion acceptance is implied by packaging tests.

No machine calibration, credentials, live reports, SDK binaries, model meshes or
the ignored reference `openarm/` repository are included. No GitHub remote is created.
