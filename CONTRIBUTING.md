# Contributing and releasing

Keep the package small: transport, control lifecycle, mapping and optional UI
have separate responsibilities. Add features when a real consumer needs them.
New optional features should not add heavy dependencies to core imports.

## Development

Use Python3.12 and uv. `uv sync --locked` installs an editable core package;
`uv run --locked python -m unittest discover -s tests` runs offline regressions.
For UI development, add `--extra viewer --extra leader` to `uv sync`.
Native follower setup is documented in [workstation setup](docs/workstation.md).

When developing the runtime together with another application, install this
checkout into that application's environment in editable mode. The checkout can
live anywhere; no sibling-directory layout is required. See the README's
[installation modes](README.md#using-the-package-vs-developing-it) for pip and
uv commands, when to reinstall, and how to return to a pinned release.

Tests must not open physical hardware. Test behavior at transport boundaries,
including exception cleanup, rather than copying implementation details.
Record actual hardware checks separately in [validation](docs/validation.md).
Keep device configuration, calibration, logs and cached assets outside Git.

## API changes

Keep examples and [the API guide](docs/api.md) aligned with public exports.
Use patch versions for compatible fixes and minor versions for breaking0.x API
changes. Document migrations in the changelog. Pin releases in consuming projects;
an editable install is for joint development, not a reproducible deployment.

## Release checklist

1. Update the version in `pyproject.toml`, `src/openarm_runtime/__init__.py` and
   version assertions in `tests/test_public_api.py`; update the changelog and
   README install tag. Run `uv lock` if package metadata/dependencies changed.
2. Run tests and `uv build`. Install the wheel in a fresh environment and run the
   tests with `OPENARM_EXPECT_INSTALLED=1`. CI performs this on Linux and macOS.
3. Commit, push and verify the CI result. Tag the tested commit `vX.Y.Z` and push
   that tag. The release workflow validates the tag/version, rebuilds and tests
   the wheel, then publishes a GitHub release with the changelog, wheel, source
   distribution and SHA256 checksums from that same commit.
4. Update downstream pins explicitly after acceptance. A release does not require
   restarting a user's live hardware services.

Releases are on GitHub. The follower extra currently contains a pinned direct Git
dependency, so a future PyPI publication needs a separate packaging review.
