"""Optional Astra/OpenNI2 depth source, isolated from Python in a bounded process.

The caller supplies an executable built against one matching Orbbec SDK. No SDK
binaries, downloads, compilation or device operations happen during import.
"""

from importlib.resources import files
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .camera import Frame, _DeviceLease


def build_depth_helper(include_dir, library_dir, output, *, compiler="c++"):
    """Compile our adapter with caller-owned OpenNI2 headers/libs; no device I/O.

    ``library_dir`` must contain libOpenNI2.so and OpenNI2/Drivers/liborbbec.so
    from the same SDK as ``include_dir``. The executable embeds its absolute
    library path. SDK binaries remain external to the wheel/repository.
    """
    if sys.platform != "linux":
        raise RuntimeError("The OpenNI2 helper currently supports Linux only")
    include_dir = Path(include_dir).expanduser().resolve()
    library_dir = Path(library_dir).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    for path in (include_dir / "OpenNI.h", library_dir / "libOpenNI2.so",
                 library_dir / "OpenNI2/Drivers/liborbbec.so"):
        if not path.is_file():
            raise FileNotFoundError(f"Matching Orbbec OpenNI2 SDK file missing: {path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    source = files("openarm_video").joinpath("native/depth_capture.cpp").read_bytes()
    # Temporary build outputs never enter site-packages, and a failed build does
    # not replace a previously working helper.
    with tempfile.TemporaryDirectory(prefix=".openarm-depth-build-", dir=output.parent) as folder:
        cpp = Path(folder) / "depth_capture.cpp"
        executable = Path(folder) / "capture"
        cpp.write_bytes(source)
        subprocess.run([compiler, "-std=c++11", "-O2", "-Wall", "-Wextra",
                        "-I" + str(include_dir), str(cpp), str(library_dir / "libOpenNI2.so"),
                        "-Wl,-rpath," + str(library_dir), "-o", str(executable)],
                       check=True, timeout=120)
        os.replace(executable, output)
    return output


class OpenNIDepthCamera:
    """Synchronous uint16 millimetre source; use CameraReader for reconnect.

    ``read`` is cancellable by ``close`` and bounded to 25 s for initialization,
    then 3 s between frames. A native USB hang is terminated with the process.
    Zero pixels are invalid measurements, never a distance of zero.
    """

    def __init__(self, config):
        if config.kind != "depth":
            raise ValueError("OpenNIDepthCamera requires kind='depth'")
        if sys.platform != "linux":
            raise RuntimeError("Astra/OpenNI2 depth capture currently requires Linux")
        if not config.depth_helper:
            raise ValueError("Depth camera requires an external depth_helper; build_depth_helper() prepares it")
        helper = Path(config.depth_helper).expanduser().resolve()
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise ValueError(f"depth_helper is not executable: {helper}")
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("Install video dependencies: pip install -e '.[video]'") from exc
        self._cv2, self._np = cv2, np
        self.config = config
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._process = None
        self._folder = None
        self._log = None
        self._lease = None
        self._stamp = None
        self._sequence = 0
        self._deadline = time.monotonic() + 25
        try:
            self._lease = _DeviceLease("openni:" + (config.uri or "default"))
            self._folder = Path(tempfile.mkdtemp(prefix="openarm-video-depth-"))
            self._path = self._folder / "depth.pgm"
            self._log = (self._folder / "capture.log").open("wb")
            self._process = subprocess.Popen(
                [str(helper), str(self._path), str(config.width), str(config.height),
                 str(int(config.fps)), config.uri or ""],
                stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log)
        except BaseException:
            self.close()
            raise

    def _error(self):
        try:
            with (self._folder / "capture.log").open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 2048))
                return stream.read().decode(errors="replace").strip()
        except OSError:
            return ""

    def read(self):
        while not self._closed.is_set():
            if self._process.poll() is not None:
                raise OSError(self._error() or f"OpenNI2 helper exited with {self._process.returncode}")
            try:
                stamp = self._path.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = None
            if stamp is not None and stamp != self._stamp:
                image = self._cv2.imread(str(self._path), self._cv2.IMREAD_UNCHANGED)
                if image is None or image.dtype != self._np.uint16 or image.ndim != 2:
                    raise OSError("OpenNI2 helper returned an invalid uint16 depth image")
                self._stamp = stamp
                self._sequence += 1
                received = time.monotonic()
                self._deadline = received + 3
                return Frame(image, received, self._sequence, "depth_mm")
            if time.monotonic() > self._deadline:
                raise TimeoutError("No new OpenNI2 depth frame before deadline; check USB/data cable/power")
            self._closed.wait(.005)
        raise OSError("Depth camera is closed")

    def close(self):
        self._closed.set()
        with self._close_lock:
            if self._process is not None and self._process.poll() is None:
                try:
                    self._process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        self._process.kill()
                    except ProcessLookupError:
                        pass
                    # If even kill cannot reap the child, keep its lease and
                    # files: another capture must not race an orphaned process.
                    self._process.wait(timeout=2)
            if self._log is not None:
                self._log.close()
            if self._folder is not None:
                shutil.rmtree(self._folder, ignore_errors=True)
            if self._lease is not None:
                self._lease.close()
                self._lease = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
