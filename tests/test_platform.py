"""Configuration and HTTP boundaries for the platform, with no hardware services."""
from dataclasses import replace
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from openarm_platform import load_config
from openarm_platform.app import make_server


class PlatformTests(unittest.TestCase):
    def profile(self, directory, data):
        path = Path(directory) / 'platform.json'
        path.write_text(json.dumps(data))
        return load_config(path)

    def test_configuration_keeps_commands_private_and_expands_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.profile(directory, {'services': {'teleop': {
                'command': ['{python}', '-m', 'openarm_runtime', '~/private-config.json'],
                'cwd': 'workspace'}}})
            teleop = config.pages[1]
            self.assertEqual(teleop.command[0], sys.executable)
            self.assertEqual(teleop.command[-1], str(Path.home() / 'private-config.json'))
            self.assertEqual(teleop.cwd, (Path(directory) / 'workspace').resolve())
            self.assertEqual(teleop.exclusive_group, 'motion')
            self.assertNotIn('command', teleop.public())
            self.assertNotIn('cwd', teleop.public())
            self.assertEqual(config.pages[2].extra_ports, (8901, 8902))

    def test_rejects_shell_commands_and_port_collisions(self):
        cases = (
            {'services': {'control': {'command': 'echo bad'}}},
            {'services': {'control': {'command': ['']}}},
            {'services': {'control': {'port': True}}},
            {'services': {'typo': {}}},
            {'port': 8891},
            {'services': {'vision': {'port': 8901}}},
            {'services': {'tactile': {'port': 65530}}},
            {'services': {'control': {'cwd': 1}}},
            {'support': {'../outside': {'port': 12000}}},
        )
        with tempfile.TemporaryDirectory() as directory:
            for data in cases:
                with self.subTest(data=data), self.assertRaises(ValueError):
                    self.profile(directory, data)

    def test_imports_and_cli_help_are_passive(self):
        script = """
import sys
from unittest.mock import patch
with patch('socket.socket', side_effect=AssertionError('socket')), patch('subprocess.Popen', side_effect=AssertionError('process')):
    from openarm_platform import load_config
    import openarm_platform.app
    config = load_config()
assert not any(x in sys.modules for x in ('viser', 'serial', 'openarm_can', 'paxini_sdk', 'flask'))
"""
        subprocess.run([sys.executable, '-I', '-c', script], cwd='/tmp', check=True)
        result = subprocess.run([sys.executable, '-I', '-m', 'openarm_runtime', 'platform', '--help'],
                                cwd='/tmp', check=True, capture_output=True, text=True)
        self.assertIn('--attach-only', result.stdout)

    def test_http_assets_status_and_selection_boundary(self):
        manager = Mock()
        manager.status.return_value = {'control': {'state': 'external', 'reachable': True}}
        config = replace(load_config(), port=0)
        server = make_server(config, manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port

        def request(method, path, data=None, origin=None):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            headers = {}
            if data is not None:
                headers['Content-Type'] = 'application/json'
            if origin:
                headers['Origin'] = origin
            connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
            response = connection.getresponse()
            result = response.status, response.read()
            connection.close()
            return result

        try:
            code, body = request('GET', '/')
            self.assertEqual(code, 200)
            self.assertIn(b'OpenArm Platform', body)
            for path in ('/static/platform.js', '/static/platform.css'):
                self.assertEqual(request('GET', path)[0], 200)
            self.assertEqual(request('GET', '/../../pyproject.toml')[0], 404)
            code, body = request('GET', '/api/config')
            self.assertEqual(len(json.loads(body)['pages']), 4)
            self.assertNotIn('command', body.decode())
            self.assertEqual(request('GET', '/api/status')[0], 200)
            self.assertEqual(request('POST', '/api/select', {'id': 'control'}, 'https://evil.invalid')[0], 403)
            manager.activate.assert_not_called()
            self.assertEqual(request('POST', '/api/select', {'id': 'control'}, f'http://127.0.0.1:{port}')[0], 200)
            manager.activate.assert_called_once_with('control')
            manager.activate.side_effect = ValueError('external controller is active')
            code, body = request('POST', '/api/select', {'id': 'teleop'}, f'http://127.0.0.1:{port}')
            self.assertEqual(code, 409)
            self.assertIn(b'external controller', body)
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()

    def test_attach_only_never_activates_a_process(self):
        manager = Mock()
        manager.status.return_value = {}
        server = make_server(replace(load_config(), port=0), manager, attach_only=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('POST', '/api/select', body='{"id":"control"}', headers={
                'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{server.server_port}'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            manager.activate.assert_not_called()
            manager.start.assert_not_called()
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()
