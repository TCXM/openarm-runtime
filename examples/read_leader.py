"""Read one leader bus; never writes servo registers."""

import argparse
import time

from openarm_leader import LeaderBus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="Stable serial device path")
    args = parser.parse_args()
    try:
        with LeaderBus(args.port) as leader:
            while True:
                sample = leader.sample()
                print("positions:", sample.positions, "volts:", sample.voltages,
                      "alarms:", sample.errors, flush=True)
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
