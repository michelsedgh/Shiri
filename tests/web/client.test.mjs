import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiClient, ApiError, RoomStore, canSaveSelection, protocolName, roomHealth, speakerState, reductionPercent, safeHttpUrl } from '../../shiri/web/app.js';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const state = (rooms = []) => ({ rooms, runtime: { ready: true, simulation: false }, interfaces: ['eth0'], capabilities: {} });
const room = (extra = {}) => ({ id: 'room-a', name: 'Kitchen', revision: 1, enabled: true, volume: 50, speakers: [], outputs: [], runtime: { status: 'running', error: null }, ...extra });
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(data) });

test('browser requests use same-origin cookies and no persistent token storage', async () => {
  let request;
  const api = new ApiClient({ fetcher: async (path, options) => { request = { path, options }; return response({ ok: true }); } });
  await api.request('/session', { method: 'POST', body: { token: 'secret' } });
  assert.equal(request.path, '/api/v1/session');
  assert.equal(request.options.credentials, 'same-origin');
  assert.equal(request.options.cache, 'no-store');
  assert.equal(request.options.headers.Authorization, undefined);
});

test('HTTP errors preserve the backend explanation and conflict code', async () => {
  const api = new ApiClient({ fetcher: async () => response({ error: 'Room changed; reload it', code: 'conflict' }, 409) });
  await assert.rejects(api.request('/rooms/a'), (error) => error.status === 409 && error.code === 'conflict' && /reload/.test(error.message));
});

test('validation errors show their actionable field explanations', async () => {
  const api = new ApiClient({ fetcher: async () => response({ error: 'Request validation failed', fields: [{ message: 'AirPlay name must fit within 50 bytes' }] }, 422) });
  await assert.rejects(api.request('/rooms', { method: 'POST', body: {} }), /AirPlay name/);
});

test('malformed successful responses never become an empty successful state', async () => {
  const api = new ApiClient({ fetcher: async () => ({ ok: true, status: 200, text: async () => '<html>Proxy error</html>' }) });
  await assert.rejects(api.request('/state'), /unreadable response/);
});

test('a failed mutation envelope does not become a success', async () => {
  const api = new ApiClient({ fetcher: async () => response({ ok: false, error: 'No speaker available' }) });
  await assert.rejects(api.request('/rooms/a/speakers', { method: 'PUT', body: {} }), /No speaker/);
});

test('hanging requests abort within their deadline', async () => {
  let aborted = false;
  const api = new ApiClient({ fetcher: (_path, { signal }) => new Promise((_resolve, reject) => signal.addEventListener('abort', () => { aborted = true; reject(new Error('aborted')); })) });
  await assert.rejects(api.request('/state', { deadlineMs: 5 }), (error) => error.code === 'timeout');
  assert.equal(aborted, true);
});

test('unconfirmed writes report ambiguity and never automatically retry', async () => {
  let calls = 0;
  const api = new ApiClient({ fetcher: async () => { calls += 1; throw new Error('connection reset after commit'); } });
  await assert.rejects(api.request('/rooms', { method: 'POST', body: {} }), (error) => error.ambiguous && /before trying again/.test(error.message));
  assert.equal(calls, 1);
});

test('state refreshes are single-flight', async () => {
  const pending = deferred();
  let calls = 0;
  const store = new RoomStore({ request: () => { calls += 1; return pending.promise; } });
  const first = store.refresh(), second = store.refresh();
  assert.equal(calls, 1);
  pending.resolve(state());
  await Promise.all([first, second]);
});

test('a poll from before a write cannot replace the state after a write', async () => {
  const old = deferred();
  let calls = 0;
  const fresh = state([room({ revision: 2, name: 'Fresh' })]);
  const store = new RoomStore({ request: () => ++calls === 1 ? old.promise : Promise.resolve(fresh) });
  const read = store.refresh();
  const write = store.change('room-a', async () => ({ room: room({ revision: 2 }) }));
  old.resolve(state([room({ name: 'Stale' })]));
  await Promise.all([read, write]);
  assert.equal(store.snapshot.rooms[0].name, 'Fresh');
  assert.equal(calls, 2);
});

test('writes to one room are locked until the resulting state is refreshed', async () => {
  const write = deferred(), read = deferred();
  const store = new RoomStore({ request: () => read.promise });
  const first = store.change('room-a', () => write.promise);
  assert.equal((await store.change('room-a', async () => {})).skipped, true);
  write.resolve({ ok: true });
  await Promise.resolve(); await Promise.resolve();
  assert.equal(store.busy.has('room-a'), true);
  assert.equal((await store.change('room-a', async () => {})).skipped, true);
  read.resolve(state());
  await first;
  assert.equal(store.busy.has('room-a'), false);
});

