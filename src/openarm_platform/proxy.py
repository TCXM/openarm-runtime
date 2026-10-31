"""Fixed-route loopback HTTP/WebSocket gateway for the existing module pages.

No arbitrary destination can be supplied by a browser. Application connections
keep their original lifetimes; stopping the gateway closes every proxy socket.
"""
import http.client
import json
import re
import select
import socket
from urllib.parse import urlsplit

HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade'}
MAX_BODY = 16 * 1024 * 1024


def routes(config):
    result = {}
    for page in config.pages:
        prefix = f'/apps/{page.id}'
        result[prefix] = page.port
        for index, port in enumerate(page.extra_ports):
            result[f'{prefix}/_extra/{index}'] = port
    return result


def resolve(path, mapping):
    """Match the most specific configured prefix, preserving the raw query."""
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        return None
    for prefix in sorted(mapping, key=len, reverse=True):
        if parsed.path == prefix or parsed.path.startswith(prefix + '/'):
            suffix = parsed.path[len(prefix):] or '/'
            if parsed.query:
                suffix += '?' + parsed.query
            return prefix, mapping[prefix], suffix
    return None


def bridge(prefix, mapping):
    # Keep legacy camera fetches/downloads and the tactile page's two iframe
    # setters on this origin. Viser's worker already receives the full prefixed
    # page URL, so its binary websocket protocol remains completely untouched.
    ports = {str(port): path + '/' for path, port in mapping.items()}
    return r'''<script>(()=>{'use strict';
const base=BASE,ports=PORTS;
const local=new Set(['127.0.0.1','localhost','[::1]']);
function route(value){
 if(typeof value!=='string'&&!(value instanceof URL))return value;
 const raw=String(value);if(!/^(?:https?:|wss?:|\/)/.test(raw))return value;
 let u;try{u=new URL(raw,location.href);}catch(_){return value;}
 if(!['http:','https:','ws:','wss:'].includes(u.protocol))return value;
 const same=u.host===location.host;
 const path=local.has(u.hostname)&&ports[u.port];
 if(!same&&!path)return value;
 if(same&&(u.pathname===base.slice(0,-1)||u.pathname.startsWith('/apps/')))return value;
 u.host=location.host;u.pathname=(same?base:path)+u.pathname.replace(/^\//,'');
 return u.href;
}
const fetch0=window.fetch;window.fetch=function(input,init){
 if(input instanceof Request){const url=route(input.url);if(url!==input.url)input=new Request(url,input);}
 else input=route(input);return fetch0.call(this,input,init);
};
const open=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(method,url,...rest){return open.call(this,method,route(url),...rest);};
for(const [type,key] of [[HTMLIFrameElement,'src'],[HTMLImageElement,'src'],[HTMLScriptElement,'src'],[HTMLLinkElement,'href'],[HTMLAnchorElement,'href'],[HTMLFormElement,'action']]){
 const descriptor=Object.getOwnPropertyDescriptor(type.prototype,key);if(!descriptor||!descriptor.set)continue;
 Object.defineProperty(type.prototype,key,{...descriptor,set(value){descriptor.set.call(this,route(value));}});
}
const set=Element.prototype.setAttribute;Element.prototype.setAttribute=function(key,value){
 if(['src','href','action','poster'].includes(String(key).toLowerCase()))value=route(value);
 return set.call(this,key,value);
};
const ws=window.WebSocket;window.WebSocket=new Proxy(ws,{construct(target,args){args[0]=route(args[0]);return Reflect.construct(target,args);}});
})();</script>'''.replace('BASE', json.dumps(prefix + '/')).replace('PORTS', json.dumps(ports))


def rewrite_html(content, prefix, mapping):
    document = content.decode('utf-8')
    # Parser-created URLs bypass JS property setters. Rewrite only URL-valued
    # attributes, never embedded Viser bundles, data URLs or application text.
    document = re.sub(r'''(\b(?:src|href|action|poster)\s*=\s*["'])(/(?!/|apps/))''',
                      lambda match: match[1] + prefix + '/', document, flags=re.I)
    injected = bridge(prefix, mapping)
    head = re.search(r'<head(?:\s[^>]*)?>', document, re.I)
    if head:
        document = document[:head.end()] + injected + document[head.end():]
    else:
        document = injected + document
    return document.encode('utf-8')


def rewrite_location(value, prefix, port):
    parsed = urlsplit(value)
    if value.startswith('/') and not value.startswith('//'):
        return prefix + value
    if parsed.hostname in ('localhost', '127.0.0.1', '::1') and parsed.port == port:
        return prefix + (parsed.path or '/') + ('?' + parsed.query if parsed.query else '') + ('#' + parsed.fragment if parsed.fragment else '')
    return value


def _headers(handler, port, *, websocket=False):
    connection_tokens = {x.strip().lower() for x in handler.headers.get('Connection', '').split(',')}
    omitted = HOP_HEADERS | connection_tokens | {'host', 'accept-encoding', 'content-length', 'origin'}
    if websocket:
        omitted -= {'upgrade'}
    headers = {key: value for key, value in handler.headers.items() if key.lower() not in omitted}
    headers['Host'] = f'127.0.0.1:{port}'
    headers['Connection'] = 'Upgrade' if websocket else 'close'
    headers['Accept-Encoding'] = 'identity'
    # The gateway has checked the external Origin before forwarding mutations.
    if handler.headers.get('Origin') is not None:
        headers['Origin'] = f'http://127.0.0.1:{port}'
    return headers


