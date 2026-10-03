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
    browser = await playwright.chromium.launch({ headless: true, args: ['--mute-audio'], ...(process.env.SHIRI_CHROMIUM_EXECUTABLE ? { executablePath: process.env.SHIRI_CHROMIUM_EXECUTABLE } : {}) });
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

function localDevice(overrides = {}) {
  return { selection_id: 'a'.repeat(64), label: 'Kitchen USB speaker', can_bind_by: ['serial', 'port'],
    bindings: [{ binding: 'serial', device: null }, { binding: 'port', device: null }], ...overrides };
}

function ttsCatalog(overrides = {}) {
  return { enabled: true, worker: { state: 'ready', model_id: 'qwen' }, models: [
    { id: 'qwen', name: 'Qwen streaming voices', voices: ['ryan', 'serena'], languages: ['English', 'French'], default_voice: 'ryan', default_language: 'English', streaming: 'incremental', supports_speed: false },
    { id: 'kokoro', name: 'Kokoro presets', voices: ['af_heart', 'am_adam'], languages: ['a', 'b'], default_voice: 'af_heart', default_language: 'a', streaming: 'phrase', supports_speed: true },
  ], ...overrides };
}

function speechReadyRoom() {
  return roomValue({ speakers: [{ id: '101', name: 'Kitchen speaker', protocol: 'airplay2', balance_percent: 100, offset_ms: 0 }],
    outputs: [{ id: '101', name: 'Kitchen speaker', protocol: 'airplay2', selected: true, available: true, assignable: true, offset_ms: 0, sync_quality: 'native' }] });
}

test('speech availability explains an unconfigured worker without changing room audio', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const writes = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() !== 'GET') writes.push(url.pathname);
    if (url.pathname.endsWith('/tts/models')) { await route.fulfill({ json: { enabled: false, worker: { state: 'stopped' }, models: [] } }); return; }
    await route.fulfill({ json: snapshot([roomValue()], false) });
  });
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speech voices', exact: true }).click();
  await page.locator('#tts-worker-status').filter({ hasText: 'not configured' }).waitFor();
  assert.equal(await page.locator('#tts-benchmark').isDisabled(), true);
  assert.equal(await page.locator('#tts-text').isDisabled(), true);
  assert.equal(await page.locator('#tts-speak').isVisible(), false);
  assert.deepEqual(writes, []);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
}));

test('quiet model benchmark has one explicit admission and shows generation metrics without acoustic claims', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const writes = [], reads = [];
  let identifier;
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/tts/models')) { await route.fulfill({ json: ttsCatalog() }); return; }
    if (url.pathname.endsWith('/tts/benchmark')) {
      const body = request.postDataJSON(); identifier = body.request_id; writes.push({ path: url.pathname, body });
      await route.fulfill({ status: 202, json: { id: identifier, state: 'queued', kind: 'benchmark', room_id: null, metrics: {} } }); return;
    }
    if (url.pathname.includes('/tts/jobs/')) {
      reads.push(url.pathname);
      await route.fulfill({ json: { id: identifier, state: 'completed', kind: 'benchmark', room_id: null,
        metrics: { first_pcm_ms: 22.4, first_non_silent_pcm_ms: 220.6, leading_silence_ms: 180, total_ms: 300, audio_duration_s: 1.2, realtime_factor: .25 } } }); return;
    }
    if (request.method() !== 'GET') writes.push({ path: url.pathname });
    await route.fulfill({ json: snapshot([speechReadyRoom()], false) });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speech voices', exact: true }).click();
  await page.locator('#tts-benchmark:not(:disabled)').waitFor();
  await page.locator('#tts-text').fill('A quiet test of voice generation.');
  await page.locator('#tts-benchmark').click();
  await page.locator('#tts-job-status').filter({ hasText: 'completed' }).waitFor();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, '/api/v1/tts/benchmark');
  assert.equal(writes[0].body.text, 'A quiet test of voice generation.');
  assert.match(identifier, /^[0-9a-f]{32}$/);
  assert.equal(writes[0].body.language, 'English');
  assert.equal(writes[0].body.speed, undefined);
  assert.equal(reads.every((path) => path.endsWith(identifier)), true);
  const metrics = await page.locator('#tts-metrics').textContent();
  assert.match(metrics, /First non-silent generated audio220.6 ms/);
  assert.equal(metrics.includes('Outputs connected'), false);
  assert.match(await page.locator('#tts-metrics-help').textContent(), /No room audio was sent/);
  assert.deepEqual(errors, []);
}));

test('room speech sends text only to its explicit room and cancels the exact admitted job', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const room = speechReadyRoom(), writes = [];
  let identifier;
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/tts/models')) { await route.fulfill({ json: ttsCatalog() }); return; }
    if (url.pathname === `/api/v1/rooms/${room.id}/tts`) {
      const body = request.postDataJSON(); identifier = body.request_id; writes.push({ path: url.pathname, body });
      await route.fulfill({ status: 202, json: { id: identifier, kind: 'speech', room_id: room.id, state: 'playing', metrics: { first_worker_pcm_received_ms: 27, room_admission_ms: 28, backend_ready_ms: 18, delivered_audio_s: .02 } } }); return;
    }
    if (url.pathname.includes('/tts/jobs/')) {
      if (request.method() === 'DELETE') writes.push({ path: url.pathname, method: 'DELETE' });
      await route.fulfill({ json: { id: identifier, kind: 'speech', room_id: room.id, state: request.method() === 'DELETE' ? 'cancelled' : 'playing', metrics: {} } }); return;
    }
    if (request.method() !== 'GET') writes.push({ path: url.pathname, method: request.method() });
    await route.fulfill({ json: snapshot([room], false) });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speak', exact: true }).click();
  await page.locator('#tts-speak:not(:disabled)').waitFor();
  await page.locator('#tts-text').fill('This reply belongs in the living room.');
  await page.locator('#tts-speak').click();
  await page.locator('#tts-job-status').filter({ hasText: 'Sending speech' }).waitFor();
  const progress = await page.locator('#tts-metrics').textContent();
  assert.match(progress, /First audio received27.0 ms/);
  assert.match(progress, /First audio sent to room28.0 ms/);
  assert.match(progress, /Delivery and cleanup durationNot measured/);
  assert.match(await page.locator('#tts-metrics-help').textContent(), /it is not the time to first sound/);
  await page.locator('#tts-cancel').click();
  await page.locator('#tts-job-status').filter({ hasText: 'Job stopped' }).waitFor();
  assert.equal(writes.length, 2);
  assert.equal(writes[0].path, `/api/v1/rooms/${room.id}/tts`);
  assert.equal(writes[1].path, `/api/v1/tts/jobs/${identifier}`);
  assert.equal(writes[1].method, 'DELETE');
  assert.equal(await page.locator('#tts-cancel').isDisabled(), true);
  assert.deepEqual(errors, []);
}));

