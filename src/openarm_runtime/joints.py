"""Shared Classic v2 joint conventions; values are motor-position degrees."""

# LeRobot OpenArm follower side-specific bounds, configuration commit
# 408e0ca763658f9294ef816d2b07f7d281ddeeb0. Stock left gripper motor zero
# opens positive; the right gripper opens negative. Zero is closed.
JOINT_LIMITS = {
    "left": ((-75, 75), (-90, 9), (-85, 85), (0, 135), (-85, 85), (-40, 40), (-80, 80), (0, 65)),
    "right": ((-75, 75), (-9, 90), (-85, 85), (0, 135), (-85, 85), (-40, 40), (-80, 80), (-65, 0)),
}
