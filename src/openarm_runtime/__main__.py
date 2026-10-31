"""Classic OpenArm: prepare assets, check buses, view feedback, or open user controls."""

import argparse
import json
from pathlib import Path
import signal

from .config import Profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Follower profile for check/view/panel/calibrate or leader --follow")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Download and verify pinned model assets only")
    prepare.add_argument("--asset-root", type=Path, required=True)
    leader = commands.add_parser("leader", help="Leader calibration and preview; optional operator-started physical following")
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
    video = commands.add_parser("video", help="Read RGB/depth cameras in one browser page")
    video.add_argument("--profile", type=Path, help="External JSON camera configuration")
    video.add_argument("--simulate", action="store_true", help="Software images only; never open cameras")
    video.add_argument("--port", type=int, default=5003)
    tactile = commands.add_parser("tactile", help="View Paxini IP-M2324-Core tactile forces")
    source = tactile.add_mutually_exclusive_group(required=True)
    source.add_argument("--device", help="USB serial converter path")
    source.add_argument("--simulate", action="store_true")
    tactile.add_argument("--right-device", help="Second serial converter; show both sensors side by side")
    tactile.add_argument("--port", type=int, default=8891)
    tactile.add_argument("--baudrate", type=int, default=921600)
    tactile.add_argument("--device-id", type=int, default=1, help="Sensor module number + 1 (1..255)")
    platform = commands.add_parser("platform", help="OpenArm Platform: integrate existing control, teleop, tactile and vision pages")
    platform.add_argument("--profile", type=Path, help="External platform service configuration")
    platform.add_argument("--port", type=int, help="Override the platform port (default 8888)")
    platform.add_argument("--attach-only", action="store_true", help="Only display existing services; never start or stop them")
    args = parser.parse_args()
    if args.command == "platform":
        from openarm_platform import load_config
        from openarm_platform.app import serve as serve_platform
        try:
            config = load_config(args.profile)
            if args.port is not None:
                config = config.with_port(args.port)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(str(exc))

        def shutdown_platform(signum, frame):
            raise KeyboardInterrupt(f"Signal {signum}")

        previous = signal.signal(signal.SIGTERM, shutdown_platform)
        try:
            serve_platform(config, attach_only=args.attach_only)
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous)
        return

    if args.command == "tactile":
        if not 1024 <= args.port <= 65535 or args.baudrate <= 0:
            parser.error("Port must be 1024..65535 and baudrate must be positive")
        if not 1 <= args.device_id <= 255:
            parser.error("Device ID must be 1..255")
        if args.right_device:
            if args.simulate or args.port > 65524:
                parser.error("--right-device requires --device and port <= 65524")
            if Path(args.device).resolve() == Path(args.right_device).resolve():
                parser.error("Left and right devices must be different")
            from openarm_tactile.dual import serve as serve_tactile
        else:
            from openarm_tactile.app import serve as serve_tactile

        def shutdown_tactile(signum, frame):
            raise KeyboardInterrupt(f"Signal {signum}")

        previous = signal.signal(signal.SIGTERM, shutdown_tactile)
        try:
            serve_tactile(args.device, simulate=args.simulate, port=args.port,
                          baudrate=args.baudrate, device_id=args.device_id,
                          **({"right_device": args.right_device} if args.right_device else {}))
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous)
        return
    if args.command == "video":
        if not 1024 <= args.port <= 65535:
            parser.error("Port must be 1024..65535")
        if args.profile is None and not args.simulate:
            parser.error("video requires --profile or explicit --simulate")
        from openarm_video import VideoCapture, load_cameras, serve as serve_video
        from openarm_video.profile import simulation_cameras
        try:
            configs = load_cameras(args.profile) if args.profile else simulation_cameras()
            capture = VideoCapture(configs, simulate=args.simulate)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(str(exc))

        def shutdown_video(signum, frame):
            raise KeyboardInterrupt(f"Signal {signum}")

        previous = signal.signal(signal.SIGTERM, shutdown_video)
        try:
            serve_video(capture, port=args.port)
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous)
        return
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