test('model selection requires loading and resets model-specific voice language and speed controls', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  let loaded = 'qwen';
  const writes = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/tts/models')) { await route.fulfill({ json: ttsCatalog({ worker: { state: 'ready', model_id: loaded } }) }); return; }
    if (url.pathname.endsWith('/tts/models/load')) {
      const body = request.postDataJSON(); writes.push({ path: url.pathname, body }); loaded = body.model_id;
      await route.fulfill({ status: 202, json: { state: 'loading', model_id: loaded } }); return;
    }
    if (url.pathname.endsWith('/tts/benchmark')) {
      const body = request.postDataJSON(); writes.push({ path: url.pathname, body });
      await route.fulfill({ status: 202, json: { id: body.request_id, state: 'completed', kind: 'benchmark', room_id: null, metrics: {} } }); return;
    }
    await route.fulfill({ json: snapshot([roomValue()], false) });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speech voices', exact: true }).click();
  await page.locator('#tts-benchmark:not(:disabled)').waitFor();
  await page.locator('#tts-model').selectOption('kokoro');
  assert.equal(await page.locator('#tts-benchmark').isDisabled(), true);
  assert.equal(await page.locator('#tts-language').inputValue(), 'a');
  assert.equal(await page.locator('#tts-voice').inputValue(), 'af_heart');
  assert.equal(await page.locator('#tts-speed-field').isVisible(), true);
  assert.match(await page.locator('#tts-model-help').textContent(), /first phrase/);
  await page.locator('#tts-load-model').click();
  await page.locator('#tts-benchmark:not(:disabled)').waitFor();
  await page.locator('#tts-speed').selectOption('1.25');
  await page.locator('#tts-benchmark').click();
  await page.locator('#tts-job-status').filter({ hasText: 'completed' }).waitFor();
  assert.deepEqual(writes[0].body, { model_id: 'kokoro' });
  assert.equal(writes[1].body.model_id, 'kokoro');
  assert.equal(writes[1].body.voice, 'af_heart');
  assert.equal(writes[1].body.language, 'a');
  assert.equal(writes[1].body.speed, 1.25);
  assert.deepEqual(errors, []);
}));

function previewWav() {
  const rate = 16000, frames = rate * 2;
  const data = Buffer.alloc(44 + frames * 2);
  data.write('RIFF', 0); data.writeUInt32LE(data.length - 8, 4); data.write('WAVEfmt ', 8);
  data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(1, 22);
  data.writeUInt32LE(rate, 24); data.writeUInt32LE(rate * 2, 28); data.writeUInt16LE(2, 32); data.writeUInt16LE(16, 34);
  data.write('data', 36); data.writeUInt32LE(frames * 2, 40);
  return data;
}

test('completed quiet voice preview waits for browser playback and retires its source on close or new work', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const writes = [], samples = [];
  let identifier, nextState = 'completed';
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/tts/models')) { await route.fulfill({ json: ttsCatalog() }); return; }
    if (url.pathname.endsWith('/tts/benchmark')) {
      const body = request.postDataJSON(); identifier = body.request_id; writes.push(url.pathname);
      await route.fulfill({ status: 202, json: { id: identifier, state: nextState, kind: 'benchmark', room_id: null,
        sample_available: nextState === 'completed', sample_url: 'https://external.invalid/untrusted.wav', metrics: {} } }); return;
    }
    if (url.pathname.endsWith('/sample.wav')) {
      samples.push(url.pathname);
      await route.fulfill({ contentType: 'audio/wav', body: previewWav() }); return;
    }
    if (url.pathname.includes('/tts/jobs/')) {
      if (request.method() === 'DELETE') writes.push(url.pathname);
      await route.fulfill({ json: { id: identifier, state: request.method() === 'DELETE' ? 'cancelled' : nextState,
        kind: 'benchmark', room_id: null, sample_available: nextState === 'completed', metrics: {} } }); return;
    }
    if (request.method() !== 'GET') writes.push(url.pathname);
    await route.fulfill({ json: snapshot([speechReadyRoom()], false) });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speech voices', exact: true }).click();
  await page.locator('#tts-benchmark:not(:disabled)').waitFor();
  await page.locator('#tts-benchmark').click();
  await page.locator('#tts-preview').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#tts-audio').getAttribute('preload'), 'none');
  assert.equal(await page.locator('#tts-audio').getAttribute('autoplay'), null);
  assert.equal(await page.locator('#tts-audio').evaluate((audio) => audio.paused), true);
  assert.equal(await page.locator('#tts-audio').getAttribute('src'), `/api/v1/tts/jobs/${identifier}/sample.wav`);
  assert.deepEqual(samples, []);
  assert.match(await page.locator('#tts-preview').textContent(), /plays on this browser, not the room speakers/);
  await page.locator('#tts-audio').evaluate((audio) => audio.play());
  assert.equal(samples.length, 1);
  assert.equal(await page.locator('#tts-audio').evaluate((audio) => audio.paused), false);
  await page.getByRole('button', { name: 'Close speech voices' }).click();
  assert.equal(await page.locator('#tts-audio').getAttribute('src'), null);
  assert.equal(await page.locator('#tts-audio').evaluate((audio) => audio.paused), true);

  await page.getByRole('button', { name: 'Speech voices', exact: true }).click();
  await page.locator('#tts-preview').waitFor({ state: 'visible' });
  nextState = 'generating';
  await page.locator('#tts-benchmark').click();
  await page.locator('#tts-job-status').filter({ hasText: 'Generating speech' }).waitFor();
  assert.equal(await page.locator('#tts-audio').getAttribute('src'), null);
  assert.equal(await page.locator('#tts-preview').isVisible(), false);
  await page.locator('#tts-cancel').click();
  await page.locator('#tts-job-status').filter({ hasText: 'Job stopped' }).waitFor();
  assert.equal(await page.locator('#tts-audio').getAttribute('src'), null);
  assert.equal(writes.filter((path) => path.includes('/rooms/')).length, 0);
  assert.deepEqual(errors, []);
}));

