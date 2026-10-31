#!/usr/bin/env python3
"""Launch the configured dual tactile page using this checkout's environment."""
import json
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parent
    python = root / ".venv" / "bin" / "python"
    config_path = Path.home() / ".config" / "openarm-runtime" / "tactile.json"
    try:
        if not python.is_file():
            raise ValueError(f"找不到项目虚拟环境：{python}")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        left = Path(config["left_device"]).expanduser()
        right = Path(config["right_device"]).expanduser()
        for device in (left, right):
            if not device.is_absolute():
                raise ValueError("设备配置必须使用绝对路径")
            if not device.exists():
                raise ValueError(f"未找到触觉设备，请检查 USB 连接：{device}")
        if left.resolve() == right.resolve():
            raise ValueError("左右触觉不能使用同一个设备")
        args = [
            str(python), "-m", "openarm_runtime", "tactile",
            "--device", str(left), "--right-device", str(right),
            "--device-id", str(config["device_id"]),
            "--port", str(config.get("port", 8891)),
            "--baudrate", str(config.get("baudrate", 921600)),
        ]
        print(f"左触觉：{left.name}\n右触觉：{right.name}", flush=True)
        print(f"打开 http://127.0.0.1:{config.get('port', 8891)} · Ctrl+C 停止两路", flush=True)
        os.chdir(root)
        os.execv(str(python), args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"启动失败：{exc}\n配置文件：{config_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
