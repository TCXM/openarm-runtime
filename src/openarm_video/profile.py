"""Explicit caller-owned JSON video configuration; no device discovery on import."""

from dataclasses import fields
import json
from pathlib import Path

from .camera import CameraConfig


def load_cameras(path):
    """Load ``{"cameras": [...]}``; resolve relative device/helper paths at the file.

    Unknown fields and duplicate names are rejected. VideoCapture also rejects
    duplicate device selections before capture starts.
    Empty device/helper fields are allowed only for explicitly simulated configs.
    """
    path = Path(path).expanduser().resolve()
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != {"cameras"}:
        raise ValueError("Video profile must contain exactly a 'cameras' array")
    entries = value["cameras"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("Video profile needs at least one camera")
    allowed = {field.name for field in fields(CameraConfig)}
    configs = []
    names = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ValueError(f"Invalid or unknown camera fields at index {index}")
        entry = dict(entry)
        for key in ("device", "depth_helper"):
            if entry.get(key) is not None and not isinstance(entry[key], str):
                raise ValueError(f"Camera {key} must be a path string")
            if entry.get(key):
                local = Path(entry[key]).expanduser()
                entry[key] = str((path.parent / local).resolve() if not local.is_absolute() else local)
        try:
            config = CameraConfig(**entry)
        except TypeError as exc:
            raise ValueError(f"Invalid camera entry {index}: {exc}") from exc
        if config.name in names:
            raise ValueError(f"Duplicate camera name: {config.name}")
        names.add(config.name)
        configs.append(config)
    return tuple(configs)


def simulation_cameras():
    """Four software-only views for UI checks; no machine-specific paths."""
    return (
        CameraConfig("head"), CameraConfig("depth", kind="depth"),
        CameraConfig("left", width=1280, height=720, fps=60, preview_width=960, preview_height=540),
        CameraConfig("right", width=1280, height=720, fps=60, preview_width=960, preview_height=540),
    )