test('expired preview reports unavailability and does not refetch automatically during model polling', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  let identifier, samples = 0;
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/tts/models')) { await route.fulfill({ json: ttsCatalog() }); return; }
    if (url.pathname.endsWith('/tts/benchmark')) {
      identifier = request.postDataJSON().request_id;
      await route.fulfill({ status: 202, json: { id: identifier, state: 'completed', kind: 'benchmark', room_id: null, sample_available: true, metrics: {} } }); return;
    }
    if (url.pathname.endsWith('/sample.wav')) { samples += 1; await route.fulfill({ status: 404, json: { error: 'Sample expired' } }); return; }
    await route.fulfill({ json: snapshot([roomValue()], false) });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speech voices', exact: true }).click();
  await page.locator('#tts-benchmark:not(:disabled)').waitFor();
  await page.locator('#tts-benchmark').click();
  await page.locator('#tts-preview').waitFor({ state: 'visible' });
  await page.locator('#tts-audio').evaluate((audio) => audio.play().catch(() => {}));
  await page.locator('#tts-preview-error').filter({ hasText: 'no longer available' }).waitFor();
  await page.locator('#tts-refresh').click();
  assert.equal(await page.locator('#tts-audio').getAttribute('src'), null);
  assert.equal(samples, 1);
  assert.deepEqual(errors, []);
}));

test('experimental model warning follows selection and a stopped worker uses its configured default', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const catalog = ttsCatalog(); catalog.models[0].experimental = true;
  catalog.worker = { state: 'stopped' }; catalog.default_model_id = 'kokoro';
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    await route.fulfill({ json: url.pathname.endsWith('/tts/models') ? catalog : snapshot([roomValue()], false) });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speech voices', exact: true }).click();
  await page.locator('#tts-model:not(:disabled)').waitFor();
  assert.equal(await page.locator('#tts-model').inputValue(), 'kokoro');
  assert.equal(await page.locator('#tts-model-warning').isVisible(), false);
  await page.locator('#tts-model').selectOption('qwen');
  assert.equal(await page.locator('#tts-model-warning').isVisible(), true);
  assert.match(await page.locator('#tts-model-warning').textContent(), /Experimental voice model/);
  await page.locator('#tts-model').selectOption('kokoro');
  assert.equal(await page.locator('#tts-model-warning').isVisible(), false);
  assert.deepEqual(errors, []);
}));

test('local speaker enrollment happens on Save and failed binding never publishes room changes', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const value = roomValue({ enabled: false, runtime: { status: 'stopped', error: null } });
  const device = 'shiri:device=12345678-0000-4000-8000-000000000002';
  let binds = 0, writes = 0;
  const calls = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/local-devices')) { await route.fulfill({ json: { devices: [localDevice({ label: 'Kitchen <USB> speaker', bindings: [
      { binding: 'serial', device: null }, { binding: 'port', device: value.local_audio_device },
    ] })] } }); return; }
    if (url.pathname.endsWith('/local-devices/bind')) {
      binds += 1; calls.push('bind');
      assert.deepEqual(request.postDataJSON(), { selection_id: 'a'.repeat(64), binding: 'port', conversion: true });
      if (binds === 1) { await route.fulfill({ status: 409, json: { error: 'Speaker moved; refresh and choose it again', code: 'hardware_changed' } }); return; }
      await route.fulfill({ status: 201, json: { device, label: 'Kitchen USB speaker', binding: 'port' } }); return;
    }
    if (request.method() === 'PATCH') {
      writes += 1; calls.push('room');
      const body = request.postDataJSON();
      assert.equal(body.changes.local_audio_device, device);
      if (writes === 1) { await route.fulfill({ status: 409, json: { error: 'Room name already in use', code: 'conflict' } }); return; }
      Object.assign(value, body.changes, { revision: 2 });
      await route.fulfill({ json: { room: value, runtime_accepted: true } }); return;
    }
    await route.fulfill({ json: url.pathname.endsWith('/events') ? { events: [] } : snapshot([value], false) });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base);
  await page.getByRole('button', { name: 'Room settings' }).click();
  await page.locator('#local-speaker option').filter({ hasText: 'Kitchen <USB> speaker' }).waitFor({ state: 'attached' });
  await page.getByLabel('Local speaker', { exact: false }).selectOption('a'.repeat(64));
  await page.getByLabel('Identify this speaker by').selectOption('port');
  assert.match(await page.locator('#local-binding-help').innerText(), /Moving the speaker to another port requires/);
  assert.equal(binds, 0);
  assert.equal(await page.locator('USB').count(), 0, 'Device labels must be rendered as text');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.getByRole('button', { name: 'Save room', exact: true }).click();
  await page.locator('#room-form-error').filter({ hasText: 'Speaker moved' }).waitFor();
  assert.equal(writes, 0, 'A rejected binding must leave room configuration untouched');
  assert.equal(await page.getByLabel('Identify this speaker by').inputValue(), 'port');
  await page.getByRole('button', { name: 'Save room', exact: true }).click();
  await page.locator('#room-form-error').filter({ hasText: 'Room name already in use' }).waitFor();
  await page.getByRole('button', { name: 'Save room', exact: true }).click();
  await page.locator('#room-dialog').waitFor({ state: 'hidden' });
  assert.equal(binds, 2, 'The confirmed enrollment is reused when only the room save failed');
  assert.deepEqual(calls, ['bind', 'bind', 'room', 'room']);
  assert.equal(value.local_audio_device, device);
  await page.getByRole('button', { name: 'Room settings' }).click();
  await page.waitForFunction(() => document.querySelector('#local-speaker').value === 'a'.repeat(64));
  assert.equal(await page.locator('#local-speaker-binding').inputValue(), 'port');
  await page.getByRole('button', { name: 'Save room', exact: true }).click();
  await page.locator('#room-dialog').waitFor({ state: 'hidden' });
  assert.equal(binds, 2, 'An unchanged discovered saved speaker must not be enrolled again');
  assert.deepEqual(errors, []);
}));

