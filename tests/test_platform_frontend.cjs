// Run with: node --test tests/test_platform_frontend.cjs
// Browser events and HTTP responses drive the real frontend. No npm dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const staticRoot = path.join(__dirname, '..', 'src', 'openarm_platform', 'static');
const source = fs.readFileSync(path.join(staticRoot, 'platform.js'), 'utf8');
const html = fs.readFileSync(path.join(staticRoot, 'index.html'), 'utf8');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.classes = new Set();
    this.classList = {
      remove: (name) => this.classes.delete(name),
      toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name),
    };
  }
  append(...children) {
    children.forEach((child) => { child.parent = this; this.children.push(child); });
  }
  prepend(child) { child.parent = this; this.children.unshift(child); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { this.parent.children = this.parent.children.filter((child) => child !== this); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  async emit(name, event = {}) {
    for (const callback of this.listeners[name] || []) await callback(event);
  }
  focus() {}
}

async function settle() {
  for (let i = 0; i < 5; i += 1) await new Promise(setImmediate);
}

async function mount() {
  const elements = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Element()]));
  const location = new URL('http://localhost:18888/');
  const pages = ['control', 'teleop', 'tactile', 'vision'].map((id, index) => ({
    id, label: id, port: 12000 + index, exclusive_group: index < 2 ? 'motion' : null,
  }));
  const state = {
    services: Object.fromEntries(pages.map((page) => [page.id, {
      state: page.id === 'teleop' ? 'standby' : 'running',
      reachable: page.id !== 'teleop', managed: true, generation: 1,
    }])),
    requests: [], intervals: [], confirms: [], confirm: true, statusError: false,
  };
  state.selection = async () => ({ok: true, json: async () => ({services: state.services})});
  const document = {
    getElementById: (id) => elements[id],
    createElement: (tag) => new Element(tag),
    addEventListener() {}, hidden: false,
  };
  const window = {
    location, addEventListener() {},
    confirm: (message) => { state.confirms.push(message); return state.confirm; },
  };
  vm.runInNewContext(source, {
    document, window, location, URL, AbortSignal,
    navigator: {clipboard: {writeText: async () => {}}},
    history: {replaceState: (_, __, url) => { location.href = new URL(url, location).href; }},
    setTimeout: () => 1,
    setInterval: (callback) => { state.intervals.push(callback); return 1; },
    fetch: async (url, options = {}) => {
      state.requests.push({url, options});
      if (url === '/api/config') return {ok: true, json: async () => ({port: 8888, pages})};
      if (url === '/api/status') {
        if (state.statusError) throw new Error('Connection closed');
        return {ok: true, json: async () => ({services: state.services})};
      }
      if (url === '/api/select') return state.selection(JSON.parse(options.body).id);
      throw new Error('Unexpected request: ' + url);
    },
  });
  await settle();
  assert.equal(elements['config-error'].hidden, true);
  return {
    elements, state, location,
    select: (id) => elements.navigation.children[pages.findIndex((page) => page.id === id)].emit('click'),
    frames: () => elements.views.children.flatMap((panel) => panel.children.filter((child) => child.tagName === 'iframe')),
    poll: async () => { state.intervals[0](); await settle(); },
  };
}

test('all module navigation uses one origin and one forwarded port; sensing retains controller frame', async () => {
  const app = await mount();
  assert.equal(app.elements['ssh-command'].textContent, 'ssh -N -L 18888:127.0.0.1:8888 USER@WORKSTATION');
  assert.equal(app.elements['port-list'].children.length, 1);
  await app.select('control');
  const controller = app.frames()[0];
  assert.equal(controller.src, 'http://localhost:18888/apps/control/');
  await app.select('tactile');
  await app.select('vision');
  assert.equal(app.frames().length, 3);
  assert.equal(app.frames()[0], controller);
  assert.equal(controller.parent.hidden, true);
  assert.equal(app.state.confirms.length, 0);
  assert.equal(app.elements['external-page'].href, 'http://localhost:18888/apps/vision/');
  await app.select('control');
  assert.equal(app.frames()[0], controller);
  assert.equal(controller.parent.hidden, false);
  assert(app.frames().every((frame) => new URL(frame.src).origin === app.location.origin));
});

test('cancelled and failed mode switches preserve prior connection and release busy state', async () => {
  const app = await mount();
  await app.select('control');
  const controller = app.frames()[0];
  app.state.confirm = false;
  const requests = app.state.requests.length;
  await app.select('teleop');
  assert.equal(app.state.requests.length, requests);
  assert.equal(app.frames()[0], controller);
  assert.equal(app.location.hash, '#control');
  app.state.confirm = true;
  let release;
  app.state.selection = () => new Promise((resolve) => { release = resolve; });
  const pending = app.select('teleop');
  assert.equal(app.elements.views.inert, true);
  assert.equal(app.elements.views.attributes['aria-busy'], 'true');
  assert.equal(app.elements['external-page'].attributes['aria-disabled'], 'true');
  assert(app.elements.navigation.children.every((button) => button.disabled));
  release({ok: false, json: async () => ({error: 'Controller still stopping'})});
  await pending;
  assert.equal(app.elements.views.inert, false);
  assert.equal(app.elements.views.attributes['aria-busy'], 'false');
  assert.equal(app.frames()[0], controller);
  assert.equal(controller.parent.hidden, false);
  assert.equal(app.elements['service-notice'].textContent, 'Controller still stopping');
  assert.equal(app.location.hash, '#control');
});

test('successful exclusive switch discards stopped peer but preserves sensing frames', async () => {
  const app = await mount();
  await app.select('control');
  const controller = app.frames()[0];
  await app.select('tactile');
  const sensor = app.frames()[1];
  app.state.selection = async () => {
    app.state.services.control = {state: 'standby', reachable: false, managed: false, generation: 1};
    app.state.services.teleop = {state: 'running', reachable: true, managed: true, generation: 1};
    return {ok: true, json: async () => ({services: app.state.services})};
  };
  await app.select('teleop');
  assert.equal(app.frames().includes(controller), false);
  assert.equal(app.frames().includes(sensor), true);
  assert(app.frames().some((frame) => frame.src === 'http://localhost:18888/apps/teleop/'));
});

test('lost status and service errors show diagnostics without destroying the frame', async () => {
  const app = await mount();
  await app.select('vision');
  const camera = app.frames()[0];
  app.state.statusError = true;
  await app.poll();
  assert.equal(app.frames()[0], camera);
  assert.equal(app.elements['service-notice'].hidden, false);
  assert.match(app.elements['service-notice'].textContent, /SSH/);
  assert.equal(app.elements['external-page'].attributes['aria-disabled'], 'true');
  app.state.statusError = false;
  app.state.services.vision = {state: 'error', reachable: false, error: 'Camera process exited'};
  await app.poll();
  assert.equal(app.elements['service-notice'].textContent, 'Camera process exited');
  app.state.services.vision = {state: 'running', reachable: true, managed: true, generation: 1};
  await app.poll();
  assert.equal(app.frames()[0], camera);
  assert.equal(camera.parent.hidden, false);
  assert.equal(app.elements['service-notice'].hidden, true);
});
