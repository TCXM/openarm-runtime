"""Classic OpenArm: prepare assets, check buses, view feedback, or open user controls."""

import argparse
import json
from pathlib import Path
import signal

from .config import Profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Follower profile for check/view/panel")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Download and verify pinned model assets only")
    prepare.add_argument("--asset-root", type=Path, required=True)
    leader = commands.add_parser("leader", help="Read physical leaders and drive virtual arms only")
    leader.add_argument("--left-port", required=True)
    leader.add_argument("--right-port", required=True)
    leader.add_argument("--calibration", type=Path, required=True)
    leader.add_argument("--asset-root", type=Path, required=True)
    leader.add_argument("--port", type=int, default=8890)
    commands.add_parser("check", help="Read-only: verify both Classic buses and disabled motors")
    for name, help_text in (("view", "Read-only live viewer"),
                            ("panel", "User-operated whole-arm control; startup is read-only")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--asset-root", type=Path, required=True)
        command.add_argument("--port", type=int, default=8890)
        if name == "panel":
            command.add_argument("--report-root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        from openarm_viewer.model import prepare
        prepare(args.asset_root)
        return
    if args.command == "leader":
        if not 1024 <= args.port <= 65535:
            parser.error("Port must be 1024..65535")
        from openarm_leader.app import serve as serve_leader

        def shutdown_leader(signum, frame):
            raise KeyboardInterrupt(f"Signal {signum}")

        previous = signal.signal(signal.SIGTERM, shutdown_leader)
        try:
            serve_leader(args.asset_root, args.left_port, args.right_port, args.calibration, args.port)
        finally:
            signal.signal(signal.SIGTERM, previous)
        return
    if args.config is None:
        parser.error("--config is required for check, view and panel")
    profile = Profile.load(args.config)
    if args.command == "check":
        from .checks import check
        result = check(profile)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["passed"] else 1)
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be 1024..65535")
    from openarm_viewer.app import serve
    options = ({"control_profile": profile, "report_root": args.report_root}
               if args.command == "panel" else {})

    def shutdown(signum, frame):
        raise KeyboardInterrupt(f"Signal {signum}")

    previous = signal.signal(signal.SIGTERM, shutdown)
    try:
        serve(args.asset_root, profile.left, profile.right, args.port, "classic", **options)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