test('an unavailable saved local speaker and manual Bluetooth setup survive discovery failure', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const saved = 'shiri:device=12345678-0000-4000-8000-000000000003';
  const value = roomValue({ local_audio_device: saved });
  let binds = 0;
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/local-devices')) { await route.fulfill({ status: 503, json: { error: 'Audio inventory unavailable', code: 'unavailable' } }); return; }
    if (url.pathname.endsWith('/local-devices/bind')) { binds += 1; await route.fulfill({ status: 500, json: { error: 'Unexpected bind' } }); return; }
    if (request.method() === 'PATCH') {
      const body = request.postDataJSON();
      Object.assign(value, body.changes, { revision: value.revision + 1 });
      await route.fulfill({ json: { room: value, runtime_accepted: true } }); return;
    }
    await route.fulfill({ json: url.pathname.endsWith('/events') ? { events: [] } : snapshot([value], false) });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Room settings' }).click();
  await page.locator('#local-speaker-status').filter({ hasText: 'Your saved speaker is retained' }).waitFor();
  assert.equal(await page.locator('#local-speaker').inputValue(), 'saved');
  assert.equal(await page.locator('#local-audio-device').inputValue(), '', 'Opaque IDs stay out of the manual field');
  assert.equal(await page.locator('#local-speaker').innerText(), 'Network speakers only\nSaved local speaker (not currently available)\nAdvanced Bluetooth or existing device');
  await page.getByLabel('Room name', { exact: true }).fill('Renamed room');
  await page.getByRole('button', { name: 'Save room', exact: true }).click();
  await page.locator('#room-dialog').waitFor({ state: 'hidden' });
  assert.equal(value.local_audio_device, saved);
  await page.getByRole('button', { name: 'Room settings' }).click();
  await page.locator('#local-speaker').selectOption('advanced');
  const bluetooth = 'bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp';
  await page.getByLabel('Local or Bluetooth audio device', { exact: false }).fill(bluetooth);
  await page.getByRole('button', { name: 'Save room', exact: true }).click();
  await page.locator('#room-dialog').waitFor({ state: 'hidden' });
  assert.equal(value.local_audio_device, bluetooth);
  assert.equal(binds, 0);
  assert.deepEqual(errors, []);
}));

test('late local discovery cannot replace another room draft or a newer inventory choice', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const first = roomValue(), second = roomValue({ id: '00000000-0000-4000-8000-000000000002', name: 'Bedroom' });
  const pending = [];
  let firstStarted, thirdStarted;
  const firstRequest = new Promise((resolve) => { firstStarted = resolve; });
  const thirdRequest = new Promise((resolve) => { thirdStarted = resolve; });
  let inventories = 0;
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/local-devices')) {
      inventories += 1;
      if (inventories === 1 || inventories === 3) { pending.push(route); (inventories === 1 ? firstStarted : thirdStarted)(); return; }
      await route.fulfill({ json: { devices: [localDevice({ label: 'Current USB speaker' })] } }); return;
    }
    await route.fulfill({ json: url.pathname.endsWith('/events') ? { events: [] } : snapshot([first, second], false) });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Room settings' }).first().click();
  await firstRequest;
  await page.getByRole('button', { name: 'Close room setup' }).click();
  await page.getByRole('button', { name: 'Room settings' }).last().click();
  await page.locator('#local-speaker option').filter({ hasText: 'Current USB speaker' }).waitFor({ state: 'attached' });
  await page.locator('#local-speaker').selectOption('a'.repeat(64));
  await pending[0].fulfill({ json: { devices: [localDevice({ selection_id: 'b'.repeat(64), label: 'Old room USB speaker' })] } });
  await page.waitForTimeout(30);
  assert.equal(await page.getByLabel('Room name', { exact: true }).inputValue(), 'Bedroom');
  assert.equal(await page.locator('#local-speaker').inputValue(), 'a'.repeat(64));
  assert.equal(await page.locator('#local-speaker option').filter({ hasText: 'Old room USB speaker' }).count(), 0);
  await page.getByRole('button', { name: 'Refresh devices' }).click();
  await thirdRequest;
  await page.getByRole('button', { name: 'Refresh devices' }).click();
  await page.locator('#local-speaker-status').filter({ hasText: 'This speaker is verified' }).waitFor();
  await page.getByLabel('Identify this speaker by').selectOption('port');
  await pending[1].fulfill({ json: { devices: [localDevice({ selection_id: 'c'.repeat(64), label: 'Stale refresh speaker' })] } });
  await page.waitForTimeout(30);
  assert.equal(await page.locator('#local-speaker').inputValue(), 'a'.repeat(64));
  assert.equal(await page.locator('#local-speaker-binding').inputValue(), 'port');
  assert.equal(await page.locator('#local-speaker option').filter({ hasText: 'Stale refresh speaker' }).count(), 0);
  assert.deepEqual(errors, []);
}));

