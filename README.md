# OpenArm Runtime

A small Python toolkit for **OpenArm follower control, Feetech leader readout,
and optional Viser interfaces**. Use the Python API in your own application, or
the CLI for live inspection and user-operated control.

Python 3.12 · MIT · Linux SocketCAN for followers · Independent community project

## Install only what you need

Create a Python 3.12 virtual environment, then install from a release tag:

```sh
python3.12 -m venv .venv
source .venv/bin/activate

# Core: state types, software mapping and control API; no GUI or hardware SDK.
pip install "openarm-runtime @ git+https://github.com/TCXM/openarm-runtime.git@v0.2.0"

# Serial leader readout, with a browser-based virtual robot.
pip install "openarm-runtime[leader,viewer] @ git+https://github.com/TCXM/openarm-runtime.git@v0.2.0"
```

Extras compose freely:
- `leader`: Feetech SDK + PySerial for STS3215 buses.
- `viewer`: Viser + URDF/mesh loading.
- `follower`: official OpenArm CAN SDK 1.4.1 and its build dependencies.

The follower extra requires a Linux machine and the matching native C++ SDK.
Follow [workstation setup](docs/workstation.md) before installing it. The core
and leader APIs import without Viser; importing a module never opens hardware.
Distribution is through GitHub tags and release artifacts, not PyPI.

## Using the package vs developing it

**Use a released version in an existing project:** no manual clone is needed.
The installer fetches the tagged source and installs the package into your
project's environment. With uv, declare and lock the dependency directly:

```sh
# Run in your own Python 3.12 project.
uv add "openarm-runtime[leader,viewer] @ git+https://github.com/TCXM/openarm-runtime.git@v0.2.0"
```

**Modify the runtime while developing your application:** clone the source
anywhere you like, then install it in editable mode into your application's
activated Python 3.12 environment:

```sh
git clone https://github.com/TCXM/openarm-runtime.git /path/to/openarm-runtime
python -m pip install -e "/path/to/openarm-runtime[leader,viewer]"
```

Replace `/path/to/openarm-runtime` with your chosen directory. It does not need
to be inside or next to your application's repository. Editable installation
makes imports use that checkout, so Python source edits take effect when you
restart your application; you do not need to reinstall for each edit. Keep the
checkout in place while using it. Dependency, entry-point or build changes may
require reinstalling; native SDK changes require rebuilding the SDK.

For a uv-managed application, use a local editable dependency so later `uv sync`
commands preserve your development setup:

```sh
uv add --editable "/path/to/openarm-runtime[leader,viewer]"
```

This records a local source dependency. Before sharing or deploying the
application, replace it with a published tag or commit using the versioned
`uv add` command above and commit the updated dependency files. The `follower`
extra still requires [native SDK setup](docs/workstation.md) in either mode.

To develop the runtime on its own, use `uv sync --locked` in this repository;
see [contributing](CONTRIBUTING.md).

## Read a leader in Python

```python
from openarm_leader import LeaderBus

port = "/dev/serial/by-id/YOUR_LEADER_ADAPTER"
with LeaderBus(port) as leader:
    sample = leader.sample()
    print(sample.positions)  # Eight encoder readings, ordered by servo ID 1–8.
    print(sample.voltages)   # Volts; alarms remain available in sample.errors.
```

For continuous background reads, use `LeaderReader`. For calibrated joint angles
and normalized gripper opening, use `Mapping`. See the [API guide](docs/api.md).

Runnable examples accept device paths rather than assuming a machine layout:

```sh
git clone https://github.com/TCXM/openarm-runtime.git
cd openarm-runtime
python examples/read_leader.py --port /dev/serial/by-id/YOUR_LEADER_ADAPTER
python examples/read_follower.py --interface can0
```

Both examples only read hardware. Press Ctrl+C to close the connection.

## Open the interfaces

Prepare the official model cache once (downloads assets outside the repository):

```sh
export MODEL_ROOT="$HOME/.cache/openarm-models"
openarm-runtime prepare --asset-root "$MODEL_ROOT"
```

