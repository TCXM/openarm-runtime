# Linux workstation installation

Requirements: Python3.12, uv, Git, CMake and a C++ toolchain. Serial leader and
Viser users only need the extras in the README; the native build below is for
follower control. Keep SDK and model installations outside the source repository.

## Build the official SDK

Reuse a verified existing installation, or build these pinned revisions on a new
Linux workstation:

```sh
export DEPS="$HOME/robot_deps"
mkdir -p "$DEPS"
git clone https://github.com/CLIUtils/CLI11.git "$DEPS/CLI11"
git -C "$DEPS/CLI11" checkout 4160d259d961cd393fd8d67590a8c7d210207348
cmake -S "$DEPS/CLI11" -B "$DEPS/CLI11/build" \
  -DCLI11_BUILD_TESTS=OFF -DCLI11_BUILD_EXAMPLES=OFF \
  -DCMAKE_INSTALL_PREFIX="$DEPS/cli11-v2.5.0"
cmake --build "$DEPS/CLI11/build" --parallel 4
cmake --install "$DEPS/CLI11/build"

git clone https://github.com/enactic/openarm_can.git "$DEPS/openarm_can"
git -C "$DEPS/openarm_can" checkout bfd4e52194aefe38389fa6e670be3738136facb9
export OPENARM_CAN_PREFIX="$DEPS/openarm-can-1.4.1"
cmake -S "$DEPS/openarm_can" -B "$DEPS/openarm_can/build" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
  -DCMAKE_PREFIX_PATH="$DEPS/cli11-v2.5.0" \
  -DCMAKE_INSTALL_PREFIX="$OPENARM_CAN_PREFIX" \
  -DCMAKE_INSTALL_RPATH="$OPENARM_CAN_PREFIX/lib"
cmake --build "$DEPS/openarm_can/build" --parallel 4
cmake --install "$DEPS/openarm_can/build"
```

Run `sh sync.sh` at this repository's root to install all extras. It installs
build dependencies first, then passes the nanobind path, native SDK prefix and
RPATH to the Python binding build. `uv.lock` pins dependencies. If you move the
native installation prefix, rebuild the bindings.

## Install the follower extra in another project

In your application's activated Python3.12 virtual environment, set
`OPENARM_RUNTIME_ROOT` to a checkout of a release tag, then run:

```sh
uv pip install nanobind==2.10.2 scikit-build-core==1.1.1
export CMAKE_ARGS="-Dnanobind_DIR=$(python -m nanobind --cmake_dir) -DCMAKE_DISABLE_FIND_PACKAGE_OpenArmCAN=OFF -DCMAKE_PREFIX_PATH=$OPENARM_CAN_PREFIX -DCMAKE_INSTALL_RPATH=$OPENARM_CAN_PREFIX/lib"
export CMAKE_BUILD_PARALLEL_LEVEL=4
uv pip install --no-build-isolation-package openarm-can "${OPENARM_RUNTIME_ROOT}[follower]"
```

For a wheel, replace the last argument with
`'/path/to/openarm_runtime-0.2.0-py3-none-any.whl[follower]'`. You can also use
`'openarm-runtime[follower] @ git+https://github.com/TCXM/openarm-runtime.git@v0.2.0'`.
Consumers do not inherit a dependency repository's uv build settings, so retain
the explicit build configuration and `--no-build-isolation-package` option.

Verify the installation without opening hardware:

```sh
python -c 'import openarm_runtime, openarm_can; print(openarm_runtime.__version__, openarm_can.__version__)'
uv pip check
```

## Configure and run

- Start from `classic.example.json`, stored outside the repository. Set the two
  interfaces and `mapping_verified=true` after checking physical left/right wiring.
- Validated links: Classic1 Mbps, FD off, restart-ms0; motor can_br4. Commands
  inspect this configuration rather than changing it automatically.
- Use stable serial device paths for leaders. Software origins and gripper
  endpoints are saved in an external calibration JSON file.
- Serial access is managed by the OS. Temporary ACLs may disappear on unplug;
  persistent udev rules should target each adapter's unique serial and actual user.
- Forward loopback Viser with SSH:
  `ssh -N -L 127.0.0.1:8890:127.0.0.1:8890 workstation`.
- Redirect background service stdout/stderr to external logs. Stop with SIGTERM
  or Ctrl+C so cleanup runs. Closing an SSH tunnel does not stop a remote service.

Run one serial reader per device and one controller per CAN bus. The shared CAN
lock namespace interoperates with the original research installation, but cannot
coordinate unrelated third-party controllers.
