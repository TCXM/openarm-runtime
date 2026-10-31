"""Public, hardware-passive API for the OpenArm runtime."""

from .config import Profile
from .control import hold_arm
from .telemetry import MotorRange, MotorState, ArmConnection

__version__ = "0.1.0"
__all__ = ["Profile", "hold_arm", "MotorRange", "MotorState", "ArmConnection"]
