"""Optional actual LeRobot roundtrip; run in the separate pinned export environment."""
import importlib.util
import tempfile
from pathlib import Path
import unittest

from test_collection import fixture


@unittest.skipUnless(importlib.util.find_spec("lerobot"), "Separate LeRobot export environment required")
class LeRobotCollectionTests(unittest.TestCase):
    def test_two_episodes_roundtrip_images_tactile_actions_and_provenance(self):
        import json
        import numpy as np
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        from openarm_collection.export import export_lerobot
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            first, second = fixture(root, images=True), fixture(root, images=True)
            output = root / "dataset"
            export_lerobot([first, second], output, "local/test", fps=20)
            dataset = LeRobotDataset("local/test", root=output, video_backend="pyav")
            self.assertEqual(dataset.num_episodes, 2)
            frame = dataset[0]
            np.testing.assert_allclose(frame["action"] - frame["observation.state"], [10] * 8)
            self.assertEqual(tuple(frame["observation.images.head"].shape), (3, 2, 3))
            self.assertEqual(tuple(frame["observation.tactile.left_pad"].shape), (69, 3))
            provenance = json.loads((output / "openarm-export.json").read_text())
            self.assertEqual(len(provenance["episodes"]), 2)
            self.assertEqual(provenance["episodes"][0]["review"]["outcome"], "success")
