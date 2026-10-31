"""Gateway integration tests using fake HTTP/WS services, never hardware."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock

from openarm_platform import load_config
from openarm_platform.app import make_server
from openarm_platform.proxy import resolve, rewrite_html


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', 'test-accept')
            self.send_header('Sec-WebSocket-Protocol', self.headers.get('Sec-WebSocket-Protocol', 'test'))
            self.end_headers()
            self.wfile.write(b'\x82\x03abc')
            self.wfile.flush()
            self.connection.settimeout(3)
            try:
                while True:
                    message = self.connection.recv(1024)
                    if not message:
                        break
                    self.connection.sendall(message)
            except OSError:
                pass
            self.close_connection = True
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/next?a=1')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.path == '/chunked':
            self.send_response(200)
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Content-Type', 'image/jpeg')
            self.end_headers()
            self.wfile.write(b'3\r\nabc\r\n3\r\ndef\r\n0\r\n\r\n')
            self.wfile.flush()
            return
        if self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.end_headers()
            self.wfile.write(b'--frame\r\n')
            self.wfile.flush()
            self.server.release_stream.wait(3)
            self.close_connection = True
            return
        if self.path == '/':
            body = b'<html><head><script src="/asset.js"></script></head><body>module</body></html>'
            mime = 'text/html; charset=utf-8'
        else:
            body = json.dumps({'path': self.path, 'host': self.headers['Host']}).encode()
            mime = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET


class PlatformProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream.release_stream = threading.Event()
        self.up_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.up_thread.start()
        config = load_config()
        page = replace(config.pages[0], port=self.upstream.server_port,
                       extra_ports=(self.upstream.server_port,))
        self.gateway = make_server(replace(config, pages=(page,), port=0), Mock())
        self.thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.gateway.server_port

    def tearDown(self):
        self.upstream.release_stream.set()
        self.gateway.shutdown()
        self.gateway.server_close()
        self.thread.join(3)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.up_thread.join(3)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def websocket(self, origin=None):
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=3)
        if origin is None:
            origin = f'http://127.0.0.1:{self.port}'
        sock.sendall((f'GET /apps/control HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
                      'Connection: Upgrade\r\nUpgrade: websocket\r\n'
                      'Sec-WebSocket-Key: test\r\nSec-WebSocket-Version: 13\r\n'
                      f'Origin: {origin}\r\nSec-WebSocket-Protocol: viser-test\r\n\r\n').encode())
        header = bytearray()
        while not header.endswith(b'\r\n\r\n'):
            header.extend(sock.recv(1))
        return sock, bytes(header)

    def test_http_html_and_nested_routes_share_one_origin(self):
        code, headers, body = self.request('GET', '/apps/control/')
        self.assertEqual(code, 200)
        self.assertIn(b'src="/apps/control/asset.js"', body)
        self.assertIn(b'window.fetch=', body)
        self.assertIn(b'/apps/control/_extra/0/', body)
        self.assertEqual(int(headers['Content-Length']), len(body))
        for path in ('/apps/control/status?x=2', '/apps/control/_extra/0/status?x=2'):
            code, _, body = self.request('GET', path)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)['path'], '/status?x=2')
            self.assertEqual(json.loads(body)['host'], f'127.0.0.1:{self.upstream.server_port}')
        self.assertEqual(self.request('GET', '/apps/unknown/')[0], 404)
        self.assertEqual(self.request('GET', '/apps/control-other/')[0], 404)

    def test_redirects_binary_streams_and_head(self):
        code, headers, _ = self.request('GET', '/apps/control?x=1')
        self.assertEqual(code, 307)
        self.assertEqual(headers['Location'], '/apps/control/?x=1')
        code, headers, _ = self.request('GET', '/apps/control/redirect')
        self.assertEqual(code, 302)
        self.assertEqual(headers['Location'], '/apps/control/next?a=1')
        code, headers, body = self.request('GET', '/apps/control/chunked')
        self.assertEqual((code, body), (200, b'abcdef'))
        self.assertNotIn('Transfer-Encoding', headers)
        code, _, body = self.request('HEAD', '/apps/control/')
        self.assertEqual((code, body), (200, b''))

    def test_mutations_need_matching_origin_and_keep_request_body(self):
        path = '/apps/control/settings'
        self.assertEqual(self.request('POST', path, b'data')[0], 403)
        self.assertEqual(self.request('POST', path, b'data', {'Origin': 'https://evil.invalid'})[0], 403)
        code, _, body = self.request('POST', path, b'\x00binary\xff', {'Origin': f'http://127.0.0.1:{self.port}'})
        self.assertEqual((code, body), (200, b'\x00binary\xff'))
        self.assertEqual(self.request('GET', '/apps/control/', headers={'Host': 'evil.invalid'})[0], 403)

    def test_websocket_handshake_binary_and_origin_boundary(self):
        sock, headers = self.websocket()
        with sock:
            self.assertIn(b'101', headers.split(b'\r\n', 1)[0])
            self.assertIn(b'Sec-WebSocket-Protocol: viser-test', headers)
            self.assertEqual(sock.recv(5), b'\x82\x03abc')
            data = b'\x82\x87\x01\x02\x03\x04payload'
            sock.sendall(data)
            self.assertEqual(sock.recv(len(data)), data)
        sock, headers = self.websocket('https://evil.invalid')
        with sock:
            self.assertIn(b'403', headers.split(b'\r\n', 1)[0])

    def test_shutdown_interrupts_live_websocket_and_stream(self):
        sock, _ = self.websocket()
        sock.recv(5)
        stream = socket.create_connection(('127.0.0.1', self.port), timeout=3)
        stream.sendall(f'GET /apps/control/stream HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n'.encode())
        seen = bytearray()
        while b'--frame' not in seen:
            seen.extend(stream.recv(4096))
        self.gateway.shutdown()
        start = time.monotonic()
        self.gateway.server_close()
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(sock.recv(1), b'')
        sock.close()
        stream.close()

    def test_gateway_never_resolves_arbitrary_destinations(self):
        mapping = {'/apps/control': 8893}
        for path in ('http://evil.invalid/apps/control/', '//evil.invalid/apps/control/', '/apps/other/'):
            self.assertIsNone(resolve(path, mapping))
        self.assertEqual(resolve('/apps/control/?u=http://evil.invalid', mapping),
                         ('/apps/control', 8893, '/?u=http://evil.invalid'))
        content = b'<html><head></head><body><a href="//example.com/a">x</a><img src="data:image/png;base64,a"></body></html>'
        rewritten = rewrite_html(content, '/apps/control', mapping)
        self.assertIn(b'href="//example.com/a"', rewritten)
        self.assertIn(b'src="data:image/png;base64,a"', rewritten)


if __name__ == '__main__':
    unittest.main()
