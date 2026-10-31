"""Print latest video frame metadata; explicit simulation is available offline."""
import argparse
import time
from openarm_video import VideoCapture, load_cameras
from openarm_video.profile import simulation_cameras


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()
    if not args.profile and not args.simulate:
        parser.error("Pass --profile or --simulate")
    configs = load_cameras(args.profile) if args.profile else simulation_cameras()
    try:
        with VideoCapture(configs, simulate=args.simulate) as capture:
            while True:
                for config in configs:
                    frame, error, count = capture.snapshot(config.name)
                    if frame is not None and not error and frame.fresh(time.monotonic()):
                        print(config.name, count, frame.pixel_format, frame.image.shape)
                    else:
                        print(config.name, error or "Waiting for a fresh frame")
                time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
