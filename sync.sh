#!/bin/sh
# Install Python bindings only; never configure CAN or construct a motor object.
set -eu
cd "$(dirname "$0")"

if [ "$(uname -s)" != Linux ]; then
    echo 'The hardware SDK requires Linux with SocketCAN.' >&2
    exit 1
fi
: "${OPENARM_CAN_PREFIX:?Set OPENARM_CAN_PREFIX to the installed OpenArm CAN 1.4.1 C++ prefix}"
OPENARM_CAN_PREFIX=$(realpath "$OPENARM_CAN_PREFIX")
test -f "$OPENARM_CAN_PREFIX/lib/cmake/OpenArmCAN/OpenArmCANConfig.cmake"

# The upstream build fails to locate nanobind automatically with CMake 3.28.
# Install the locked build dependencies first, then give CMake their real path.
uv sync --locked --all-extras --no-install-package openarm-can
NB_DIR=$(uv run --no-sync python -m nanobind --cmake_dir)
export CMAKE_ARGS="-Dnanobind_DIR=$NB_DIR -DCMAKE_DISABLE_FIND_PACKAGE_OpenArmCAN=OFF -DCMAKE_PREFIX_PATH=$OPENARM_CAN_PREFIX -DCMAKE_INSTALL_RPATH=$OPENARM_CAN_PREFIX/lib"
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-4}"
uv sync --locked --all-extras
