import assert from 'node:assert/strict';
import { readFile, readdir, access } from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';

// The project's optional Python test dependencies bundle Playwright's Node API.
// Enable with SHIRI_BROWSER_TESTS=1 after installing that test extra and Chromium.
const enabled = process.env.SHIRI_BROWSER_TESTS === '1';
const root = fileURLToPath(new URL('../..', import.meta.url));
let playwright;
if (enabled) {
  let modulePath = process.env.SHIRI_PLAYWRIGHT_MODULE;
  if (!modulePath) {
    const entries = await readdir(path.join(root, '.venv/lib'));
    for (const entry of entries.filter((name) => name.startsWith('python'))) {
      const candidate = path.join(root, '.venv/lib', entry, 'site-packages/playwright/driver/package/index.mjs');
      try { await access(candidate); modulePath = candidate; break; } catch {}
    }
  }
  if (!modulePath) throw new Error('Install the Python Playwright test dependency or set SHIRI_PLAYWRIGHT_MODULE.');
  playwright = await import(pathToFileURL(modulePath).href);
}

async function withPage(action) {
  const server = http.createServer(async (request, response) => {
    const asset = request.url === '/' ? 'index.html' : request.url.replace(/^\/assets\//, '');
    if (!['index.html', 'app.js', 'style.css'].includes(asset)) { response.writeHead(404).end(); return; }
    const content = await readFile(path.join(root, 'shiri/web', asset));
    response.writeHead(200, { 'Content-Type': asset.endsWith('.js') ? 'text/javascript' : asset.endsWith('.css') ? 'text/css' : 'text/html' }).end(content);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  let browser, context;
  try {
    browser = await playwright.chromium.launch({ headless: true, ...(process.env.SHIRI_CHROMIUM_EXECUTABLE ? { executablePath: process.env.SHIRI_CHROMIUM_EXECUTABLE } : {}) });
    context = await browser.newContext();
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await action(page, `http://127.0.0.1:${server.address().port}`, errors);
  } finally { await context?.close(); await browser?.close(); await new Promise((resolve) => server.close(resolve)); }
}

function snapshot(rooms = [], simulation = true) {
  return { rooms, interfaces: ['eth0'], runtime: { ready: true, simulation, error: null }, capabilities: {} };
}

function roomValue(overrides = {}) {
  return { id: '00000000-0000-4000-8000-000000000001', name: 'Living room', airplay_name: 'Living room', interface: 'eth0', nobly_room_id: null, local_audio_device: null, enabled: true, volume: 50, duck_gain: .28, revision: 1, speakers: [], outputs_error: null,
    runtime: { status: 'running', error: null }, outputs: [{ id: '101', name: 'Kitchen speaker', protocol: 'airplay2', selected: false, available: true, assignable: true, offset_ms: 0, sync_quality: 'native' }, { id: '202', name: 'Google Cast', protocol: 'chromecast', selected: false, available: true, assignable: true, sync_quality: 'approximate' }], ...overrides };
}

test('browser creates a room, protects drafts, assigns speakers, and handles conflicts', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  let current = snapshot();
  let nextConflict = false;
  let postedCreate;
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const body = request.method() === 'GET' || request.method() === 'DELETE' ? null : request.postDataJSON();
    let data;
    if (url.pathname.endsWith('/state')) data = current;
    else if (url.pathname.endsWith('/events')) data = { events: [] };
    else if (url.pathname.endsWith('/rooms') && request.method() === 'POST') {
      postedCreate = body;
      current.rooms.push(roomValue({ ...body, enabled: false, runtime: { status: 'stopped', error: null } }));
      data = { room: current.rooms[0], runtime_accepted: true };
    } else if (request.method() === 'PATCH' && !url.pathname.includes('/offset')) {
      if (nextConflict) { nextConflict = false; await route.fulfill({ status: 409, json: { error: 'Room changed; reload before saving', code: 'conflict' } }); return; }
      Object.assign(current.rooms[0], body.changes, { revision: current.rooms[0].revision + 1 });
      current.rooms[0].runtime.status = current.rooms[0].enabled ? 'running' : 'stopped';
      data = { room: current.rooms[0], runtime_accepted: true };
    } else if (url.pathname.endsWith('/speakers') && request.method() === 'PUT') {
      current.rooms[0].speakers = current.rooms[0].outputs.filter((output) => body.speaker_ids.includes(output.id)).map(({ id, name, protocol }) => ({ id, name, protocol, offset_ms: 0 }));
      current.rooms[0].revision += 1;
      for (const output of current.rooms[0].outputs) output.selected = body.speaker_ids.includes(output.id);
      data = { room: current.rooms[0], runtime_accepted: true };
    } else data = { ok: true };
    await route.fulfill({ status: 200, json: data });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Add your first room' }).waitFor();
  await page.getByRole('button', { name: 'Add room', exact: true }).click();
  await page.getByLabel('Room name', { exact: true }).fill('Living <room>');
  await page.getByLabel('Nobly room ID', { exact: false }).fill('Room/UPPER_ID');
  await page.getByRole('button', { name: 'Create room', exact: true }).click();
  await page.getByRole('heading', { name: 'Living <room>', exact: true }).waitFor();
  assert.equal(postedCreate.duck_gain, undefined, 'Strict RoomCreate must not receive patch-only fields');
  assert.equal(postedCreate.nobly_room_id, 'Room/UPPER_ID');
  assert.equal(await page.locator('room').count(), 0, 'Names must be rendered as text');
  await page.getByRole('checkbox', { name: 'Turn Living <room> audio on' }).check();
  await page.getByRole('button', { name: 'Speakers', exact: true }).click();
  await page.getByRole('checkbox', { name: 'Use Kitchen speaker in Living <room>' }).check();
  await page.getByRole('button', { name: 'Refresh discovery' }).click();
  assert.equal(await page.getByRole('checkbox', { name: 'Use Kitchen speaker in Living <room>' }).isChecked(), true);
  await page.getByRole('button', { name: 'Save speakers', exact: true }).click();
  await page.locator('#speaker-dialog').waitFor({ state: 'hidden' });
  assert.equal(current.rooms[0].speakers.length, 1);
  await page.getByRole('button', { name: 'Room settings' }).click();
  await page.getByLabel('Room name', { exact: true }).fill('Unsaved room');
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  assert.equal(await page.getByLabel('Room name', { exact: true }).inputValue(), 'Unsaved room');
  nextConflict = true;
  await page.getByRole('button', { name: 'Save room', exact: true }).click();
  await page.getByText('Room changed; reload before saving', { exact: true }).last().waitFor();
  assert.equal(await page.getByLabel('Room name', { exact: true }).inputValue(), 'Unsaved room');
  assert.equal(await page.getByRole('button', { name: 'Reload current room settings' }).isVisible(), true);
  assert.equal(await page.getByRole('button', { name: 'Save room', exact: true }).isEnabled(), true, 'Busy controls must unlock after a failed save');
  assert.deepEqual(errors, []);
}));

test('browser authentication clears the token and shows a mobile layout without overflow', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  let authenticated = false;
  let token;
  const room = roomValue({ speakers: [{ id: '101', name: 'Kitchen speaker', protocol: 'airplay2', offset_ms: 0 }] });
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.endsWith('/session') && request.method() === 'POST') { token = request.postDataJSON().token; authenticated = true; await route.fulfill({ json: { ok: true } }); return; }
    if (url.pathname.endsWith('/session') && request.method() === 'DELETE') { authenticated = false; await route.fulfill({ json: { ok: true } }); return; }
    if (!authenticated) { await route.fulfill({ status: 401, json: { error: 'Sign in', code: 'unauthorized' } }); return; }
    await route.fulfill({ json: url.pathname.endsWith('/events') ? { events: [] } : snapshot([room], false) });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base);
  await page.getByRole('heading', { name: 'Connect to Shiri' }).waitFor();
  await page.getByLabel('Access token').fill('test-secret-never-stored');
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  await page.getByRole('heading', { name: 'Living room', exact: true }).waitFor();
  assert.equal(token, 'test-secret-never-stored');
  assert.equal(await page.locator('#pairing-token').inputValue(), '');
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.getByRole('button', { name: 'Sign out' }).click();
  await page.getByRole('heading', { name: 'Connect to Shiri' }).waitFor();
  assert.equal(await page.locator('.room-card').count(), 0);
  assert.deepEqual(errors, []);
}));