def proxy(handler, route, mapping, *, head=False):
    prefix, port, target = route
    websocket = handler.headers.get('Upgrade', '').lower() == 'websocket'
    if websocket or handler.command not in ('GET', 'HEAD'):
        if handler.headers.get('Origin') != 'http://' + handler.headers.get('Host', ''):
            handler.send(403, {'error': 'Same-origin request required'}, head=head)
            return
    if handler.headers.get('Transfer-Encoding') or len(handler.headers.get_all('Content-Length', [])) > 1:
        handler.send(400, {'error': 'Ambiguous request framing'}, head=head)
        return
    try:
        length = int(handler.headers.get('Content-Length', '0'))
        if not 0 <= length <= MAX_BODY:
            raise ValueError('Invalid request size')
    except ValueError:
        handler.send(413, {'error': 'Invalid request size'}, head=head)
        return
    if websocket:
        if handler.command != 'GET' or length:
            handler.send(400, {'error': 'Invalid WebSocket upgrade'}, head=head)
            return
        _websocket(handler, port, target)
        return
    # Relative asset paths need a trailing slash on the module document URL.
    if urlsplit(handler.path).path == prefix and handler.command in ('GET', 'HEAD'):
        handler.send_response(307)
        query = urlsplit(handler.path).query
        handler.send_header('Location', prefix + '/' + ('?' + query if query else ''))
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    started = False
    upstream_socket = None
    try:
        body = handler.rfile.read(length) if length else None
        if length and len(body) != length:
            handler.send(400, {'error': 'Incomplete request body'}, head=head)
            return
        connection.connect()
        upstream_socket = connection.sock
        handler.server.track_socket(upstream_socket)
        connection.request(handler.command, target, body, _headers(handler, port))
        response = connection.getresponse()
        html = not head and response.getheader('Content-Type', '').split(';')[0].strip() == 'text/html'
        content = None
        if html:
            content = response.read(MAX_BODY + 1)
            if len(content) > MAX_BODY:
                raise ValueError('Upstream HTML exceeds gateway limit')
            if response.getheader('Content-Encoding', 'identity') != 'identity':
                raise ValueError('Upstream ignored identity content encoding')
            content = rewrite_html(content, prefix, mapping)
        handler.send_response(response.status, response.reason)
        omitted = HOP_HEADERS | {'content-length', 'server', 'date', 'x-frame-options'}
        omitted |= {x.strip().lower() for x in response.getheader('Connection', '').split(',')}
        if html:
            omitted |= {'content-encoding', 'etag', 'content-md5', 'content-security-policy'}
        for key, value in response.getheaders():
            if key.lower() in omitted:
                continue
            if key.lower() == 'location':
                value = rewrite_location(value, prefix, port)
            if key.lower() == 'set-cookie':
                value = re.sub(r'(?i)(;\s*path=)/', lambda match: match[1] + prefix + '/', value)
            handler.send_header(key, value)
        if content is not None:
            handler.send_header('Content-Length', str(len(content)))
        elif response.getheader('Content-Length') is not None:
            handler.send_header('Content-Length', response.getheader('Content-Length'))
        handler.send_header('X-Frame-Options', 'SAMEORIGIN')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.close_connection = True
        started = True
        if head:
            return
        if content is not None:
            handler.wfile.write(content)
        else:
            while not handler.server.stopping.is_set():
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
    except (OSError, ValueError, http.client.HTTPException, UnicodeError):
        if not started and not handler.server.stopping.is_set():
            handler.send(502, {'error': 'Module service unavailable; check its platform status'}, head=head)
    finally:
        connection.close()
        if upstream_socket is not None:
            handler.server.untrack_socket(upstream_socket)


def _websocket(handler, port, target):
    upstream = None
    started = False
    try:
        upstream = socket.create_connection(('127.0.0.1', port), timeout=10)
        handler.server.track_socket(upstream)
        headers = _headers(handler, port, websocket=True)
        request = f'GET {target} HTTP/1.1\r\n' + ''.join(f'{key}: {value}\r\n' for key, value in headers.items()) + '\r\n'
        upstream.sendall(request.encode('latin-1'))
        response = bytearray()
        while b'\r\n\r\n' not in response:
            chunk = upstream.recv(4096)
            if not chunk:
                raise OSError('Upstream closed during handshake')
            response.extend(chunk)
            if len(response) > 65536:
                raise ValueError('Oversized upgrade headers')
        raw_headers, payload = bytes(response).split(b'\r\n\r\n', 1)
        if raw_headers.split(b'\r\n', 1)[0].split()[1] != b'101':
            raise ValueError('Upstream rejected upgrade')
        handler.wfile.write(raw_headers + b'\r\n\r\n' + payload)
        handler.wfile.flush()
        started = True
        handler.close_connection = True
        # Protocol, compression, fragmentation, binary data, ping and close frames
        # are transported byte-for-byte; only the HTTP handshake is interpreted.
        handler.connection.settimeout(10)
        peers = {upstream: handler.connection, handler.connection: upstream}
        while not handler.server.stopping.is_set():
            ready, _, _ = select.select(list(peers), [], [], 0.25)
            for source in ready:
                data = source.recv(64 * 1024)
                if not data:
                    return
                peers[source].sendall(data)
    except (OSError, ValueError, IndexError):
        if not started and not handler.server.stopping.is_set():
            handler.send(502, {'error': 'Module WebSocket unavailable'})
    finally:
        if upstream is not None:
            handler.server.untrack_socket(upstream)
            upstream.close()
