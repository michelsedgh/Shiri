import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, TtsStore, ttsPreviewPath, ttsRequestId, validTtsCatalog, validTtsJob } from '../../shiri/web/app.js';

const identifier = '01'.repeat(16);
const secondId = '02'.repeat(16);
const roomId = 'room-a';
const body = { request_id: identifier, model_id: 'qwen', text: 'Hello.', voice: 'ryan', language: 'English' };
const model = (id = 'qwen') => ({ id, name: id, voices: ['ryan'], languages: ['English'], streaming: 'incremental' });
const catalog = (id = 'qwen') => ({ enabled: true, worker: { state: 'ready', model_id: id }, models: [model('qwen'), model('kokoro')] });
const job = (state = 'queued', overrides = {}) => ({ id: identifier, kind: 'speech', room_id: roomId, state, metrics: {}, ...overrides });

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('catalog and job boundaries reject malformed metadata and different job identities', () => {
  assert.equal(validTtsCatalog(catalog()), true);
  assert.equal(validTtsCatalog({ enabled: false, worker: { state: 'stopped' }, models: [] }), true);
  assert.equal(validTtsCatalog({ enabled: true, worker: { state: 'unavailable' }, models: [] }), true);
  for (const value of [null, { ...catalog(), enabled: 'true' }, { ...catalog(), models: [model(), model()] },
    { ...catalog(), models: [{ ...model(), voices: 'ryan' }] }, { ...catalog(), worker: { state: 'unknown' } }]) {
    assert.equal(validTtsCatalog(value), false);
  }
  assert.equal(validTtsJob(job(), identifier), true);
  assert.equal(validTtsJob(job(), secondId), false);
  assert.equal(validTtsJob(job('unconfirmed')), false); // Only a local pending observation uses this state.
  assert.equal(validTtsJob({ ...job(), id: '\nsecret' }), false);
});

test('32-hex request identities work without the secure-context randomUUID API', () => {
  assert.equal(ttsRequestId({ getRandomValues: (bytes) => { bytes.fill(7); return bytes; } }), '07'.repeat(16));
  assert.throws(() => ttsRequestId({}), /cannot create a speech request identity/);
});

test('voice previews use only completed quiet job identities and never a returned URL', () => {
  const quiet = job('completed', { kind: 'benchmark', room_id: null, sample_available: true,
    sample_url: 'https://external.invalid/private.wav' });
  assert.equal(ttsPreviewPath(quiet), `/api/v1/tts/jobs/${identifier}/sample.wav`);
  for (const invalid of [job('completed', { sample_available: true }), { ...quiet, state: 'generating' },
    { ...quiet, state: 'cancelled' }, { ...quiet, sample_available: false }, { ...quiet, sample_available: 'true' },
    { ...quiet, id: '' }]) assert.equal(ttsPreviewPath(invalid), null);
});

test('a catalog read from before model loading cannot replace its newer confirmed state', async () => {
  const old = deferred(), calls = [];
  const store = new TtsStore({ request: async (path, options) => {
    calls.push([path, options]);
    if (calls.length === 1) return old.promise;
    if (path.endsWith('/load')) return { state: 'loading' };
    return catalog('kokoro');
  } });
  const reading = store.refreshModels();
  await Promise.resolve();
  const loading = store.loadModel('kokoro');
  await loading;
  old.resolve(catalog('qwen'));
  await reading;
  assert.equal(store.catalog.worker.model_id, 'kokoro');
  assert.equal(calls.filter(([path]) => path === '/tts/models').length, 2);
  assert.equal(store.busy, false);
});

test('a lost admission response recovers the exact request by read without repeating speech', async () => {
  const calls = [];
  const store = new TtsStore({ request: async (path, options = {}) => {
    calls.push({ path, options });
    if (options.method === 'POST') throw new ApiError('Connection lost', { ambiguous: true });
    return job('playing');
  } });
  assert.equal((await store.createJob('/rooms/room-a/tts', body)).state, 'playing');
  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.body.request_id, identifier);
  assert.equal(calls[1].path, `/tts/jobs/${identifier}`);
  assert.equal(calls.filter((call) => call.options.method === 'POST').length, 1);
});

