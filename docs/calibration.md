# Automatic v2 follower calibration

This operation **moves one physical arm and writes all eight hardware zeros**.
It supports OpenArm v2 with its stock gripper over Classic CAN at 1 Mbps. The
official SDK is unchanged. Available since v0.2.0. Both arms completed hardware
zero writes and one power-cycle readback test on the development rig on
2026-10-31. Quantified absolute accuracy, repeatability and other rigs are not
yet validated. See [validation](validation.md) for the measured scope.

## Operator workflow

1. Verify the profile's physical left/right assignment. Remove payloads and clear
   the entire sweep of both arms, including cables and nearby equipment. A
   collision can be indistinguishable from a mechanical stop to this algorithm.
2. Position the selected arm approximately at the [official reference pose](https://docs.openarm.dev/setup/openarm-setup/motor-config/#step-2-zero-position-calibration).
   Start near the reference pose so the prescribed sweep and bounded seek travel
   are appropriate. Inferred initial angles are checked against the side-specific
   mechanical ranges, not a ±25-degree zero-pose window. The gripper can start
   open (up to 100 degrees of motor travel). These checks happen after finding
   stops, not before movement, and do not verify collision clearance. Ensure
   support when motors are disabled.
3. In the follower panel, select **机械臂** and **自动标定**, mark the reference pose
   and clear sweep ready, then click **开始标定**. This runs the complete
   sequence, returns to computed zero, disables and writes/verifies the zeros.
   The stop button or disconnecting the initiating browser requests cancellation.
4. Inspect the report and model alignment. Repeatability, several independent
   poses and a power-cycle readback are required before accepting the calibration.

The CLI is an alternative to the panel. Stop readers/panels on the selected bus
first; the command acquires exclusive ownership. Executing it starts motion:

```sh
openarm-runtime --config /path/to/classic.json calibrate \
  --side right --report-root /path/to/reports
```

Ctrl+C requests stop and disable. `--allow-unreadable-mode` has the same explicit
RID10-timeout exception as normal control; readable non-MIT modes remain rejected.
The operator panel enables this timeout exception by default for the MIT setup;
there is no extra checkbox. CLI and Python callers retain their explicit option.
Software stop is not a substitute for a physical emergency stop or arm support.

## Sequence and zero calculation

IDs 1–7 are joints; ID8 is the gripper. For each stop:
`raw_zero = measured_raw_stop - known_model_stop`. Targets then use
`raw_zero + model_angle`. Encoder directions must match the stock assembly.

The sequence follows the upstream v2 ordering:

1. Close the gripper, then seek J4's 0-degree stop.
2. Move J2 5 degrees away from its nearby stop; seek J3 at +90 degrees and
   return J3 to zero. Restore J2's initial angle; move J4 to 90 degrees.
3. Seek and zero J5 (+90), J6 (+45), and J7 (+90).
4. Seek J2 at right −10 / left +10 degrees; return it to zero.
5. Move J4 to 135 degrees; seek J1 at right −80 / left +80 degrees and zero it.
6. Move J4 through 90 degrees to zero. Simultaneously fine-adjust all eight joints
   using bounded correction for up to 25 seconds. Require measured zero errors
   within 0.5 degrees and velocity below 0.08 rad/s for six consecutive frames,
   matching the 0.5-degree disabled/write checks. Record the raw
   solution, disable, then write and verify each zero. Completing the 20 motion
   stages alone does not mean calibration succeeded.

The gripper only receives a closed zero; no maximum-opening or millimeter mapping
is measured. Initial pose and stop geometry are assumptions, not measurements of
collision-free workspace. A virtual mechanical-stop model verifies the arithmetic
and lifecycle but does not simulate gravity, compliance, friction or real contact.

## Execution bounds

- 20 Hz target loop; moves up to 20 degrees/s, seeks up to 10 degrees/s,
  zero feedforward. Calibration uses the upstream per-action gains: normal moves
  52/1.5, normal seeks 45/1.2, J1 seek 280/2.1, J2 zero move 280/2.0 (Kp/Kd).
  Initial holding ramps to 52/1.5 over one second; each active-joint gain change
  ramps over 0.5 seconds, while other joints retain their last gains. Normal
  manual holding is unchanged. Upstream's separate gripper pre-close (10/0.9)
  is not a stage in this executor, which directly seeks closed zero.
  The gains match upstream; Classic whole-arm timing, limits and final zero
  convergence remain runtime-specific, not a verbatim upstream script.
- Overall 900-second limit; 250 ms control deadline. Callbacks must stay fast.
- Moves accelerate at 40 degrees/s² toward the 20 degrees/s ceiling. Their
  command lead is limited by 70% of firmware torque range minus damping effort,
  divided by Kp. The former fixed 2.5-degree lead cap starved J4 of holding torque
  at upstream Kp52 and has been removed. A blocked move still times out.
- Stop thresholds: J1 5 Nm, other joints 2 Nm, gripper 0.3 Nm. Firmware torque
  ranges are checked before enable, including the known 3 Nm ID5 configuration.
- Seeking spring loading is limited by 15 degrees, 80% of firmware torque range,
  or 1.5 times the stop threshold, whichever is smallest. Predicted/measured
  effort above 90% of firmware range is rejected.
- Six consecutive low-velocity, loaded feedback samples with less than 0.2-degree
  position spread are needed for a stop. Stop travel limits are ID8 110 degrees,
  J4 60, J3/J5/J7 120, J6 75, J2 40 and J1 110.
- Inactive joints receive continuous bounded target correction (up to 3 degrees/s,
  firmware-derived loading limit and 30-degree correction ceiling). If physical
  drift exceeds 3 degrees for one second despite compensation, calibration aborts.
  Loaded MIT command offsets are not counted as physical drift. Active moves use
  effort limits and arrival timeout instead.
  Calibration rejects velocity
  above 0.7 rad/s, position jumps above 5 degrees, stale/missing/faulted feedback
  and control deadlines. Zero-pose/write tolerance is 0.5 degrees.
- After reaching a move's nominal target and waiting one second, low-velocity
  position error can accumulate a bounded target correction: at most 1 degree/s,
  limited by the firmware-effort-derived lead and an absolute 30-degree ceiling.
  Correction does not change gains or feedforward. Completion still uses
  the actual measured position versus the original goal (0.5 degrees), never the
  corrected command. Existing effort/tracking guards remain active; each move's
  settling allowance is 20 seconds. This compensates static load error but cannot
  distinguish load from contact and is not a collision detector. Failed motion
  retains up to 200 samples, written to the report after disable is attempted.

## Writes and recovery

The computed raw offsets are recorded before any zero writes. All motors must
report disabled, stationary and at their expected raw zeros before writing; each
write checks its motor again and verifies near-zero feedback. Stop/exception
cleanup attempts to disable all enabled motors and reports unconfirmed disable.
If gravity moves a joint after disable, writing is refused rather than accepting
that displaced pose. Disabled support is particularly important here.

Hardware writes are **not atomic**. A report includes every attempt, verified ID,
and partial completion. A failed send may still have reached the motor. There is
no automatic rollback: inspect the report and current readback before retrying a
complete calibration from the reference pose. The record preserves evidence, not
a promise that the old hardware origins can be restored automatically.

No CAN baud, control-mode, flash or save-parameter commands are issued. The motor
firmware determines whether its zero command persists; persistence remains a
separate power-cycle acceptance check. The tool does not apply an extra software
offset after hardware zeros have been written.

## Provenance

The sequence and nominal stop angles were audited from Enactic's
[`openarm-can-zero-position-calibration`](https://github.com/enactic/openarm_can/blob/bfd4e52194aefe38389fa6e670be3738136facb9/setup/openarm-can-zero-position-calibration)
and the v2 URDF. The runtime executor is independently implemented. It replaces
the upstream hardcoded FD transport, aliased J1/gripper identities, unbounded seek
loops and omitted precise-home step; the upstream SDK checkout is not patched.
