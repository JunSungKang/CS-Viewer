'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Minimal DOM for executing the shipped renderer without browser/network access.
class Element {
  constructor(tag, text = '') {
    this.tagName = tag.toUpperCase();
    this.nodes = [];
    this.text = text;
    this.attributes = {};
    this.className = '';
    this.dataset = {};
    this.classList = {toggle() {}, contains() { return false; }};
  }
  append(...nodes) { this.nodes.push(...nodes); }
  replaceChildren(...nodes) { this.nodes = nodes; this.text = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener() {}
  set textContent(value) { this.text = String(value); this.nodes = []; }
  get textContent() { return this.text + this.nodes.map(node => node.textContent).join(''); }
}
const elements = new Map();
const context = vm.createContext({
  Node: Element, AbortController, console,
  document: {
    body: new Element('body'),
    createElement: tag => new Element(tag),
    createElementNS: (_namespace, tag) => new Element(tag),
    createTextNode: text => new Element('#text', String(text)),
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element('div'));
      return elements.get(selector);
    },
    querySelectorAll: () => [],
  },
  localStorage: {getItem: () => null, setItem() {}},
  setTimeout: () => 1, clearTimeout() {},
  fetch: () => new Promise(() => {}),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context);
function run(expression, fixture) {
  context.fixture = fixture;
  return vm.runInContext(expression, context);
}
function descendants(node) { return [node, ...node.nodes.flatMap(descendants)]; }
function named(node, className) { return descendants(node).filter(item => item.className.split(' ').includes(className)); }
function binding(port, container, label, host = '203.0.113.10', owner = 'proxy') {
  return {port: String(port), container, label, host, owner};
}
function directory(ports) { return run('portDirectory(fixture)', ports); }
const canonical = binding(8443, '443/tcp', {title: 'Example', kind: 'https'});
const redirect = binding(8080, '80/tcp', {title: 'Example', kind: 'redirect', redirect_to: 8443});
let checks = 0;
function test(name, callback) { callback(); checks++; console.log('ok - ' + name); }

test('canonical and redirect ports share a row with canonical first', () => {
  const input = [redirect, canonical];
  const before = JSON.stringify(input);
  const view = directory(input);
  assert.equal(named(view, 'port-row').length, 1);
  assert.deepEqual(named(view, 'port-chip').map(node => node.textContent), ['HTTPS :8443', '이동 :8080 → :8443']);
  assert.ok(view.textContent.includes('호스트 :8443 → 컨테이너 443/TCP'));
  assert.equal(JSON.stringify(input), before);
});
test('pairing cannot cross owner, host, transport, title or group boundaries', () => {
  for (const altered of [
    {...canonical, owner: 'other'}, {...canonical, host: '203.0.113.20'},
    {...canonical, container: '443/udp'}, {...canonical, label: {...canonical.label, title: 'Other'}},
    {...canonical, label: {...canonical.label, group: 'entry'}},
  ]) {
    const view = directory([redirect, altered]);
    assert.equal(named(view, 'port-row').length, 2);
    assert.equal(named(view, 'port-chip').length, 2);
  }
});
test('missing targets and metadata do not invent published bindings or HTTPS', () => {
  assert.equal(named(directory([redirect]), 'port-chip').length, 1);
  const unknown = directory([binding(443, '443/tcp'), binding(5353, '5353/udp')]);
  assert.deepEqual(named(unknown, 'port-chip').map(node => node.textContent), ['TCP :443', 'UDP :5353']);
  assert.equal(named(unknown, 'port-row').length, 2);
  assert.ok(unknown.textContent.includes('용도 미등록'));
});
test('loopback scope and IPv6 addresses retain actual mappings', () => {
  const view = directory([binding(20190, '8082/tcp', {title: 'Health', kind: 'http'}, '127.0.0.1'),
    binding(20191, '8082/tcp', undefined, '::1')]);
  assert.equal(descendants(view).filter(node => node.tagName === 'H3' && node.textContent === '이 컴퓨터 전용').length, 2);
  assert.ok(view.textContent.includes('[::1]'));
  assert.ok(view.textContent.includes('호스트 :20190 → 컨테이너 8082/TCP'));
});
test('exact duplicates are removed while owner and address distinctions remain', () => {
  const port = binding(80, '80/tcp');
  const containers = [{name: 'one', ports: [port, {...port}, {...port, host: '::'}]},
    {name: 'two', ports: [port]}];
  const values = run('publishedPorts(fixture)', containers);
  assert.equal(values.length, 3);
});
test('configured label strings are rendered as text', () => {
  const title = '<script>bad()</script>', detail = '<img src=x onerror=bad()>';
  const view = directory([binding(443, '443/tcp', {title, detail, kind: 'https'})]);
  assert.ok(view.textContent.includes(title) && view.textContent.includes(detail));
  assert.equal(descendants(view).filter(node => ['SCRIPT', 'IMG'].includes(node.tagName)).length, 0);
});
function service(module, http_ms, error = null) {
  return {module, http_ms, tcp_ms: 1, status: error ? 503 : 200, error, running: 1, total: 1,
    cpu_percent: 0, memory_bytes: 1, restarts: 0};
}
function board(services, containers) {
  run('state.data=fixture;renderBoard()', {services, containers, collected_at: 1});
  return elements.get('#services').nodes;
}
function listeners(count, dualStack = false) {
  return Array.from({length: count}, (_, index) => binding(8000 + index, (8000 + index) + '/tcp'))
    .flatMap(port => dualStack ? [port, {...port, host: '::'}] : [port]);
}
test('compact and directory cards retain error-first slow-to-fast ordering', () => {
  const cards = board([service('fast', 100), service('slow-directory', 500), service('error', 1, 'HTTP 503')],
    [{name: 'slow', module: 'slow-directory', ports: listeners(5)}]);
  assert.deepEqual(cards.map(card => descendants(card).find(node => node.tagName === 'H2').textContent), ['error', 'slow-directory', 'fast']);
});
test('four listeners remain compact with dual stack; five or configured purpose expand', () => {
  for (const [ports, expected] of [[listeners(4, true), false], [listeners(5), true], [[canonical], true]]) {
    const cards = board([service('example', 1)], [{name: 'example', module: 'example', ports}]);
    assert.equal(cards[0].className.includes('service-card-ports'), expected);
  }
});
console.log(checks + ' frontend port behavior checks passed');