test('unconfirmed admission remains recoverable and blocks duplicate generation', async () => {
  let recoverable = false;
  const calls = [];
  const store = new TtsStore({ request: async (path, options = {}) => {
    calls.push({ path, options });
    if (options.method === 'POST') throw new ApiError('Response unreadable', { ambiguous: true });
    if (!recoverable) throw new ApiError('Not found yet', { status: 404 });
    return job();
  } });
  await assert.rejects(store.createJob('/rooms/room-a/tts', body), /no repeat was sent/);
  assert.equal(store.job.state, 'unconfirmed');
  await assert.rejects(store.createJob('/rooms/room-a/tts', { ...body, request_id: secondId }), /Stop the current/);
  recoverable = true;
  assert.equal((await store.refreshJob()).state, 'queued');
  assert.equal(calls.filter((call) => call.options.method === 'POST').length, 1);
});

test('late status from before exact cancellation cannot revive the stopped job', async () => {
  const late = deferred(), calls = [];
  const store = new TtsStore({ request: async (path, options = {}) => {
    calls.push({ path, options });
    if (options.method === 'POST') return job('generating');
    if (options.method === 'DELETE') return job('cancelled');
    return late.promise;
  } });
  await store.createJob('/rooms/room-a/tts', body);
  const read = store.refreshJob();
  await Promise.resolve();
  await store.cancelJob();
  late.resolve(job('playing'));
  await read;
  assert.equal(store.job.state, 'cancelled');
  assert.equal(store.activeJob(), false);
  assert.equal(calls.find((call) => call.options.method === 'DELETE').path, `/tts/jobs/${identifier}`);
});

test('status for another room cannot replace a confirmed speech target even with the same ID', async () => {
  const store = new TtsStore({ request: async (_path, options = {}) => options.method === 'POST'
    ? job('playing') : job('completed', { room_id: 'room-b' }) });
  await store.createJob('/rooms/room-a/tts', body);
  await assert.rejects(store.refreshJob(), /different speech job/);
  assert.equal(store.job.state, 'playing');
  assert.equal(store.job.room_id, 'room-a');
});

test('a status poll started during cancellation cannot restore pre-stop state after its acknowledgment', async () => {
  const cancellation = deferred(), status = deferred();
  const store = new TtsStore({ request: async (_path, options = {}) => {
    if (options.method === 'POST') return job('playing');
    if (options.method === 'DELETE') return cancellation.promise;
    return status.promise;
  } });
  await store.createJob('/rooms/room-a/tts', body);
  const stopping = store.cancelJob();
  const reading = store.refreshJob();
  await Promise.resolve();
  cancellation.resolve(job('cancelled'));
  await stopping;
  status.resolve(job('playing'));
  await reading;
  assert.equal(store.job.state, 'cancelled');
});

test('invalidating private speech state excludes outstanding catalog and admission results', async () => {
  const read = deferred(), write = deferred();
  const store = new TtsStore({ request: async (_path, options = {}) => options.method === 'POST' ? write.promise : read.promise });
  const reading = store.refreshModels();
  const creating = store.createJob('/rooms/room-a/tts', body);
  store.invalidate();
  read.resolve(catalog()); write.resolve(job());
  await Promise.all([reading, creating]);
  assert.equal(store.catalog, null);
  assert.equal(store.job, null);
  assert.equal(store.busy, false);
});

test('benchmark admission requires an exact quiet job and no room target', async () => {
  const calls = [];
  const store = new TtsStore({ request: async (path, options = {}) => {
    calls.push({ path, options });
    return job('completed', { kind: 'benchmark', room_id: null });
  } });
  await store.createJob('/tts/benchmark', body);
  assert.equal(store.job.kind, 'benchmark');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/tts/benchmark');
  assert.equal(calls[0].options.body.room_id, undefined);
});