test('revision conflict remains visible after the fresh state arrives', async () => {
  const store = new RoomStore({ request: async () => state([room({ revision: 2 })]) });
  const result = await store.change('room-a', async () => { throw new ApiError('Room changed', { status: 409, code: 'conflict' }); });
  assert.equal(result.ok, false);
  assert.equal(result.conflict, true);
  assert.equal(store.writeErrors.get('room-a'), 'Room changed');
  assert.equal(store.room('room-a').revision, 2);
});

test('auth expiry clears private room state and prevents further background reads', async () => {
  let calls = 0;
  const store = new RoomStore({ request: async () => { calls += 1; throw new ApiError('Sign in', { status: 401 }); } });
  store.snapshot = state([room()]);
  await store.refresh(); await store.refresh();
  assert.equal(store.snapshot, null);
  assert.equal(store.authRequired, true);
  assert.equal(calls, 1);
});

test('invalid room state cannot replace the previously confirmed state', async () => {
  const store = new RoomStore({ request: async () => ({ rooms: [] }) });
  const confirmed = state([room()]);
  store.snapshot = confirmed;
  await store.refresh();
  assert.equal(store.snapshot, confirmed);
  assert.match(store.readError, /incomplete room state/);
});

test('incomplete room records cannot crash rendering or replace confirmed state', async () => {
  const store = new RoomStore({ request: async () => state([{}]) });
  const confirmed = state([room()]);
  store.snapshot = confirmed;
  await store.refresh();
  assert.equal(store.snapshot, confirmed);
  assert.match(store.readError, /incomplete room state/);
});

test('disabled desired state is shown as off even if runtime is recovering', () => {
  assert.equal(roomHealth(room({ enabled: false, runtime: { status: 'recovering' } })).label, 'Off');
  assert.equal(roomHealth(room({ enabled: false })).label, 'Turning off');
});

test('desired enabled does not imply running or speech ready', () => {
  for (const status of ['starting', 'stopped', 'recovering', 'degraded', 'error']) assert.equal(roomHealth(room({ runtime: { status } })).ready, false);
});

test('an assignment is not a confirmed active speaker', () => {
  const saved = [{ id: '101', name: 'Kitchen', protocol: 'airplay2' }];
  const inactive = room({ speakers: saved, outputs: [{ id: '101', available: true, selected: false }] });
  assert.equal(speakerState(inactive).missing.length, 1);
  assert.equal(roomHealth(inactive).ready, false);
  assert.equal(roomHealth(room({ speakers: saved, outputs: [{ id: '101', available: true, selected: true }] })).ready, true);
});

test('saved speaker ownership can be released during an outage without adding new outputs', () => {
  const value = room({ enabled: false, speakers: [{ id: '101' }], outputs_error: 'Offline' });
  assert.equal(canSaveSelection(value, { dirty: true, selected: new Set() }), true);
  assert.equal(canSaveSelection(value, { dirty: true, selected: new Set(['101']) }), true);
  assert.equal(canSaveSelection(value, { dirty: true, selected: new Set(['202']) }), false);
});

test('a failed discovery query cannot advertise speech readiness', () => {
  const value = room({ speakers: [{ id: '101' }], outputs: [{ id: '101', available: true, selected: true }], outputs_error: 'OwnTone unavailable' });
  assert.equal(roomHealth(value).ready, false);
  assert.match(roomHealth(value).label, /unavailable/);
});

test('simulation never claims physical playback readiness', () => {
  assert.equal(roomHealth(room({ speakers: [{ id: '101' }], outputs: [{ id: '101', available: true, selected: true }] }), { simulation: true }).ready, false);
});

test('protocol names reflect capabilities without claiming universal synchronization', () => {
  assert.equal(protocolName('chromecast'), 'Cast');
  assert.equal(protocolName('airplay1'), 'AirPlay');
  assert.equal(protocolName('alsa'), 'Local audio');
});

test('music reduction maps to the desired duck gain and bounds invalid values', () => {
  assert.equal(reductionPercent(.28), 72);
  assert.equal(reductionPercent(Infinity), 72);
  assert.equal(reductionPercent(-1), 100);
  assert.equal(reductionPercent(2), 0);
});

test('runtime links reject executable schemes and credential-bearing URLs', () => {
  assert.equal(safeHttpUrl('http://192.168.1.5:3689'), 'http://192.168.1.5:3689/');
  assert.equal(safeHttpUrl('javascript:alert(1)'), null);
  assert.equal(safeHttpUrl('http://admin:password@example.com'), null);
});
