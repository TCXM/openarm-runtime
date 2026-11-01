#!/usr/bin/env python3
"""Run the local OpenArm Platform with the checkout environment."""
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parent
    python = root / '.venv' / 'bin' / 'python'
    profile = Path.home() / '.config' / 'openarm-runtime' / 'platform.json'
    if not python.is_file() or not profile.is_file():
        print(f'需要项目 .venv 和平台配置：{profile}\n参见 docs/platform.md', file=sys.stderr)
        return 1
    os.chdir(root)
    os.execv(str(python), [str(python), '-m', 'openarm_runtime', 'platform',
                          '--profile', str(profile), *sys.argv[1:]])


if __name__ == '__main__':
    raise SystemExit(main())
