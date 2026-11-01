"""Collection UI construction with archive fixtures and no device services."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

from test_collection import fixture


@unittest.skipUnless(importlib.util.find_spec('nicegui'),'Requires platform extra')
class CollectionViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_state_and_archive_can_render_without_video_dependencies(self):
        from nicegui import Client, ui
        from openarm_platform.collection_view import CollectionView
        with tempfile.TemporaryDirectory() as directory:
            path=fixture(Path(directory))
            client=Client(ui.page('/collection-test'))
            try:
                with client:
                    view=CollectionView(directory)
                    await view.refresh_live()
                    self.assertEqual(view.metrics['state'].text,'未连接')
                    await view.refresh_episodes()
                    view.episode.value=path.name
                    await view.load()
                    self.assertFalse(view.error.text,view.error.text)
                    self.assertEqual(len(view.joint_table.rows),8)
                    self.assertIn('已验收',view.summary.text)
                    self.assertNotIn('待验收',view.summary.text)
                    self.assertIn('已验收',view.episode.options[path.name])
                    self.assertTrue(view.play_button.enabled)
            finally:
                client.delete()