test('an invalid local enrollment acknowledgment cannot become a saved room assignment', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const expected = 'shiri:device=12345678-0000-4000-8000-000000000005';
  const candidate = localDevice({ bindings: [{ binding: 'serial', device: expected }, { binding: 'port', device: null }] });
  const invalid = [
    { device: expected.replaceAll('-', ''), label: 'Kitchen USB speaker', binding: 'serial' },
    { device: expected, label: '', binding: 'serial' },
    { device: expected, label: 'Kitchen USB speaker', binding: 'port' },
    { device: expected.replace(/5$/, '6'), label: 'Kitchen USB speaker', binding: 'serial' },
  ];
  let binds = 0, writes = 0;
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith('/local-devices')) { await route.fulfill({ json: { devices: [candidate] } }); return; }
    if (url.pathname.endsWith('/local-devices/bind')) { await route.fulfill({ json: invalid[binds++] }); return; }
    if (request.method() === 'PATCH') writes += 1;
    await route.fulfill({ json: url.pathname.endsWith('/events') ? { events: [] } : snapshot([roomValue()], false) });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Room settings' }).click();
  await page.locator('#local-speaker option').filter({ hasText: 'Kitchen USB speaker' }).waitFor({ state: 'attached' });
  await page.locator('#local-speaker').selectOption('a'.repeat(64));
  for (let index = 0; index < invalid.length; index += 1) {
    await page.getByRole('button', { name: 'Save room', exact: true }).click();
    await page.locator('#room-form-error').filter({ hasText: 'did not confirm' }).waitFor();
    await page.waitForFunction(() => !document.querySelector('#save-room').disabled);
  }
  assert.equal(binds, invalid.length);
  assert.equal(writes, 0);
  assert.equal(await page.locator('#local-speaker').inputValue(), 'a'.repeat(64));
  assert.deepEqual(errors, []);
}));

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

