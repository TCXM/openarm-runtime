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
    leader.add_argument("--follow", action="store_true", help="Add operator-started physical follower controls")
    leader.add_argument("--report-root", type=Path)
    commands.add_parser("check", help="Read-only: verify both Classic buses and disabled motors")
    calibration = commands.add_parser("calibrate", help="Move one v2 arm, find stops and write hardware zeros (experimental)")
    calibration.add_argument("--side", choices=("left", "right"), required=True)
    calibration.add_argument("--report-root", type=Path, required=True)
    calibration.add_argument("--allow-unreadable-mode", action="store_true")
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
        if args.follow and (args.config is None or args.report_root is None):
            parser.error("leader --follow requires --config and --report-root")
        if not 1024 <= args.port <= 65535:
            parser.error("Port must be 1024..65535")
        from openarm_leader.app import serve as serve_leader

        def shutdown_leader(signum, frame):
            raise KeyboardInterrupt(f"Signal {signum}")

        previous = signal.signal(signal.SIGTERM, shutdown_leader)
        try:
            options = {"control_profile": Profile.load(args.config), "report_root": args.report_root} if args.follow else {}
            serve_leader(args.asset_root, args.left_port, args.right_port, args.calibration, args.port, **options)
        finally:
            signal.signal(signal.SIGTERM, previous)
        return
    if args.config is None:
        parser.error("--config is required for check, view, panel and calibrate")
    profile = Profile.load(args.config)
    if args.command == "calibrate":
        from datetime import datetime, timezone
        import threading
        import uuid
        from .calibration import calibrate_arm
        from .report import Report
        cancel = threading.Event()
        previous = {sig: signal.signal(sig, lambda *_: cancel.set())
                    for sig in (signal.SIGINT, signal.SIGTERM)}
        report = None
        try:
            report = Report(args.report_root / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex+".json"),
                {"source": "cli_user", "action": "auto_calibrate", "side": args.side,
                 "interface": profile.interface(args.side),
                 "allow_unreadable_mode": args.allow_unreadable_mode})
            print("Automatic v2 calibration: motion and zero writes. Ctrl+C stops. Report:", report.path, flush=True)
            calibrate_arm(profile, args.side, cancelled=cancel.is_set, record=report.record,
                          progress=lambda message: print(message, flush=True),
                          allow_unreadable_mode=args.allow_unreadable_mode)
        except BaseException as exc:
            if report:
                report.record({"event": "ended", "error": f"{type(exc).__name__}: {exc}"})
            raise
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return
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
