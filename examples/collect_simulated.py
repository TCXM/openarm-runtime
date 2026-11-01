"""Offline multimodal acquisition demo, or browser operator rehearsal. No devices."""
import argparse
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from openarm_collection import Collection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=2)
    parser.add_argument("--ui", action="store_true", help="Rehearse operator controls with Viser")
    parser.add_argument("--port", type=int, default=8894)
    args = parser.parse_args()
    import numpy as np
    collection = Collection(args.root)
    collection.context = dict(simulation=True, sides=["right"], task="simulation",
                              joint_order=[f"right/j{i}" for i in range(1, 9)])
    collection.register("robot/right", encoding="mit_exchange")
    collection.register("leader/right", encoding="leader_registers")
    collection.register("intent/right", encoding="mapped_targets")
    collection.register("camera/head", encoding="bgr8", shape=[48, 64, 3])
    collection.register("tactile/left_pad", encoding="xyz_counts", shape=[69, 3],
                        identity={"serial": "SIM", "mount": "left_gripper"})
    stop, ready = threading.Event(), threading.Event()

    def generate():
        index = 0
        while not stop.is_set():
            now = time.monotonic()
            q = .05 * np.sin(index / 20)
            states = {j: dict(position=float(q), velocity=0., torque=0., received_at=now - .005)
                      for j in range(1, 9)}
            collection.emit("robot/right", now, dict(
                targets_rad={j: float(q + .01) for j in range(1, 9)}, observation_before=states, feedback=states))
            collection.emit("leader/right", now, dict(positions=[2048] * 8))
            collection.emit("intent/right", now, dict(targets_rad=[float(q + .01)] * 8))
            image = np.full((48, 64, 3), [index % 255, 90, 180], dtype=np.uint8)
            collection.emit("camera/head", now, image)
            collection.previews["camera/head"] = (SimpleNamespace(image=image), index)
            collection.emit("tactile/left_pad", now, [[0., 0., float(index % 20)]] * 69)
            index += 1
            ready.set()
            stop.wait(.05)

    worker = threading.Thread(target=generate, daemon=True)
    worker.start()
    server = None
    try:
        if not ready.wait(3):
            raise RuntimeError("Simulation failed to start")
        collection.ready = True
        if args.ui:
            import viser
            from openarm_collection.ui import attach
            server = viser.ViserServer(host="127.0.0.1", port=args.port, label="OpenArm 模拟数采")
            refresh = attach(server, SimpleNamespace(owner=None), collection)
            while True:
                refresh()
                time.sleep(.25)
        else:
            collection.start("synthetic reach", "simulation")
            stop.wait(args.duration)
            path = collection.finish()
            collection.review("success")
            print(path)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(3)
        collection.close()
        if server:
            server.stop()


if __name__ == "__main__":
    main()
