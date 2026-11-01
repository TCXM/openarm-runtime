"""GUI-independent whole-arm lifecycle built from the validated controller."""

from contextlib import contextmanager

from .arm import ArmHold
from .config import bus_lease
from .telemetry import IDS
from .transport import ClassicBus
from .gravity import prepare_gravity


def _ignore(_event):
    pass


def _running():
    return False


def check_modes(bus, allow_unreadable=False, cancelled=_running):
    """Read control modes; an explicit non-MIT value is always rejected."""
    modes = {}
    for mid in IDS:
        if cancelled():
            raise InterruptedError("User stop")
        try:
            mode = bus.parameter(mid, 10)
        except TimeoutError:
            if not allow_unreadable:
                raise RuntimeError(f"ID{mid}: RID10 unreadable; explicit MIT trial required") from None
            mode = None
        if mode not in (None, 1):
            raise RuntimeError(f"ID{mid}: mode={mode}, expected MIT; not enabled")
        modes[mid] = mode
    return modes


@contextmanager
def hold_arm(profile, side, *, allow_unreadable_mode=False, cancelled=_running,
             record=_ignore, publish=_ignore):
    """Acquire bus, preflight, enable/hold, then disable and close on exit.

    Entering this context enables the physical arm. Stop any reader using this bus
    first. Call the yielded ArmHold.tick() from one thread at profile.rate_hz, using
    offsets in degrees relative to the freshly captured enable pose. There is no
    background loop here: the caller owns its schedule and stop signal.
    """
    if not profile.mapping_verified:
        raise ValueError("Confirm physical left/right mapping before control")
    interface = profile.interface(side)
    if cancelled():
        raise InterruptedError("User stop")
    with bus_lease(interface, exclusive=True):
        bus = ClassicBus(interface, profile.feedback_timeout_s)
        try:
            preflight = bus.preflight()
            record({"event": "preflight", "motors": preflight})
            gravity = prepare_gravity(profile, side, bus, preflight)
            modes = check_modes(bus, allow_unreadable_mode, cancelled)
            record({"event": "modes", "readback": modes})
            with ArmHold(bus, cancelled, record, publish, gravity=gravity) as arm:
                yield arm
        finally:
            bus.close()
