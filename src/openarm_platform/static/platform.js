(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const icons = {
    control: '<path d="M4 5h12M4 10h12M4 15h12M7 3v4M13 8v4M8 13v4"/>',
    teleop: '<path d="M3 15v-4l5-3 4-5M8 8l7 4v4M1 17h4m8 0h4"/><circle cx="8" cy="8" r="2"/><circle cx="12" cy="3" r="1.5"/>',
    tactile: '<path d="M5 16c-2-2-3-5-2-6s2 0 3 1V4a1 1 0 0 1 2 0v5-6a1 1 0 0 1 2 0v6-5a1 1 0 0 1 2 0v6-4a1 1 0 0 1 2 0v6c0 3-2 5-4 5H8l-3-1Z"/>',
    vision: '<rect x="2" y="5" width="12" height="11" rx="2"/><path d="m14 8 4-2v9l-4-2"/><circle cx="8" cy="10.5" r="2.5"/>'
  };
  const defaults = {control: '电机控制', teleop: '遥操', tactile: '触觉', vision: '视觉'};
  let pages = [];
  let active = null;
  let statuses = {};
  let configured = false;
  let polling = false;
  let selecting = false;
  let attachOnly = false;
  let platformPort = 8888;
  let statusRevision = 0;
  let selectionError = '';
  let selectionTarget = '';
  const frames = new Map();
  const elements = new Map();
  function icon(id) {
    const span = document.createElement('span');
    span.className = 'module-icon';
    span.setAttribute('aria-hidden', 'true');
    span.innerHTML = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round">' + (icons[id] || icons.control) + '</svg>';
    return span;
  }
  function text(tag, className, content) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    el.textContent = content;
    return el;
  }
  function pageUrl(page) {
    return new URL('/apps/' + encodeURIComponent(page.id) + '/', window.location.origin).href;
  }
  function isReady(state) {
    return state?.reachable === true && state.state !== 'error';
  }
  function statusFor(id) {
    const state = statuses[id];
    if (!state || typeof state.reachable !== 'boolean') return {kind: '', label: '状态未知'};
    if (state.state === 'error') return {kind: 'unreachable', label: '启动失败'};
    if (state.reachable) return {kind: 'reachable', label: '服务可达'};
    const labels = {starting: '启动中', unconfigured: '未配置', error: '启动失败', stopped: '已停止', standby: '待切换'};
    return {kind: 'unreachable', label: labels[state.state] || '服务不可达'};
  }
  function updateStatus() {
    pages.forEach((page) => {
      const state = statusFor(page.id), els = elements.get(page.id);
      els.dot.className = 'status-dot ' + state.kind;
      els.dot.title = state.label;
      els.cardDot.className = 'status-dot ' + state.kind;
      els.cardStatus.textContent = state.label;
    });
    const notice = $('service-notice');
    if (selecting) {
      notice.hidden = false;
      notice.textContent = '正在切换到' + selectionTarget + '，等待原有服务完成停止与启动…';
    } else if (selectionError) {
      notice.hidden = false;
      notice.textContent = selectionError;
    } else if (!active) {
      notice.hidden = true;
    }
    if (!active) return;
    const state = statusFor(active.id);
    const current = statuses[active.id] || {};
    $('current-status').className = 'service-state ' + state.kind;
    $('current-status').textContent = state.label;
    $('reload-page').disabled = selecting || !isReady(current);
    const canOpen = !selecting && isReady(current);
    $('external-page').setAttribute('aria-disabled', String(!canOpen));
    $('external-page').tabIndex = canOpen ? 0 : -1;
    if (!selectionError && !selecting) {
      notice.hidden = state.kind === 'reachable';
      const notices = {
        starting: '正在启动' + active.label + '服务，就绪后将自动打开页面。',
        unconfigured: active.label + '尚未配置启动命令。请在工作站的平台配置中补充该模块。',
        error: current.error || (active.label + '启动失败，请在工作站终端检查启动信息。'),
        stopped: active.label + '服务已停止，可重新选择该模块。',
        standby: active.label + '处于待切换状态，选择该模块以打开页面。'
      };
      notice.textContent = notices[current.state] || '暂时无法读取平台状态，正在重试。当前模块连接会保留；请检查工作站或 SSH 连接。';
    }
    syncActiveFrame();
  }
  async function pollStatus() {
    if (polling || selecting || !configured) return;
    polling = true;
    const revision = statusRevision;
    try {
      const response = await fetch('/api/status', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (!response.ok) throw new Error('status ' + response.status);
      const data = await response.json();
      if (revision === statusRevision) statuses = data.services || {};
    } catch (_) {
      if (revision === statusRevision) statuses = {};
    } finally { polling = false; updateStatus(); }
  }
  function showOverview(updateHash = true) {
    if (selecting) return;
    active = null;
    selectionError = '';
    frames.forEach(({panel}) => { panel.hidden = true; });
    elements.forEach(({nav}) => { nav.classList.remove('active'); nav.removeAttribute('aria-current'); });
    $('overview').hidden = false; $('views').hidden = true;
    $('page-actions').hidden = true; $('service-notice').hidden = true;
    $('current-label').textContent = '概览';
    $('footer-caption').textContent = '四个模块 · 同一个工作空间';
    if (updateHash) history.replaceState(null, '', window.location.pathname + window.location.search);
  }
  function removeFrame(id) {
    const frame = frames.get(id);
    if (frame) { frame.panel.remove(); frames.delete(id); }
  }
  function mountFrame(page, generation) {
    const panel = document.createElement('section');
    panel.className = 'view-panel'; panel.setAttribute('aria-label', page.label);
    const iframe = document.createElement('iframe');
    iframe.title = page.label + ' · OpenArm';
    iframe.allowFullscreen = true;
    const loading = text('div', 'view-loading', '正在打开' + page.label + '…');
    const dots = document.createElement('span'); dots.className = 'loading-dots'; dots.setAttribute('aria-hidden', 'true');
    for (let i = 0; i < 3; i += 1) dots.append(document.createElement('i'));
    loading.prepend(dots);
    iframe.addEventListener('load', () => { loading.hidden = true; });
    panel.append(iframe, loading);
    $('views').append(panel);
    iframe.src = pageUrl(page);
    // Frame load also fires for an error response; service status remains separate.
    setTimeout(() => { loading.hidden = true; }, 8000);
    frames.set(page.id, {panel, iframe, loading, generation});
  }
  function syncActiveFrame() {
    if (!active) return;
    const current = statuses[active.id] || {};
    const ready = isReady(current);
    const generation = current.generation ?? 0;
    if (ready) {
      const existing = frames.get(active.id);
      if (existing && existing.generation !== generation) removeFrame(active.id);
      if (!frames.has(active.id)) mountFrame(active, generation);
    }
    frames.forEach(({panel}, id) => { panel.hidden = id !== active.id || !ready; });
    $('view-waiting').hidden = ready;
    $('waiting-title').textContent = current.state === 'starting' ? '正在启动' + active.label : '等待' + active.label + '服务';
    $('waiting-description').textContent = current.state === 'standby'
      ? '控制与遥操使用同一组电机连接，请选择需要的模式。'
      : '服务就绪后自动显示模块。';
  }
  function setSelecting(value) {
    selecting = value;
    $('views').inert = value;
    $('views').setAttribute('aria-busy', String(value));
    elements.forEach(({nav, card}) => { nav.disabled = value; card.disabled = value; });
    $('home-button').disabled = value;
    $('reload-page').disabled = value || !active || !isReady(statuses[active.id]);
  }
  async function selectPage(id, updateHash = true) {
    const page = pages.find((p) => p.id === id);
    if (!page || selecting) return;
    const peer = !attachOnly && page.exclusive_group && pages.find((p) => {
      const state = statuses[p.id];
      return p.id !== id && p.exclusive_group === page.exclusive_group && state?.managed && (state.reachable || state.state === 'running');
    });
    if (peer && !window.confirm('切换将关闭当前' + peer.label + '页面服务，并执行原有停止流程。继续？')) {
      if (!updateHash) history.replaceState(null, '', active ? '#' + active.id : window.location.pathname + window.location.search);
      return;
    }
    selectionTarget = page.label;
    setSelecting(true);
    statusRevision += 1;
    selectionError = '';
    updateStatus();
    try {
      const response = await fetch('/api/select', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id}), signal: AbortSignal.timeout(60000)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || ('切换失败：HTTP ' + response.status));
      statuses = data.services || {};
      // Only a successful coordinated switch may release the stopped peer's frame.
      if (page.exclusive_group) {
        pages.forEach((p) => {
          const state = statuses[p.id];
          if (p.id !== id && p.exclusive_group === page.exclusive_group && state && !state.reachable && ['standby', 'stopped', 'error', 'unconfigured'].includes(state.state)) removeFrame(p.id);
        });
      }
      active = page;
      elements.forEach(({nav}, key) => {
        nav.classList.toggle('active', key === id);
        if (key === id) nav.setAttribute('aria-current', 'page'); else nav.removeAttribute('aria-current');
      });
      $('overview').hidden = true; $('views').hidden = false;
      $('page-actions').hidden = false;
      $('current-label').textContent = page.label;
      $('external-page').href = pageUrl(page);
      $('external-page').setAttribute('aria-label', '在新标签页打开' + page.label);
      $('footer-caption').textContent = page.exclusive_group
        ? '控制与遥操共享电机连接 · 切换模式会执行原有停止流程'
        : '切换到感知页面会保留当前控制或遥操连接';
      if (updateHash) history.replaceState(null, '', '#' + id);
    } catch (error) {
      selectionError = error.name === 'TimeoutError'
        ? '切换请求超时，请查看工作站终端状态后重试。'
        : String(error.message || '无法切换页面，请重试。');
      if (!updateHash) history.replaceState(null, '', active ? '#' + active.id : window.location.pathname + window.location.search);
    } finally {
      setSelecting(false);
      updateStatus();
    }
  }
  function connectionPanel(open) {
    $('connection-panel').hidden = !open;
    $('connection-toggle').setAttribute('aria-expanded', String(open));
    if (open) $('connection-close').focus();
  }
  function renderConnections() {
    const localPort = Number(location.port || (location.protocol === 'https:' ? 443 : 80));
    $('port-list').replaceChildren(text('span', 'port-tag', '统一入口 · ' + localPort));
    $('ssh-command').textContent = 'ssh -N -L ' + localPort + ':127.0.0.1:' + platformPort + ' USER@WORKSTATION';
  }
  function renderPages() {
    $('navigation').replaceChildren(); $('module-grid').replaceChildren(); elements.clear();
    pages.forEach((page, index) => {
      const nav = document.createElement('button'); nav.className = 'nav-item';
      const dot = text('span', 'status-dot', ''); dot.setAttribute('aria-hidden', 'true');
      nav.append(icon(page.id), text('span', '', page.label), dot);
      nav.addEventListener('click', () => selectPage(page.id));
      $('navigation').append(nav);
      const card = document.createElement('button'); card.className = 'module-card';
      const top = text('span', 'card-top', '');
      top.append(icon(page.id), text('span', 'card-index', String(index + 1).padStart(2, '0')));
      const bottom = text('span', 'card-bottom', '');
      const state = text('span', 'card-state', '');
      const cardDot = text('span', 'status-dot', ''); cardDot.setAttribute('aria-hidden', 'true');
      const cardStatus = text('span', '', '状态未知');
      state.append(cardDot, cardStatus);
      const arrow = text('span', 'card-arrow', '↗'); arrow.setAttribute('aria-hidden', 'true');
      bottom.append(state, arrow);
      card.append(top, text('h2', '', page.label), text('p', '', page.description || ''), bottom);
      card.addEventListener('click', () => selectPage(page.id));
      $('module-grid').append(card);
      elements.set(page.id, {nav, card, dot, cardStatus, cardDot});
    });
  }
  async function configure() {
    $('retry-config').disabled = true;
    try {
      const response = await fetch('/api/config', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const config = await response.json();
      if (!Array.isArray(config.pages) || config.pages.length !== 4) throw new Error('模块配置不完整');
      const seen = new Set();
      pages = config.pages.map((p) => {
        if (!Object.hasOwn(defaults, p.id) || seen.has(p.id)) throw new Error('模块配置无效');
        seen.add(p.id);
        return {...p, label: p.label || defaults[p.id]};
      });
      document.title = config.title || 'OpenArm Platform';
      $('overview-title').textContent = config.title || 'OpenArm Platform';
      attachOnly = config.attach_only === true;
      platformPort = Number.isInteger(config.port) ? config.port : 8888;
      configured = true; $('config-error').hidden = true;
      renderPages(); renderConnections();
      await pollStatus();
      const selected = location.hash.slice(1);
      if (pages.some((page) => page.id === selected)) await selectPage(selected, false); else showOverview(false);
    } catch (error) {
      $('config-error').hidden = false; $('error-message').textContent = error.message;
    } finally { $('retry-config').disabled = false; }
  }
  $('home-button').addEventListener('click', () => showOverview());
  $('retry-config').addEventListener('click', configure);
  $('connection-toggle').addEventListener('click', () => connectionPanel($('connection-panel').hidden));
  $('connection-close').addEventListener('click', () => connectionPanel(false));
  $('footer-connection').addEventListener('click', () => connectionPanel(true));
  $('reload-page').addEventListener('click', () => {
    if (!active || selecting || !isReady(statuses[active.id]) || !frames.has(active.id) || !window.confirm('重新加载会中断当前子页面的浏览器连接，控制或遥操可能因此停止。继续重新加载？')) return;
    const frame = frames.get(active.id);
    frame.loading.hidden = false;
    frame.iframe.src = pageUrl(active);
    setTimeout(() => { frame.loading.hidden = true; }, 8000);
    pollStatus();
  });
  $('copy-command').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('ssh-command').textContent); $('copy-command').textContent = '已复制'; }
    catch (_) { $('copy-command').textContent = '请手动复制'; }
    setTimeout(() => { $('copy-command').textContent = '复制'; }, 2000);
  });
  window.addEventListener('hashchange', () => {
    const id = location.hash.slice(1);
    if (pages.some((page) => page.id === id)) selectPage(id, false); else showOverview(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('connection-panel').hidden) { connectionPanel(false); $('connection-toggle').focus(); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) pollStatus(); });
  setInterval(() => { if (!document.hidden) pollStatus(); }, 5000);
  configure();
})();
