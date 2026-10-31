"""Read eight follower motors on an already-configured Classic CAN bus."""

import argparse
import time

from openarm_runtime import ArmConnection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True, help="SocketCAN interface, e.g. can0")
    args = parser.parse_args()
    reader = ArmConnection(args.interface, "classic")
    try:
        reader.restart()
        while True:
            states, error, message, replies, missed = reader.shared.snapshot()
            fresh = {mid: state.position for mid, state in states.items()
                     if state.fresh(time.monotonic(), 0.4)}
            print("positions (rad):", fresh, "error:", error,
                  "replies/missed:", (replies, missed), flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()


if __name__ == "__main__":
    main()