test('calibration imports PCM, requires room off, preserves conflicts and reviews verification/rollback', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const value = roomValue({ speakers: [{ id: '101', name: 'Reference', protocol: 'airplay2', offset_ms: 0 }, { id: '202', name: 'Target', protocol: 'chromecast', offset_ms: 0 }] });
  for (const output of value.outputs) output.selected = true;
  const current = snapshot([value], false);
  current.capabilities.calibration_analysis = true;
  let session = null, imports = 0, verifies = 0, conflict = true;
  const binary = Buffer.from('RIFF-private-fixture');
  const analysis = (accepted = 0) => ({ status: accepted >= 21 ? 'candidate_correction' : 'insufficient_evidence', reasons: accepted >= 21 ? [] : ['Need three fresh recordings'], accepted_markers: accepted, rejected_markers: 0, median_lag_ms: accepted ? 23 : null, mad_ms: 0, p05_ms: 23, p95_ms: 23, minimum_confidence: .99, observed_uncertainty_ms: .021, drift_estimates: [], candidate_offset_ms: accepted >= 21 ? -23 : null });
  const record = () => ({ sample_rate: 48000, pcm_sha256: 'private-pcm-hash', environment_at_import: { simulation: false }, markers: Array.from({ length: 8 }, (_, marker) => ({ marker, accepted: true })) });
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    const method = request.method();
    const json = method === 'GET' || method === 'DELETE' || url.pathname.endsWith('/recordings') ? null : request.postDataJSON();
    let data;
    if (url.pathname.endsWith('/state')) data = current;
    else if (url.pathname.endsWith('/events')) data = { events: [] };
    else if (url.pathname.endsWith('/calibration') && method === 'GET') data = { sessions: session ? [session] : [] };
    else if (url.pathname.endsWith('/calibration') && method === 'POST') {
      const { id, name, airplay_name, nobly_room_id, interface: network, local_audio_device, duck_gain, speakers } = value;
      session = { id: 'calibration-1', generation: 1, created_at: Date.now()/1000, room_id: value.id, target_id: json.target_id, reference_id: json.reference_id, room_revision: value.revision, status: 'insufficient_evidence', capture_device: json.capture_device, geometry: json.geometry, previous_offset_ms: 0, applied_offset_ms: null, applied_revision: null, configuration: structuredClone({ id, name, airplay_name, nobly_room_id, interface: network, local_audio_device, duck_gain, speakers }), result: analysis(), recordings: [], verification_recordings: [], verification_backend: [], verification: null };
      data = session;
    } else if (url.pathname.endsWith('/recordings')) {
      assert.equal(request.headers()['content-type'], 'audio/wav');
      assert.deepEqual(request.postDataBuffer(), binary, 'Import must not JSON/base64-wrap microphone PCM');
      if (url.searchParams.get('verification') === 'true') {
        verifies += 1;
        session.verification_recordings.push(record());
        session.verification_backend.push({ simulation: false });
        session.verification = { ...analysis(verifies * 7), median_lag_ms: 0, p05_ms: 0, p95_ms: 0 };
        if (verifies === 3) session.status = 'measured_at_this_setup';
      } else {
        imports += 1; session.recordings.push(record()); session.result = analysis(imports * 7);
        session.status = session.result.status;
      }
      session.generation += 1;
      data = session;
    } else if (url.pathname.endsWith('/apply')) {
      assert.equal(value.enabled, false);
      assert.equal(json.expected_revision, value.revision);
      assert.equal(json.expected_generation, session.generation);
      if (conflict) {
        conflict = false; value.volume = 70; value.revision += 1;
        await route.fulfill({ status: 409, json: { error: 'Room changed; review current revision', code: 'conflict' } }); return;
      }
      value.speakers[1].offset_ms = -23; value.revision += 1;
      session.applied_offset_ms = -23; session.applied_revision = value.revision; session.status = 'offset_saved'; session.generation += 2;
      data = { room: value, runtime_accepted: true, calibration: session };
    } else if (url.pathname.endsWith('/rollback')) {
      assert.equal(value.enabled, false); assert.equal(json.expected_revision, value.revision);
      value.speakers[1].offset_ms = 0; value.revision += 1; session.status = 'rolled_back'; session.generation += 2;
      data = { room: value, runtime_accepted: true, calibration: session };
    } else if (method === 'PATCH') {
      assert.equal(json.expected_revision, value.revision);
      Object.assign(value, json.changes); value.revision += 1; value.runtime.status = value.enabled ? 'running' : 'stopped';
      data = { room: value, runtime_accepted: true };
    } else if (method === 'DELETE') { session = null; data = { ok: true }; }
    else data = session;
    await route.fulfill({ json: data });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speakers', exact: true }).click();
  await page.getByRole('button', { name: 'Measure speaker timing' }).click();
  await page.getByLabel('Capture device', { exact: true }).fill('<script>private ADC</script>');
  await page.getByLabel('Microphone placement and test conditions').fill('Matched distances, shared clock');
  await page.getByRole('button', { name: 'Start measurement session' }).click();
  await page.getByText('More reliable recordings needed', { exact: true }).waitFor();
  for (let take = 0; take < 3; take += 1) {
    await page.getByLabel('Stereo WAV recording').setInputFiles({ name: 'take.wav', mimeType: 'audio/wav', buffer: binary });
    await page.getByRole('button', { name: 'Analyze recording', exact: true }).click();
    await page.waitForFunction((count) => document.querySelector('#calibration-result').textContent.includes(`Take ${count}:`), take + 1);
  }
  assert.equal(imports, 3);
  assert.equal(await page.getByRole('button', { name: 'Save candidate delay' }).isDisabled(), true);
  assert.equal(value.speakers[1].offset_ms, 0);
  assert.equal(await page.locator('#calibration-result script').count(), 0);
  await page.getByRole('button', { name: 'Turn room off for timing change' }).click();
  await page.getByRole('button', { name: 'Save candidate delay' }).click();
  await page.getByText('Room changed; review current revision', { exact: true }).last().waitFor();
  assert.equal(value.speakers[1].offset_ms, 0, 'Conflict must retain the reviewed candidate without pretending to apply it');
  await page.getByRole('button', { name: 'Save candidate delay' }).click();
  await page.getByText('Delay saved · Verification needed', { exact: true }).waitFor();
  assert.equal(value.speakers[1].offset_ms, -23);
  assert.equal(await page.getByRole('button', { name: 'Analyze fresh verification' }).isDisabled(), true);
  await page.getByRole('button', { name: 'Turn room on for verification' }).click();
  for (let take = 0; take < 3; take += 1) {
    await page.getByLabel('Stereo WAV recording').setInputFiles({ name: 'fresh.wav', mimeType: 'audio/wav', buffer: binary });
    await page.getByRole('checkbox', { name: 'This is a fresh post-change recording', exact: false }).check();
    await page.getByRole('button', { name: 'Analyze fresh verification' }).click();
    await page.waitForFunction((count) => document.querySelector('#calibration-result').textContent.includes(`Take ${count}:`), take + 4);
  }
  await page.getByText('Alignment measured at this setup', { exact: true }).waitFor();
  assert.equal(verifies, 3);
  assert.equal(await page.getByRole('link', { name: 'Export evidence' }).getAttribute('href'), `/api/v1/rooms/${value.id}/calibration/calibration-1/export`);
  // A fresh server observation of stable but wrong residual alignment must
  // keep the existing offset and offer investigation/rollback, never another
  // automatically applied candidate.
  session.status = 'verification_failed'; session.verification.median_lag_ms = 5;
  await page.getByRole('button', { name: 'Refresh measurement' }).click();
  await page.getByText('Correction did not align recorded arrivals', { exact: true }).waitFor();
  assert.equal(value.speakers[1].offset_ms, -23);
  assert.equal(await page.getByRole('button', { name: 'Save candidate delay' }).isDisabled(), true);
  await page.getByRole('button', { name: 'Turn room off for timing change' }).click();
  await page.getByRole('button', { name: 'Restore previous delay' }).click();
  await page.getByText('Previous delay restored', { exact: true }).waitFor();
  assert.equal(value.speakers[1].offset_ms, 0);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  // A fresh browser has no in-memory receipts. It must resume the retained
  // session through the authenticated history endpoint even without analysis.
  current.capabilities.calibration_analysis = false;
  await page.reload();
  await page.getByRole('button', { name: 'Speakers' }).click();
  await page.getByRole('button', { name: 'Measure speaker timing' }).click();
  await page.getByText('Previous delay restored', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('Saved measurements in this room').inputValue(), 'calibration-1');
  await page.getByRole('button', { name: 'Start another measurement' }).click();
  assert.equal(await page.getByRole('button', { name: 'Start measurement session' }).isDisabled(), true);
  await page.getByLabel('Saved measurements in this room').selectOption('calibration-1');
  await page.getByText('Previous delay restored', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Delete measurement evidence' }).click();
  await page.getByRole('button', { name: 'Start measurement session' }).waitFor();
  assert.deepEqual(errors, []);
}));