**Leader → virtual robot:** move a physical leader and inspect its mapped pose.
This mode reads serial buses only; it does not command follower hardware.

```sh
openarm-runtime leader \
  --left-port /dev/serial/by-id/LEFT_ADAPTER \
  --right-port /dev/serial/by-id/RIGHT_ADAPTER \
  --calibration "$HOME/.config/openarm/leader.json" \
  --asset-root "$MODEL_ROOT" --port 8890
```

**Follower:** copy [classic.example.json](classic.example.json) to your own config
directory, set the CAN interfaces, and confirm their physical left/right mapping.
The host links must already be configured for Classic CAN at 1 Mbps.

```sh
export MACHINE_CONFIG="$HOME/.config/openarm/classic.json"
openarm-runtime --config "$MACHINE_CONFIG" check
openarm-runtime --config "$MACHINE_CONFIG" view --asset-root "$MODEL_ROOT"
openarm-runtime --config "$MACHINE_CONFIG" panel \
  --asset-root "$MODEL_ROOT" --report-root "$HOME/openarm-reports" --port 8891
```

Run `view` and `panel` separately. `check` and `view` are read-only. The control
panel starts read-only; clicking enable holds the selected arm's current pose,
then sliders set relative targets. Stop attempts to disable the complete arm
and verifies feedback. UI labels are currently Chinese.

Servers bind to loopback. For a remote robot, forward ports with SSH and open
`http://127.0.0.1:8890` (leader) or `http://127.0.0.1:8891` (follower) in Chrome:

```sh
ssh -N -L 8890:127.0.0.1:8890 -L 8891:127.0.0.1:8891 your-workstation
```

## Integrate without a browser

The public API consists of:

- `openarm_leader`: `LeaderBus`, `LeaderReader`, `Sample`, `Mapping`.
- `openarm_runtime`: `Profile`, `hold_arm`, `calibrate_arm`, `ArmConnection`, `MotorState`, `MotorRange`.

`hold_arm(profile, side)` owns preflight, bus locking, enable and exit cleanup.
Your application owns the control loop and supplies relative targets to
`arm.tick()`. Entering the context enables the physical arm. The
[API guide](docs/api.md) explains units, scheduling and a complete hold-only loop.

Configuration, calibration, assets and logs are explicit paths owned by the caller.
No research repository, sibling checkout, or running web server is required.

## Automatic follower calibration

Select an arm and **自动标定** in the panel to perform single-arm
stop-based calibration and hardware zero writes over Classic CAN. The same
operation is available through `calibrate --side left|right --report-root PATH`
and `calibrate_arm()`. Both arms completed zero writes and one power-cycle
readback test on the development rig; quantified accuracy and repeatability are
not yet established. Read the [operator guide](docs/calibration.md) for the
required starting pose, motion sequence, stop behavior and partial-write recovery.

## Scope

The working baseline covers eight motors per follower arm, Classic CAN,
whole-arm hold/relative targets, two STS3215 leader buses and virtual pose mapping.
Manual control uses validated fixed gains, ±30° relative offsets and
0.1–5°/s target speed. See [validation](docs/validation.md) for measured evidence.

Full leader-to-follower physical teleoperation, collision avoidance, quantified
calibration accuracy and long-term reliability are future work. Firmware encoding
ranges are not mechanical joint limits. Software cleanup cannot replace a physical
stop mechanism when a process or connection fails.

## Develop

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests
uv build
```

Offline tests require no hardware. The official-model test additionally needs
the `viewer` extra and `OPENARM_VIEWER_ASSET_ROOT`. CI tests the built wheel outside
the source tree on Linux and macOS. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
release workflow and compatibility policy.

Implementation lives in `src/`, regression tests in `tests/`, and small API usage
examples in `examples/`. [Changelog](CHANGELOG.md) · [Provenance](ORIGIN.md) ·
[Third-party notices](THIRD_PARTY.md) · [MIT license](LICENSE)
