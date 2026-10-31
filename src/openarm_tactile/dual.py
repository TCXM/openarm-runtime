"""One local page and lifecycle for two independent tactile viewers."""
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys


def page(left, right, port):
    cards = ''.join(
        f'<section><header><div class="channel">{number:02d}</div><div><strong>左臂 · {side}侧触觉</strong>'
        f'<span class="device">{escape(Path(device).name)}</span></div><span class="sensor-tag">68 测点</span></header>'
        f'<iframe title="左臂{side}侧触觉传感器" data-port="{viewer_port}"></iframe></section>'
        for number, side, device, viewer_port in ((1, "左", left, port + 10), (2, "右", right, port + 11))
    )
    return ('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>左臂触觉工作台 · OpenArm</title><style>
:root{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:#20342f;background:#f3f5f3;font-synthesis:none}
*{box-sizing:border-box}body{margin:0}nav{display:flex;justify-content:space-between;align-items:center;padding:25px 32px 20px;gap:20px}
.eyebrow{font-size:10px;letter-spacing:2.4px;color:#71857d;font-weight:700;margin-bottom:8px}
h1{margin:0;font-size:25px;letter-spacing:-.7px;font-weight:600}.subtitle{margin:8px 0 0;font-size:12px;color:#74837d}
.context{display:flex;gap:9px;align-items:center;font-size:12px;white-space:nowrap}.pill{padding:8px 13px;border-radius:24px;background:#e3eee8;color:#386551}.muted{background:#eaece9;color:#7b857f}
main{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;padding:0 24px;height:calc(100dvh - 165px);min-height:560px}
section{min-width:0;background:#fff;border:1px solid #dce4dd;border-radius:16px;overflow:hidden;box-shadow:0 6px 24px #263f3010}
header{height:76px;padding:16px 20px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #e9eee9;background:#fff}
.channel{display:grid;place-items:center;width:34px;height:38px;background:#edf3ef;border:1px solid #e1eae3;color:#54816b;border-radius:9px;font-size:12px;font-weight:600;flex-shrink:0}
header strong{display:block;font-size:14px;font-weight:600}header .device{display:block;font-size:10px;color:#87938d;margin-top:5px;overflow-wrap:anywhere;font-family:ui-monospace,monospace}
.sensor-tag{margin-left:auto;color:#74837d;font-size:10px;white-space:nowrap}iframe{display:block;border:0;width:100%;height:calc(100% - 76px);background:#fafcfb}
footer{display:flex;justify-content:space-between;padding:14px 28px;font-size:11px;color:#819087;gap:16px}
@media(max-width:1000px){nav{padding:20px}.context{flex-direction:column;align-items:flex-end}main{grid-template-columns:1fr;height:auto;padding:0 16px;gap:16px}section{height:680px}footer{padding:16px;flex-wrap:wrap}}
@media(max-width:520px){h1{font-size:21px}.context{display:none}header{padding:12px}.sensor-tag{display:none}section{height:740px}}
</style></head><body><nav><div><div class="eyebrow">OPENARM / TACTILE WORKSPACE</div><h1>左臂触觉工作台</h1><p class="subtitle">双路触觉分布与力反馈 · IP-M2324-Core</p></div>
<div class="context"><span class="pill">左臂 · 双传感器</span><span class="pill muted">右臂 · 未安装触觉</span></div></nav><main>''' + cards + '''</main>
<footer><span>拖动旋转 · 滚轮缩放 · 在各视图中查看采集状态</span><span>力值单位 counts · 坐标单位 mm</span></footer><script>
const panels=[...document.querySelectorAll('section')];
// Preserve the mapping previously confirmed in this browser, using the original key.
const key='tactile-sides:'+panels.map(p=>p.querySelector('.device').textContent).join('|');
let swapped=false;try{swapped=localStorage.getItem(key)==='1';}catch(e){}
if(swapped){const a=panels[0],b=panels[1],af=a.querySelector('iframe'),bf=b.querySelector('iframe');
a.append(bf);b.append(af);const ad=a.querySelector('.device'),bd=b.querySelector('.device');[ad.textContent,bd.textContent]=[bd.textContent,ad.textContent];}
panels.forEach((p,i)=>{const f=p.querySelector('iframe');f.title='左臂'+(i===0?'左':'右')+'侧触觉传感器';const u=new URL(location.href);u.port=f.dataset.port;u.pathname='/';u.search='';u.hash='';f.src=u.href;});
</script></body></html>''').encode()


def serve(device, *, right_device, simulate=False, port=8891, baudrate=921600, device_id=1):
    if simulate or Path(device).resolve() == Path(right_device).resolve():
        raise ValueError("Two different hardware devices are required")
    content = page(device, right_device, port)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    children = []
    with ThreadingHTTPServer(('127.0.0.1', port), Handler) as server:
        server.timeout = 0.25
        try:
            for path, viewer_port in ((device, port + 10), (right_device, port + 11)):
                children.append(subprocess.Popen([
                    sys.executable, '-m', 'openarm_runtime', 'tactile', '--device', str(path),
                    '--port', str(viewer_port), '--baudrate', str(baudrate), '--device-id', str(device_id),
                ]))
            print(f'左臂触觉工作台：http://127.0.0.1:{port}', flush=True)
            while True:
                if any(child.poll() is not None for child in children):
                    raise RuntimeError('A tactile viewer exited; stopping both viewers')
                server.handle_request()
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
            for child in children:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