test('retained cross-zone verification preserves historical runtime scope after browser and runtime changes', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const target = roomValue({ name: 'Target zone', local_audio_device: 'shiri:device=12345678-0000-4000-8000-000000000003',
    speakers: [{ id: '0', name: 'Target local output', protocol: 'alsa', offset_ms: -23 }], revision: 6 });
  const reference = roomValue({ id: '00000000-0000-4000-8000-000000000002', name: 'Reference zone',
    local_audio_device: 'shiri:device=12345678-0000-4000-8000-000000000004',
    speakers: [{ id: '0', name: 'Reference local output', protocol: 'alsa', offset_ms: 0 }], revision: 3 });
  const configuration = (room) => Object.fromEntries(['id', 'name', 'airplay_name', 'interface', 'nobly_room_id', 'local_audio_device', 'duck_gain', 'speakers'].map((key) => [key, structuredClone(room[key])]));
  const baseline = configuration(target); baseline.speakers[0].offset_ms = 0;
  const record = () => ({ sample_rate: 48000, pcm_sha256: 'retained-private-pcm', environment_at_import: { simulation: false },
    markers: Array.from({ length: 8 }, (_, marker) => ({ marker, accepted: true })) });
  const retained = { id: 'retained-cross-zone', room_id: target.id, reference_room_id: reference.id,
    target_id: '0', reference_id: '0', generation: 9, created_at: 1760000000,
    room_revision: 1, reference_revision: 1, configuration: baseline, reference_configuration: configuration(reference),
    playback_context: 'Operator-declared native phone group', geometry: 'Declared shared ADC geometry', capture_device: 'Imported capture',
    status: 'measured_at_this_setup', applied_revision: 5, applied_offset_ms: -23, previous_offset_ms: 0,
    result: { status: 'candidate_correction', candidate_offset_ms: -23, reasons: [] },
    recordings: [record()], verification_recordings: [record()], verification_backend: [{ simulation: false }],
    verification: { status: 'stable_measurement', median_lag_ms: 0, reasons: [], drift_estimates: [] },
  };
  let session, current, mutations = 0;
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() !== 'GET') mutations += 1;
    await route.fulfill({ json: url.pathname.endsWith('/calibration') ? { sessions: [session] }
      : url.pathname.endsWith('/events') ? { events: [] } : current });
  });
  const cases = [
    { name: 'simulated backend survives a real runtime', modify: (saved) => { saved.verification_backend[0].simulation = true; },
      label: 'Recorded alignment verified · Simulated observations', historicalSimulation: true },
    { name: 'simulated baseline import survives real verification readback', modify: (saved) => { saved.recordings[0].environment_at_import.simulation = true; },
      label: 'Recorded alignment verified · Simulated observations', historicalSimulation: true },
    { name: 'simulated verification import is retained', modify: (saved) => { saved.verification_recordings[0].environment_at_import.simulation = true; },
      label: 'Recorded alignment verified · Simulated observations', historicalSimulation: true },
    { name: 'legacy import without runtime observations remains unknown', modify: (saved) => { delete saved.recordings[0].environment_at_import; },
      label: 'Recorded alignment verified · Runtime evidence incomplete', incomplete: true },
    { name: 'missing verification backend remains unknown', modify: (saved) => { delete saved.verification_backend; },
      label: 'Recorded alignment verified · Runtime evidence incomplete', incomplete: true },
    { name: 'real retained observations are qualified independently', label: 'Alignment measured at this setup', good: true },
    { name: 'a simulated runtime keeps earlier real observations intact', runtimeSimulation: true,
      label: 'Recorded alignment verified · Current runtime simulated' },
    { name: 'switching back preserves the earlier real scope', label: 'Alignment measured at this setup', good: true },
  ];
  for (const example of cases) {
    session = structuredClone(retained); example.modify?.(session);
    current = snapshot([target, reference], !!example.runtimeSimulation);
    const before = structuredClone(session);
    await page.goto(base);
    await page.getByRole('button', { name: 'Speakers', exact: true }).first().click();
    await page.getByRole('button', { name: 'Measure speaker timing' }).click();
    await page.locator('#calibration-status').filter({ hasText: example.label }).waitFor();
    assert.equal(await page.locator('#calibration-status').textContent(), example.label, example.name);
    assert.equal(await page.locator('#calibration-status').getAttribute('class'), example.good ? 'notice good' : 'notice warning', example.name);
    const evidence = await page.locator('#calibration-result').textContent();
    assert.equal(evidence.includes('Retained simulated observations:'), !!example.historicalSimulation, example.name);
    assert.equal(evidence.includes('Retained runtime observations are incomplete.'), !!example.incomplete, example.name);
    assert.equal(evidence.includes('Current runtime simulation:'), !!example.runtimeSimulation, example.name);
    assert.match(evidence, /imported audio does not certify the phone’s native grouping/);
    assert.equal(await page.getByRole('link', { name: 'Export evidence' }).getAttribute('href'), `/api/v1/rooms/${target.id}/calibration/${retained.id}/export`);
    assert.deepEqual(session, before, 'Reviewing retained scope must not rewrite evidence');
    assert.equal(target.speakers[0].offset_ms, -23);
    assert.equal(reference.speakers[0].offset_ms, 0);
  }
  assert.equal(mutations, 0, 'Opening retained evidence must not apply, rollback, or rewrite a receipt');
  assert.deepEqual(errors, []);
}));

