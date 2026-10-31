/* Finite JPEG/PNG requests: Safari must never fetch multipart/x-mixed-replace. */
async function initialise() {
  try {
    const response = await fetch('/config', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw Error('Configuration unavailable');
    startViewer(await response.json());
  } catch {
    document.querySelector('#summary').textContent = '无法加载相机配置，正在重试…';
    setTimeout(initialise, 2000);
  }
}

function startViewer(config) {
  const names = {head: '头部 RGB', depth: '头部深度', left: '左腕', right: '右腕',
    wrist_usb_2_4: '腕部 · USB 2.4', wrist_usb_2_3: '腕部 · USB 2.3'};
  const cameras = config.cameras.map(c => ({...c, label: Object.hasOwn(names, c.id) ? names[c.id] : c.id}));
  const grid = document.querySelector('#grid'), layout = document.querySelector('#layout');
  const depthSource = document.querySelector('#depth-source');
  const showDepth = document.querySelector('#show-depth');
  const rgbCameras = cameras.filter(c => c.kind === 'rgb');
  const depthCameras = cameras.filter(c => c.kind === 'depth');
  const labels = {Online: '在线', Connected: '等待首帧', Starting: '启动中', Connecting: '连接中',
    Disconnected: '未连接', Stale: '画面超时', Stopped: '已停止', Unavailable: '采集服务暂不可达'};
  const feeds = Object.fromEntries(cameras.map(c => [c.id, {config: c, url: null, last: 0,
    error: '等待画面…', controller: null, epoch: 0, failures: 0, attempts: 0,
     successes: 0, lastError: '', retryAt: 0, rateAt: Date.now(), rateFrames: 0, fps: 0}]));
  let paused = false, suspended = false, states = {}, depths = {}, statusFailures = 0,
      statusAt = 0, depthRevision = 0;
  const running = () => !paused && !suspended && !document.hidden;
  const visible = id => layout.value === 'all' ?
    feeds[id].config.kind !== 'depth' || showDepth.checked : layout.value === 'camera:' + id;

  document.querySelector('#simulation').hidden = !config.simulation;
  document.querySelector('#depth-controls').hidden = !depthCameras.length;
  document.querySelector('#depth-toggle').hidden = !depthCameras.length;
  for (const c of cameras) {
    const option = new Option(c.label, 'camera:' + c.id); layout.add(option);
    if (c.kind === 'depth') depthSource.add(new Option(c.label, c.id));
    const card = document.createElement('section'); card.className = 'card'; card.id = 'camera-' + c.id;
    card.classList.toggle('primary', c.id === 'head');
    card.innerHTML = '<header><strong></strong><span class="status">等待画面</span><button type="button">放大</button></header>' +
      '<div class="viewport"><img hidden><div class="placeholder">等待画面…</div></div>' +
      (c.kind === 'depth' ? '<div class="scale" title="近端红色，远端蓝紫色"></div>' : '') +
      '<footer><span></span><span></span></footer>';
    card.querySelector('strong').textContent = c.label;
    card.querySelector('button').ariaLabel = c.label + '全屏';
    card.querySelector('button').onclick = async () => {
      try { if (document.fullscreenElement) await document.exitFullscreen(); else await card.requestFullscreen(); }
      catch { layout.value = 'camera:' + c.id; render(); }
    };
    card.querySelector('.viewport').style.setProperty('--aspect', `${c.width}/${c.height}`);
    const footer = card.querySelector('footer');
    footer.children[0].textContent = `${c.kind === 'depth' ? '深度' : 'RGB'} · ${c.width} × ${c.height}`;
    footer.children[1].textContent = `配置 ${c.fps} FPS`;
    const source = document.createElement('div'); source.className = 'source';
    source.innerHTML = '<span></span><span class="status">等待画面</span>';
    source.children[0].textContent = c.label;
    document.querySelector('#sources').append(source); grid.append(card);
    Object.assign(feeds[c.id], {card, img: card.querySelector('img'),
      placeholder: card.querySelector('.placeholder'), badge: card.querySelector('.status'), sourceBadge: source.children[1], footer});
    feeds[c.id].img.alt = c.label + '实时画面';
  }

  function updateCard(id) {
    const f = feeds[id], statusRecent = statusAt && Date.now() - statusAt < 10000;
    const fresh = f.last && Date.now() - f.last < 5000;
    const confirmed = statusFailures === 0 && statusRecent &&
      ['Stale', 'Disconnected', 'Unavailable', 'Stopped'].includes(states[id]);
    const unknown = !statusRecent && statusFailures >= 3;
    f.img.hidden = !f.url;
    f.placeholder.hidden = Boolean(fresh && !confirmed && running() && !f.error && !unknown);
    f.placeholder.textContent = paused ? '预览已暂停' : document.hidden ? '后台暂停' :
      f.error || (confirmed ? labels[states[id]] : unknown ? '状态连接中断，画面可能延迟' : fresh ? '' : '正在等待新画面…');
    f.badge.textContent = paused ? '已暂停' : fresh && !confirmed && !f.error && !unknown ? '在线' :
      f.error ? '恢复中' : unknown ? '状态未知' : confirmed ? labels[states[id]] : '等待画面';
    f.badge.dataset.online = String(Boolean(fresh && !confirmed && !f.error && !unknown && running()));
    f.sourceBadge.textContent = f.badge.textContent;
    f.sourceBadge.dataset.online = f.badge.dataset.online;
    updateFrameRate(id);
  }
  function updateFrameRate(id) {
    const f = feeds[id], now = Date.now(), elapsed = now - f.rateAt;
    if (elapsed >= 1000) {
      f.fps = (f.successes - f.rateFrames) * 1000 / elapsed;
      f.rateFrames = f.successes; f.rateAt = now;
    }
    f.footer.children[1].textContent = !running() || !visible(id) ? '已暂停' :
      `${f.fps.toFixed(1)} FPS · ${f.last ? Math.floor((now - f.last) / 1000) + ' 秒前更新' : '等待首帧'}`;
  }
  function updateDepthDetails() {
    const id = depthSource.value, d = depths[id] || {};
    document.querySelector('.download').href = '/depth/' + encodeURIComponent(id) + '.png?raw=1';
    document.querySelector('#depth-stats').textContent = states[id] === 'Online' ?
      `有效像素 ${d.valid_percent ?? '—'}% · 中位深度 ${d.median_mm ?? '—'} mm` : '深度数据暂不可用';
  }
  function render() {
    grid.classList.toggle('single', layout.value !== 'all');
    grid.classList.toggle('has-head', layout.value === 'all' && cameras.some(c => c.id === 'head'));
    const selected = cameras.find(c => 'camera:' + c.id === layout.value);
    document.querySelector('#view-title').textContent = selected ? selected.label : '相机预览';
    for (const c of cameras) {
      const f = feeds[c.id]; f.card.hidden = !visible(c.id);
      f.img.style.transform = document.querySelector('#mirror').checked ? 'scaleX(-1)' : '';
      updateCard(c.id);
    }
    document.querySelector('#pause').textContent = paused ? '恢复预览' : '暂停预览';
    document.querySelector('#mode').textContent = paused ? 'PAUSED' : config.simulation ? 'SIMULATION' : 'LIVE VIEW';
  }
  async function showFrame(id, bytes, type, epoch) {
    const f = feeds[id], next = URL.createObjectURL(new Blob([bytes], {type}));
    const decoded = new Image(); decoded.src = next;
    try { await decoded.decode(); } catch (error) { URL.revokeObjectURL(next); throw error; }
    if (f.epoch !== epoch || !running() || !visible(id)) { URL.revokeObjectURL(next); return; }
    const previous = f.url; f.img.src = next; f.url = next; f.last = Date.now();
    f.error = ''; f.failures = 0; f.successes++; f.lastError = '';
    if (previous) URL.revokeObjectURL(previous); updateCard(id);
  }
  function syncPlayback() {
    if (!running()) for (const f of Object.values(feeds)) { f.epoch++; f.controller?.abort(); }
    render();
  }
  async function poll() {
    if (document.hidden || suspended) { setTimeout(poll, 2000); return; }
    try {
      const response = await fetch('/status', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (!response.ok) throw Error();
      const data = await response.json(); states = data.cameras; depths = data.depths;
      statusAt = Date.now(); statusFailures = 0;
      document.querySelector('#summary').textContent =
        `${rgbCameras.filter(c => states[c.id] === 'Online').length} / ${rgbCameras.length} 路 RGB 在线` +
        (depthCameras.length ? ` · 深度 ${depthCameras.filter(c => states[c.id] === 'Online').length} / ${depthCameras.length} 在线` : '');
      updateDepthDetails();
    } catch {
      statusFailures++;
      document.querySelector('#summary').textContent = statusFailures < 3 ?
        '状态查询延迟，视频继续播放…' : '状态连接中断，自动重试中；各路画面独立恢复';
    } finally { render(); setTimeout(poll, 2000); }
  }
  async function updateFrame(id) {
    const f = feeds[id], depth = f.config.kind === 'depth';
    try {
      if (!running() || !visible(id) || Date.now() < f.retryAt) return;
      let url = '/frame/' + encodeURIComponent(id) + '.jpg';
      const revision = depthRevision;
      if (depth) {
        const near = Number(document.querySelector('#near').value), far = Number(document.querySelector('#far').value);
        if (!(0 <= near && near < far && far <= 20)) { f.error = '请设置 0 ≤ 近端 < 远端 ≤ 20 米'; updateCard(id); return; }
        url = `/depth/${encodeURIComponent(id)}.png?near=${near}&far=${far}`;
      }
      const epoch = f.epoch, controller = new AbortController(); f.controller = controller; f.attempts++;
      const timeout = setTimeout(() => controller.abort('timeout'), 6000);
      try {
        const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
        if (!response.ok) throw Error('HTTP ' + response.status);
        const bytes = await response.arrayBuffer();
        if (epoch === f.epoch && !controller.signal.aborted && (!depth || revision === depthRevision)) {
          await showFrame(id, bytes, depth ? 'image/png' : 'image/jpeg', epoch);
          if (depth) f.footer.children[0].textContent =
            `深度 · ${document.querySelector('#near').value}–${document.querySelector('#far').value} m · 近红 / 远蓝`;
        }
      } catch (error) {
        if (epoch === f.epoch && running()) {
          f.failures++; f.lastError = String(error.message || error);
          f.retryAt = Date.now() + Math.min(1500, 200 * f.failures);
          f.error = f.last && Date.now() - f.last < 2000 ? '' : '画面暂时延迟，自动恢复中…'; updateCard(id);
        }
      } finally { clearTimeout(timeout); if (f.controller === controller) f.controller = null; }
    } finally { setTimeout(() => updateFrame(id), depth ? 100 : 50); }
  }

  layout.onchange = render; document.querySelector('#mirror').onchange = render;
  showDepth.onchange = render;
  depthSource.onchange = updateDepthDetails;
  for (const id of ['near', 'far']) document.getElementById(id).oninput = () => { depthRevision++; };
  document.querySelector('#pause').onclick = () => { paused = !paused; syncPlayback(); };
  document.querySelector('#reconnect').onclick = () => {
    for (const f of Object.values(feeds)) { f.epoch++; f.controller?.abort(); f.retryAt = 0; }
    syncPlayback();
  };
  document.addEventListener('visibilitychange', syncPlayback);
  window.addEventListener('pagehide', () => {
    suspended = true; syncPlayback();
    for (const f of Object.values(feeds)) { if (f.url) URL.revokeObjectURL(f.url); f.url = null; f.last = 0; }
  });
  window.addEventListener('pageshow', () => { suspended = false; syncPlayback(); });
  setInterval(() => { for (const c of cameras) updateCard(c.id); }, 1000);
  window.cameraDiagnostics = () => Object.fromEntries(Object.entries(feeds).map(([id, f]) => [id,
    {requests: f.attempts, frames: f.successes, failures: f.failures, lastError: f.lastError,
      ageMs: f.last ? Date.now() - f.last : null}]));
  syncPlayback(); poll(); for (const c of cameras) updateFrame(c.id);
}
initialise();
