"""Loopback HTTP shell and lifecycle for unchanged OpenArm web applications."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import socket
import threading
from urllib.parse import urlsplit

from .services import ServiceManager
from .proxy import proxy, resolve, routes


class PlatformServer(ThreadingHTTPServer):
    """Close live streams before joining handlers and cleaning up services."""
    daemon_threads = False

    def __init__(self, *args, **kwargs):
        self.stopping = threading.Event()
        self._sockets = set()
        self._sockets_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def track_socket(self, connection):
        with self._sockets_lock:
            if self.stopping.is_set():
                connection.close()
                raise OSError('Gateway is closing')
            self._sockets.add(connection)

    def untrack_socket(self, connection):
        with self._sockets_lock:
            self._sockets.discard(connection)

    def server_close(self):
        self.stopping.set()
        with self._sockets_lock:
            connections = list(self._sockets)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        super().server_close()


def make_server(config, manager, *, attach_only=False):
    assets = files('openarm_platform').joinpath('static')
    mapping = routes(config)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(45)
            self.server.track_socket(self.connection)

        def finish(self):
            try:
                super().finish()
            finally:
                self.server.untrack_socket(self.connection)

        def log_message(self, *_):
            pass

        def send(self, status, content, content_type='application/json; charset=utf-8', *, head=False):
            if not isinstance(content, bytes):
                content = json.dumps(content, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            self.end_headers()
            if not head:
                self.wfile.write(content)

        def valid_host(self):
            try:
                return urlsplit('http://' + self.headers.get('Host', '')).hostname in ('127.0.0.1', 'localhost', '::1')
            except ValueError:
                return False

        def do_GET(self, head=False):
            if not self.valid_host():
                self.send(403, {'error': 'Use a localhost SSH forward'}, head=head)
                return
            route = resolve(self.path, mapping)
            if route is not None:
                proxy(self, route, mapping, head=head)
                return
            path = urlsplit(self.path).path
            if path == '/api/config':
                self.send(200, dict(title=config.title, port=config.port, attach_only=attach_only,
                                    pages=[p.public() for p in config.pages]), head=head)
            elif path == '/api/status':
                self.send(200, {'services': manager.status()}, head=head)
            elif path in ('/', '/index.html', '/static/platform.css', '/static/platform.js'):
                name, mime = {'/': ('index.html', 'text/html'), '/index.html': ('index.html', 'text/html'),
                              '/static/platform.css': ('platform.css', 'text/css'),
                              '/static/platform.js': ('platform.js', 'text/javascript')}[path]
                self.send(200, assets.joinpath(name).read_bytes(), mime + '; charset=utf-8', head=head)
            else:
                self.send(404, {'error': 'Not found'}, head=head)

        def do_HEAD(self):
            self.do_GET(head=True)

        def do_POST(self):
            route = resolve(self.path, mapping)
            if route is not None and self.valid_host():
                proxy(self, route, mapping)
                return
            if (not self.valid_host() or self.headers.get('Origin') != 'http://' + self.headers.get('Host', '')
                    or self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json'):
                self.send(403, {'error': 'Same-origin JSON request required'})
                return
            if urlsplit(self.path).path != '/api/select':
                self.send(404, {'error': 'Not found'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024:
                    raise ValueError('Invalid request size')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or set(data) != {'id'} or not isinstance(data['id'], str) or data['id'] not in {p.id for p in config.pages}:
                    raise ValueError('Select a configured page')
                if not attach_only:
                    manager.activate(data['id'])
                self.send(200, {'services': manager.status()})
            except (ValueError, RuntimeError, OSError) as exc:
                self.send(409, {'error': str(exc)})

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

    server = PlatformServer(('127.0.0.1', config.port), Handler)
    server.timeout = 0.25
    # Selection callbacks finish before manager cleanup, to avoid starting a child after close.
    server.daemon_threads = False
    return server


def serve(config, *, attach_only=False):
    manager = ServiceManager(config.services, config.log_dir)
    with make_server(config, manager, attach_only=attach_only) as server:
        try:
            if not attach_only:
                manager.start()
            print(f'OpenArm Platform: http://127.0.0.1:{config.port}', flush=True)
            print(f'服务日志：{config.log_dir} · Ctrl+C 关闭本次启动的服务', flush=True)
            while True:
                server.handle_request()
        finally:
            # Wait for in-flight selections before closing children.
            server.server_close()
            manager.close()