test('cross-zone calibration chooses a separate reference ID0 and preserves exact target changes', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const target = roomValue({ name: 'Bluetooth target', local_audio_device: 'bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp',
    speakers: [{ id: '0', name: 'Bluetooth speaker', protocol: 'alsa', offset_ms: 0 }] });
  const reference = roomValue({ id: '00000000-0000-4000-8000-000000000002', name: 'Wi-Fi reference',
    airplay_name: 'Wi-Fi reference', local_audio_device: 'hw:CARD=Reference,DEV=0', revision: 8,
    speakers: [{ id: '0', name: 'Wi-Fi zone speaker', protocol: 'alsa', offset_ms: 0 }] });
  const current = snapshot([target, reference]); current.capabilities.calibration_analysis = true;
  let session;
  const configuration = (room) => Object.fromEntries(['id', 'name', 'airplay_name', 'interface', 'nobly_room_id', 'local_audio_device', 'duck_gain', 'speakers'].map((key) => [key, structuredClone(room[key])]));
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(); const url = new URL(request.url()); const method = request.method();
    const body = method === 'GET' || method === 'DELETE' ? null : request.postDataJSON();
    let data;
    if (url.pathname.endsWith('/state')) data = current;
    else if (url.pathname.endsWith('/events')) data = { events: [] };
    else if (url.pathname.endsWith('/calibration') && method === 'GET') data = { sessions: session ? [session] : [] };
    else if (url.pathname.endsWith('/calibration') && method === 'POST') {
      assert.equal(body.reference_room_id, reference.id); assert.equal(body.expected_reference_revision, reference.revision);
      assert.equal(body.reference_id, '0'); assert.equal(body.target_id, '0');
      assert.equal(body.playback_context, 'Native iPhone group: both test zones');
      // This browser contract fixture starts with pre-reviewed synthetic
      // evidence; real waveform qualification is covered by Python tests.
      session = { id: 'cross-session', room_id: target.id, reference_room_id: reference.id,
        target_id: '0', reference_id: '0', generation: 4, created_at: Date.now() / 1000,
        room_revision: target.revision, reference_revision: reference.revision,
        configuration: configuration(target), reference_configuration: configuration(reference),
        playback_context: body.playback_context, geometry: body.geometry, capture_device: body.capture_device,
        status: 'candidate_correction', applied_revision: null, applied_offset_ms: null, previous_offset_ms: 0,
        recordings: [], verification_recordings: [], result: { status: 'candidate_correction', reasons: [], candidate_offset_ms: -23 } };
      data = session;
    } else if (method === 'PATCH') {
      assert.equal(url.pathname, `/api/v1/rooms/${target.id}`, 'Room-off action must not disable the reference zone');
      assert.equal(body.expected_revision, target.revision); Object.assign(target, body.changes); target.revision += 1;
      target.runtime.status = target.enabled ? 'running' : 'stopped'; data = { room: target, runtime_accepted: true };
    } else if (url.pathname.endsWith('/apply')) {
      assert.equal(body.expected_revision, target.revision); assert.equal(body.expected_generation, session.generation);
      assert.equal(body.expected_reference_revision, reference.revision); assert.equal(target.enabled, false);
      target.speakers[0].offset_ms = -23; target.revision += 1;
      session.applied_offset_ms = -23; session.applied_revision = target.revision; session.generation += 2; session.status = 'offset_saved';
      data = { room: target, runtime_accepted: true, calibration: session };
    } else data = session;
    await route.fulfill({ status: 200, json: data });
  });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speakers', exact: true }).first().click();
  await page.getByRole('button', { name: 'Measure speaker timing' }).click();
  assert.equal(await page.getByLabel('Reference zone', { exact: true }).inputValue(), reference.id);
  assert.equal(await page.getByLabel('Reference speaker · channel 1').inputValue(), '0');
  await page.getByLabel('Capture device', { exact: true }).fill('Shared USB ADC');
  await page.getByLabel('Microphone placement and test conditions').fill('Matched distances');
  await page.getByLabel('Common playback and grouping context').fill('Native iPhone group: both test zones');
  await page.getByRole('button', { name: 'Start measurement session' }).click();
  await page.getByText('Stable correction ready for review', { exact: true }).waitFor();
  assert.match(await page.locator('#calibration-playback-help').textContent(), /phone's native group/);
  await page.getByRole('button', { name: 'Turn room off for timing change' }).click();
  await page.getByRole('button', { name: 'Save candidate delay' }).click();
  await page.getByText('Delay saved · Verification needed', { exact: true }).waitFor();
  assert.equal(target.speakers[0].offset_ms, -23);
  assert.equal(reference.speakers[0].offset_ms, 0); assert.equal(reference.revision, 8); assert.equal(reference.enabled, true);
  assert.match(await page.locator('#calibration-result').textContent(), /Bluetooth target.*relative to.*Wi-Fi reference/);
  assert.deepEqual(errors, []);
}));


test('speaker balance is saved separately while the room keeps one master volume', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const value = roomValue({ speakers: [{ id: '101', name: 'Kitchen speaker', protocol: 'airplay2', offset_ms: 0, balance_percent: 100 }] });
  value.outputs[0].selected = true;
  value.outputs[0].balance_percent = 100;
  const writes = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() === 'PATCH') {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      assert.equal(url.pathname, `/api/v1/rooms/${value.id}/speakers/101/balance`);
      assert.deepEqual(request.postDataJSON(), { expected_revision: 1, balance_percent: 55 });
      value.speakers[0].balance_percent = 55;
      value.outputs[0].balance_percent = 55;
      value.revision = 2;
      await route.fulfill({ json: { room: value, runtime_accepted: true } }); return;
    }
    await route.fulfill({ json: url.pathname.endsWith('/events') ? { events: [] } : snapshot([value], false) });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speakers', exact: true }).click();
  await page.getByLabel('Saved balance for Kitchen speaker').fill('55');
  await page.getByRole('button', { name: 'Save balance', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[id^="balance-"]').value === '55' && document.querySelector('[id^="volume-"]').value === '50');
  assert.equal(writes.length, 1);
  assert.equal(value.volume, 50);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.deepEqual(errors, []);
}));

test('AirPlay clock preference is explicit, revision guarded and reports native mode', { skip: !enabled }, async () => withPage(async (page, base, errors) => {
  const value = roomValue({ speakers: [{ id: '101', name: 'Kitchen speaker', protocol: 'airplay2', offset_ms: 0, balance_percent: 100, airplay_timing: 'auto' }] });
  value.outputs[0].selected = true;
  value.outputs[0].airplay_timing = 'ptp';
  const writes = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() === 'PATCH') {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      assert.equal(url.pathname, `/api/v1/rooms/${value.id}/speakers/101/airplay-timing`);
      assert.deepEqual(request.postDataJSON(), { expected_revision: 1, airplay_timing: 'ntp' });
      value.speakers[0].airplay_timing = 'ntp'; value.outputs[0].airplay_timing = 'ntp'; value.revision = 2;
      await route.fulfill({ json: { room: value, runtime_accepted: true } }); return;
    }
    await route.fulfill({ json: url.pathname.endsWith('/events') ? { events: [] } : snapshot([value], false) });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base);
  await page.getByRole('button', { name: 'Speakers', exact: true }).click();
  const selector = page.getByLabel('AirPlay timing for Kitchen speaker');
  assert.equal(await selector.inputValue(), 'auto');
  assert.equal(await page.getByRole('button', { name: 'Save timing mode', exact: true }).isDisabled(), true);
  await selector.selectOption('ntp');
  assert.equal(writes.length, 0);
  await page.getByRole('button', { name: 'Save timing mode', exact: true }).click();
  await page.getByText('Saved: ntp · Backend timing: NTP.', { exact: true }).waitFor();
  assert.equal(writes.length, 1);
  assert.equal(value.volume, 50);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.deepEqual(errors, []);
}));
