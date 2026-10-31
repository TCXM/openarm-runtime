"""Pinned official OpenArm v2 visual assets, cached outside the repository."""

from __future__ import annotations

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen
from xml.etree import ElementTree as ET

REVISION = "2e27c793e10aa2045e7eb597b9b9b9dc08fb0a08"
URDF = "assets/robot/openarm_v2.0/urdf/example/v2.urdf"
ARM = "assets/robot/openarm_v2.0/meshes/arm/visual/"
EE = "assets/end_effector/pinch_gripper/meshes/visual/"
# Git blob hashes from the pinned official tree. No third-party meshes in Git.
FILES = {
    "LICENSE.txt": "261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64",
    URDF: "2e99c84d467b41964f704a1c1bbc4134aa2c30b9",
    ARM + "base_link.dae": "f2d59e6b7dab25357a03b7a330ea87d6a01ed106",
    ARM + "link1.dae": "4bc0358b2d5ed9fd315fefde9ee6f8504790e512",
    ARM + "link2.dae": "d40ec4b69a187fed0310587c88275cd4a67f934b",
    ARM + "link3.dae": "33a208b4db4a0a722085d831bc7b5d308fba86a8",
    ARM + "link4.dae": "25ce5512765c5c8d993402bad79a185e934e87e0",
    ARM + "link5.dae": "f3d3b206d8e3b2281a7dfb3981f29b0f80bc453d",
    ARM + "link6.dae": "04d37e7f8417be99fd9656c8516a489fc64f3cbc",
    EE + "ee_base_link.dae": "da3195b6a7aab93e9220d95a9c1a3f5c04a5fe2b",
    EE + "finger_inner.dae": "5fc329517a7f1c565c00fdff03bde2377efb58f7",
    EE + "finger_outer.dae": "9bef16bd65b05305365483fcea4fe7abce31cbde",
    "assets/robot/openarm_v2.0/meshes/body/visual/body_link0.dae": "ea69763e9e9d909a4955611e8de657f621f8a5c9",
}


def verify_blob(data: bytes, expected: str):
    digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
    if digest != expected:
        raise ValueError("Official asset hash mismatch; refusing modified data")


def asset_path(root: Path, relative: str) -> Path:
    if relative not in FILES:
        raise ValueError(f"Unregistered visual asset: {relative}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Asset path escapes cache root")
    return path


def prepare(root: Path):
    def download(item):
        relative, expected = item
        path = asset_path(root, relative)
        if path.exists():
            verify_blob(path.read_bytes(), expected)
            return
        url = f"https://raw.githubusercontent.com/enactic/openarm_description/{REVISION}/{relative}"
        with urlopen(url, timeout=90) as response:
            data = response.read(25_000_001)
        if len(data) > 25_000_000:
            raise ValueError("Asset exceeds size budget")
        verify_blob(data, expected)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(data)
        print(f"Verified {relative}", flush=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(download, FILES.items()))


def single_arm_xml(data: bytes, side: str) -> bytes:
    if side not in ("left", "right"):
        raise ValueError("Geometry side must be left or right")
    robot = ET.fromstring(data)
    kept = {"world", "openarm_body_link0"}
    kept.update(
        link.attrib["name"]
        for link in robot.findall("link")
        if link.attrib["name"].startswith(f"openarm_{side}_")
    )
    for element in list(robot):
        keep = False
        if element.tag == "link":
            keep = element.attrib["name"] in kept
            for collision in list(element.findall("collision")):
                element.remove(collision)
        elif element.tag == "joint":
            keep = (
                element.find("parent").attrib["link"] in kept
                and element.find("child").attrib["link"] in kept
            )
        if not keep:
            robot.remove(element)
    return ET.tostring(robot)


def component_xml(data: bytes, component: str) -> bytes:
    """Preserve official mount transforms; show the body once, not once per arm."""
    if component == "body":
        robot = ET.fromstring(data)
        for element in list(robot):
            if element.tag == "link" and element.attrib["name"] in ("world", "openarm_body_link0"):
                for collision in list(element.findall("collision")):
                    element.remove(collision)
            elif (
                element.tag == "joint"
                and element.attrib["name"] == "openarm_body_link0_mount_joint"
            ):
                continue
            else:
                robot.remove(element)
        return ET.tostring(robot)
    robot = ET.fromstring(single_arm_xml(data, component))
    body = robot.find("./link[@name='openarm_body_link0']")
    for visual in list(body.findall("visual")):
        body.remove(visual)
    return ET.tostring(robot)


def load_model(root: Path, side: str, *, component: bool = False):
    import yourdfpy

    for relative, expected in FILES.items():
        verify_blob(asset_path(root, relative).read_bytes(), expected)
    source = asset_path(root, URDF).read_bytes()
    data = component_xml(source, side) if component else single_arm_xml(source, side)

    def filename_handler(fname):
        prefix = "package://openarm_description/"
        if not fname.startswith(prefix):
            raise ValueError(f"Unexpected mesh URI: {fname}")
        return str(asset_path(root, fname[len(prefix) :]))

    return yourdfpy.URDF.load(
        io.BytesIO(data),
        filename_handler=filename_handler,
        load_meshes=True,
        load_collision_meshes=False,
    )


def joint_configuration(joint_names, states, side: str, gripper_mode: str = "fixed"):
    """Identity encoder mapping for 7 axes, not a zero/sign calibration.

    The optional pinch reference follows the official ROS2 driver's identity
    mapping, not a measurement/calibration of this physical gripper's transmission.
    """
    if gripper_mode not in ("fixed", "official-pinch"):
        raise ValueError("Unknown gripper display mapping")
    values = []
    for name in joint_names:
        expected = f"openarm_{side}_joint"
        if name.startswith(expected) and name[len(expected) :].isdigit():
            motor = int(name[len(expected) :])
            if motor not in range(1, 8) or motor not in states:
                raise ValueError(f"No measured joint for {name}")
            values.append(states[motor].position)
        elif name == f"openarm_{side}_finger_joint1":
            if gripper_mode == "fixed":
                values.append(0.0)  # Explicitly unmeasured geometry, marked in the UI.
            else:
                if 8 not in states:
                    raise ValueError("No measured gripper motor angle")
                values.append(states[8].position)
        else:
            raise ValueError(f"Unexpected actuated joint: {name}")
    return values
