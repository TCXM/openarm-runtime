"""Read-only, bounded dual-bus verification shared by the command-line entry."""

from .config import bus_lease
from .transport import ClassicBus


def check(profile):
    result = {}
    for side in ("left", "right"):
        interface = profile.interface(side)
        result[side] = {"interface": interface, "passed": False}
        try:
            with bus_lease(interface, exclusive=False):
                bus = ClassicBus(interface, profile.feedback_timeout_s)
                try:
                    result[side]["motors"] = bus.preflight()
                    bus.prepare_encoder(range(1, 9))  # Encode only; never send controls.
                    result[side].update(sdk_version=bus.sdk.__version__, passed=True)
                finally:
                    bus.close()
        except Exception as exc:
            result[side]["error"] = f"{type(exc).__name__}: {exc}"
    return {"passed": all(bus["passed"] for bus in result.values()),
            "mapping_verified": profile.mapping_verified, "buses": result}