test('different rooms retain independent speech jobs while duplicate request identities stay blocked', async () => {
  const calls = [];
  const store = new TtsStore({ request: async (path, options = {}) => {
    calls.push({ path, options });
    const target = path.includes('/room-b/') ? 'room-b' : 'room-a';
    return job('queued', { id: options.body.request_id, room_id: target });
  } });
  await store.createJob('/rooms/room-a/tts', body);
  await store.createJob('/rooms/room-b/tts', { ...body, request_id: secondId });
  assert.equal(store.activeJob('room-a'), true);
  assert.equal(store.activeJob('room-b'), true);
  assert.equal(store.job.room_id, 'room-b');
  store.selectTarget('room-a');
  assert.equal(store.job.id, identifier);
  await assert.rejects(store.createJob('/rooms/room-a/tts', body), /already being tracked/);
  await assert.rejects(store.loadModel('kokoro'), /active speech jobs/);
  assert.equal(calls.length, 2);
});

test('each room admits one active and two waiting replies and cancellation frees only its exact queue entry', async () => {
  const thirdId = '03'.repeat(16), fourthId = '04'.repeat(16), jobs = new Map(), admissions = [];
  const store = new TtsStore({ request: async (path, options = {}) => {
    if (options.method === 'POST') {
      const value = job(jobs.size ? 'queued' : 'playing', { id: options.body.request_id });
      jobs.set(value.id, value); admissions.push(value.id); return value;
    }
    const value = jobs.get(path.split('/').at(-1));
    return options.method === 'DELETE' ? { ...value, state: 'cancelled' } : value;
  } });
  for (const id of [identifier, secondId, thirdId]) await store.createJob('/rooms/room-a/tts', { ...body, request_id: id });
  assert.equal(store.canQueue(roomId), false);
  assert.equal(store.canQueue('room-b'), true);
  assert.deepEqual(store.activeJobs(roomId).map((value) => value.id), [identifier, secondId, thirdId]);
  await assert.rejects(store.createJob('/rooms/room-a/tts', { ...body, request_id: fourthId }), /wait for this room’s queue/);
  store.selectJob(secondId);
  await store.cancelJob();
  assert.equal(store.jobs.get(identifier).state, 'playing');
  assert.equal(store.jobs.get(secondId).state, 'cancelled');
  assert.equal(store.jobs.get(thirdId).state, 'queued');
  assert.equal(store.canQueue(roomId), true);
  await store.createJob('/rooms/room-a/tts', { ...body, request_id: fourthId });
  assert.deepEqual(admissions, [identifier, secondId, thirdId, fourthId]);
});

test('a rejected queued admission preserves the selected existing reply', async () => {
  const store = new TtsStore({ request: async (_path, options = {}) => {
    if (options.body.request_id === identifier) return job('playing');
    throw new ApiError('Room queue full', { status: 409 });
  } });
  await store.createJob('/rooms/room-a/tts', body);
  await assert.rejects(store.createJob('/rooms/room-a/tts', { ...body, request_id: secondId }), /Room queue full/);
  assert.equal(store.job.id, identifier);
  assert.equal(store.job.state, 'playing');
});

test('switching rooms during cancellation cannot retire another room or lose its polling', async () => {
  const cancellation = deferred();
  const store = new TtsStore({ request: async (path, options = {}) => {
    if (options.method === 'POST') return job('playing', { id: options.body.request_id, room_id: path.includes('/room-b/') ? 'room-b' : 'room-a' });
    if (options.method === 'DELETE') return cancellation.promise;
    return path.endsWith(secondId) ? job('playing', { id: secondId, room_id: 'room-b' }) : job('completed');
  } });
  await store.createJob('/rooms/room-a/tts', body);
  await store.createJob('/rooms/room-b/tts', { ...body, request_id: secondId });
  store.selectTarget('room-a');
  const pending = store.cancelJob();
  store.selectTarget('room-b');
  cancellation.resolve(job('cancelled'));
  await pending;
  await store.refreshJobs();
  assert.equal(store.job.room_id, 'room-b');
  assert.equal(store.job.state, 'playing');
  assert.equal(store.activeJob('room-a'), false);
  assert.equal(store.activeJob('room-b'), true);
  store.invalidate();
  assert.equal(store.hasActiveJobs(), false);
  assert.equal(store.jobs.size, 0);
});
