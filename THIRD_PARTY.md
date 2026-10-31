# Third-party dependencies and reference material

- OpenArm CAN1.4.1: `https://github.com/enactic/openarm_can`, pinned revision
  `bfd4e52194aefe38389fa6e670be3738136facb9`. Used as a dependency, not vendored.
- Feetech Servo SDK1.0.0: `https://pypi.org/project/feetech-servo-sdk/`;
  PySerial3.5: `https://pypi.org/project/pyserial/`. Installed only with leader extra.
- Viser1.1.1, yourdfpy and pycollada are optional visualization dependencies.
- Official OpenArm description assets are downloaded into a caller-supplied cache
  and hash-verified; the asset preparation workflow retains upstream LICENSE.txt.
- Follower gains and leader direction/J6–J7 mapping were checked against the user's
  LeRobot/OpenArm reference integration. That imported reference tree is not copied
  into this repository. Protocol facts and retained validation are documented in
  docs/validation.md.

Upstream packages/assets retain their own licenses; the MIT license in this
repository covers this project's own source, not those separate dependencies.
