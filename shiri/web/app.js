/** Shiri's browser client. All device and room text is rendered as text nodes. */
export class ApiError extends Error {
  constructor(message, { status = 0, code = '', ambiguous = false } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.ambiguous = ambiguous;
  }
}

export class ApiClient {
  constructor({ fetcher = (...args) => fetch(...args), deadlineMs = 15000 } = {}) {
    this.fetcher = fetcher;
    this.deadlineMs = deadlineMs;
  }

  async request(path, { method = 'GET', body, rawBody, contentType = 'audio/wav', deadlineMs = method === 'GET' ? this.deadlineMs : Math.max(this.deadlineMs, 45000) } = {}) {
    if (rawBody !== undefined && body !== undefined) throw new ApiError('Choose one request body format.');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), deadlineMs);
    const writing = method !== 'GET';
    try {
      const response = await this.fetcher(`/api/v1${path}`, {
        method, credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: { Accept: 'application/json', ...(rawBody !== undefined ? { 'Content-Type': contentType } : body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(rawBody !== undefined ? { body: rawBody } : body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const text = await response.text();
      let data = null;
      if (text) {
        try { data = JSON.parse(text); }
        catch { throw new ApiError('Shiri returned an unreadable response. Refresh to check the current state.', { status: response.status, ambiguous: writing }); }
      }
      if (!response.ok) {
        const detail = data?.error || data?.detail?.message || data?.detail;
        const fields = Array.isArray(data?.fields) ? data.fields.map((field) => field.message).filter(Boolean).join(' ') : '';
        const message = fields || (typeof detail === 'string' ? detail : `Shiri could not complete this request (${response.status}).`);
        throw new ApiError(message, { status: response.status, code: data?.code || data?.detail?.code || '' });
      }
      if (data?.ok === false) throw new ApiError(data.error || 'Shiri did not accept this change.', { status: response.status, code: data.code || '' });
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      const message = writing
        ? 'Shiri did not confirm the change. Refresh to check its state before trying again.'
        : 'Cannot reach Shiri. The displayed room state may be out of date.';
      throw new ApiError(message, { ambiguous: writing, code: controller.signal.aborted ? 'timeout' : 'connection' });
    } finally {
      clearTimeout(timer);
    }
  }
}

/** Read/write coordination is separate from rendering so races can be tested. */
export class RoomStore {
  constructor(client) {
    this.client = client;
    this.snapshot = null;
    this.authRequired = false;
    this.readError = '';
    this.writeErrors = new Map();
    this.busy = new Set();
    this.version = 0;
    this.reading = null;
    this.listeners = new Set();
  }

  subscribe(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  notify() { for (const listener of this.listeners) listener(this); }
  room(id) { return this.snapshot?.rooms.find((room) => room.id === id) || null; }

  requireAuth() {
    this.authRequired = true;
    this.snapshot = null;
    this.readError = '';
    this.writeErrors.clear();
    this.version += 1;
    this.notify();
  }

  async refresh() {
    if (this.authRequired) return false;
    if (this.reading) return this.reading;
    const version = this.version;
    this.reading = (async () => {
      try {
        const snapshot = await this.client.request('/state');
        if (version !== this.version) return false;
        if (!validSnapshot(snapshot)) {
          throw new ApiError('Shiri returned an incomplete room state. Refresh and check the service.');
        }
        this.snapshot = snapshot;
        this.readError = '';
        this.notify();
        return true;
      } catch (error) {
        if (version !== this.version) return false;
        if (error.status === 401) this.requireAuth();
        else { this.readError = error.message; this.notify(); }
        return false;
      } finally { this.reading = null; }
    })();
    return this.reading;
  }

  async change(key, work) {
    if (this.busy.has(key) || this.authRequired) return { ok: false, skipped: true };
    this.busy.add(key);
    this.version += 1;
    this.writeErrors.delete(key);
    this.notify();
    let result;
    try {
      const data = await work();
      result = { ok: true, data };
    } catch (error) {
      if (error.status === 401) this.requireAuth();
      else this.writeErrors.set(key, error.message);
      result = { ok: false, error, conflict: error.status === 409 && error.code === 'conflict' };
    } finally {
      this.version += 1;
      // Keep controls locked through the refresh that confirms the resulting state.
      if (this.reading) await this.reading;
      if (!this.authRequired) await this.refresh();
      this.busy.delete(key);
      this.notify();
    }
    return result;
  }
}

export function validSnapshot(snapshot) {
  return !!snapshot && Array.isArray(snapshot.rooms) && snapshot.rooms.length <= 8
    && !!snapshot.runtime && Array.isArray(snapshot.interfaces) && snapshot.interfaces.every((entry) => typeof entry === 'string')
    && snapshot.rooms.every((room) => !!room && typeof room.id === 'string' && typeof room.name === 'string'
      && Number.isInteger(room.revision) && room.revision >= 1 && typeof room.enabled === 'boolean'
      && Number.isInteger(room.volume) && room.volume >= 0 && room.volume <= 100
      && Array.isArray(room.speakers) && room.speakers.every((speaker) => !!speaker && typeof speaker.id === 'string' && typeof speaker.name === 'string')
      && Array.isArray(room.outputs) && room.outputs.length <= 1024 && room.outputs.every((output) => !!output && typeof output.id === 'string' && typeof output.name === 'string')
      && !!room.runtime && typeof room.runtime.status === 'string');
}

export function protocolName(protocol) {
  const value = String(protocol || '').toLowerCase();
  if (value.includes('airplay') || value === 'raop') return 'AirPlay';
  if (value.includes('cast')) return 'Cast';
  if (value.includes('alsa') || value === 'local') return 'Local audio';
  if (value.includes('bluetooth')) return 'Bluetooth';
  return String(protocol || 'Speaker');
}

export function speakerState(room) {
  const saved = Array.isArray(room.speakers) ? room.speakers : [];
  const outputs = Array.isArray(room.outputs) ? room.outputs : [];
  const missing = saved.filter((speaker) => !outputs.some((output) => output.id === speaker.id && output.available !== false && output.selected));
  return { saved, outputs, missing, active: saved.length - missing.length };
}

export function canSaveSelection(room, draft) {
  if (!draft.dirty) return false;
  const saved = new Set(room.speakers.map((speaker) => speaker.id));
  const adding = [...draft.selected].some((id) => !saved.has(id));
  // Releasing a saved assignment must work even while its room is off or offline.
  return !adding || room.enabled && !room.outputs_error && room.runtime?.status === 'running';
}

export function roomHealth(room, runtime = {}) {
  const status = room.runtime?.status || 'stopped';
  if (runtime.simulation) return { label: room.enabled ? 'Simulated · On' : 'Simulated · Off', tone: '', ready: false };
  if (!room.enabled) return { label: status === 'running' ? 'Turning off' : 'Off', tone: '', ready: false };
  const transient = { stopped: 'Waiting to start', starting: 'Starting', recovering: 'Recovering', stopping: 'Stopping' };
  if (transient[status]) return { label: transient[status], tone: 'warning', ready: false };
  if (status === 'error' || status === 'degraded') return { label: 'Needs attention', tone: 'error', ready: false };
  if (status !== 'running') return { label: 'Status unavailable', tone: 'warning', ready: false };
  const speakers = speakerState(room);
  if (room.outputs_error) return { label: 'Speaker discovery unavailable', tone: 'warning', ready: false };
  if (!speakers.saved.length) return { label: 'Choose speakers', tone: 'warning', ready: false };
  if (speakers.missing.length) return { label: 'Speakers not fully active', tone: 'warning', ready: false };
  return { label: room.nobly_room_id ? 'Ready for room speech' : 'Audio ready', tone: 'good', ready: true };
}

export function reductionPercent(gain) {
  const value = Number(gain);
  return Math.round((1 - (Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : .28)) * 100);
}

export function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function validLocalDeviceURI(value) {
  return typeof value === 'string' && /^shiri:device=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
}

function validDeviceLabel(value) {
  return typeof value === 'string' && value.length > 0 && [...value].length <= 256 && !/[\u0000-\u001f\u007f]/.test(value);
}

const localBindingNames = { serial: 'Serial number', port: 'USB port', path: 'System device path', loopback: 'Loopback device' };
const localBindingHelp = {
  serial: 'Uses this speaker’s verified unique serial number. Its USB port can change.',
  port: 'Uses this USB port. Moving the speaker to another port requires choosing and saving it again. Replacing it at this port also requires verification.',
  path: 'Uses this device’s stable system path. A hardware change requires choosing and saving it again.',
  loopback: 'Uses this installed Loopback device and its playback endpoint.',
};

export function validLocalDeviceInventory(value) {
  if (!value || !Array.isArray(value.devices) || value.devices.length > 512) return false;
  const identifiers = new Set();
  return value.devices.every((device) => {
    if (!device || typeof device.selection_id !== 'string' || !/^[0-9a-f]{64}$/.test(device.selection_id)
      || identifiers.has(device.selection_id) || !validDeviceLabel(device.label)
      || !Array.isArray(device.can_bind_by) || !device.can_bind_by.length || device.can_bind_by.length > 4
      || new Set(device.can_bind_by).size !== device.can_bind_by.length
      || !device.can_bind_by.every((binding) => Object.hasOwn(localBindingNames, binding))
      || !Array.isArray(device.bindings) || device.bindings.length !== device.can_bind_by.length) return false;
    identifiers.add(device.selection_id);
    return device.bindings.every((binding, index) => !!binding && binding.binding === device.can_bind_by[index]
      && (binding.device === null || validLocalDeviceURI(binding.device)));
  });
}

export function validLocalBindingAck(value, binding, expectedDevice = null) {
  return !!value && validLocalDeviceURI(value.device) && validDeviceLabel(value.label) && value.binding === binding
    && (!expectedDevice || value.device === expectedDevice);
}

const client = new ApiClient();
const store = new RoomStore(client);
const view = { roomDraft: null, speakerDraft: null, calibrationDraft: null, calibrationSessions: new Map(), calibrationHistory: new Map(), toastTimer: null, eventsRequest: null, eventsVersion: 0, speech: new Map(), cardSignature: '', pollTimer: null };
const elements = {};

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
}

function node(tag, className = '', text = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== '') element.textContent = String(text);
  return element;
}

function button(text, className, action) {
  const element = node('button', `button ${className}`, text);
  element.type = 'button';
  element.addEventListener('click', action);
  return element;
}

function notice(element, message, tone = 'error') {
  element.textContent = message || '';
  element.className = `notice ${tone}`;
  element.hidden = !message;
}

function toast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  clearTimeout(view.toastTimer);
  view.toastTimer = setTimeout(() => { elements.toast.hidden = true; }, 4000);
}

async function init() {
  for (const element of document.querySelectorAll('[id]')) elements[element.id.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = element;
  store.subscribe(render);
  bindEvents();
  await store.refresh();
  poll();
}

function bindEvents() {
  elements.refresh.addEventListener('click', async () => { if (await store.refresh()) toast('Room state refreshed'); });
  elements.addRoom.addEventListener('click', () => openRoom());
  elements.pairingForm.addEventListener('submit', login);
  elements.signOut.addEventListener('click', logout);
  elements.roomForm.addEventListener('submit', saveRoom);
  elements.roomForm.addEventListener('input', () => { if (view.roomDraft) view.roomDraft.dirty = true; });
  elements.roomForm.addEventListener('change', () => { if (view.roomDraft) view.roomDraft.dirty = true; });
  elements.roomDuck.addEventListener('input', () => { elements.roomDuckValue.textContent = `${elements.roomDuck.value}%`; });
  elements.localSpeaker.addEventListener('change', () => {
    const local = view.roomDraft?.local;
    if (!local) return;
    local.choice = elements.localSpeaker.value;
    local.edited = true;
    const device = local.devices.find((item) => item.selection_id === local.choice);
    local.binding = device?.can_bind_by[0] || null;
    renderLocalSpeaker();
  });
  elements.localSpeakerBinding.addEventListener('change', () => {
    if (!view.roomDraft) return;
    view.roomDraft.local.binding = elements.localSpeakerBinding.value;
    view.roomDraft.local.edited = true;
    renderLocalSpeaker();
  });
  elements.localAudioDevice.addEventListener('input', () => {
    if (!view.roomDraft) return;
    view.roomDraft.local.choice = 'advanced';
    view.roomDraft.local.edited = true;
    renderLocalSpeaker();
  });
  elements.refreshLocalSpeakers.addEventListener('click', () => loadLocalSpeakers(view.roomDraft));
  elements.deleteRoom.addEventListener('click', deleteRoom);
  elements.reloadRoomDraft.addEventListener('click', () => reloadRoom());
  elements.speakerForm.addEventListener('submit', saveSpeakers);
  elements.refreshSpeakers.addEventListener('click', async () => { await store.refresh(); renderSpeakers(); });
  elements.reloadSpeakerDraft.addEventListener('click', reloadSpeakers);
  elements.measureTiming.addEventListener('click', () => openCalibration(view.speakerDraft?.id));
  elements.calibrationForm.addEventListener('submit', startCalibration);
  elements.calibrationForm.addEventListener('input', () => { if (view.calibrationDraft) view.calibrationDraft.dirty = true; });
  elements.calibrationUpload.addEventListener('submit', uploadCalibration);
  elements.calibrationOff.addEventListener('click', () => calibrationRoomToggle(false));
  elements.calibrationOn.addEventListener('click', () => calibrationRoomToggle(true));
  elements.calibrationReferenceOn.addEventListener('click', calibrationReferenceOn);
  elements.calibrationReferenceRoom.addEventListener('change', populateCalibrationReference);
  elements.calibrationApply.addEventListener('click', () => applyCalibration(false));
  elements.calibrationRollback.addEventListener('click', () => applyCalibration(true));
  elements.calibrationEnd.addEventListener('click', endCalibration);
  elements.calibrationRefresh.addEventListener('click', refreshCalibration);
  elements.calibrationNew.addEventListener('click', () => {
    const draft = view.calibrationDraft;
    if (!draft || draft.loading || store.busy.has(draft.id) || !confirmDiscard()) return;
    view.calibrationSessions.delete(draft.id);
    draft.newSession = true;
    draft.dirty = false;
    elements.calibrationUpload.reset();
    renderCalibration();
  });
  elements.calibrationHistory.addEventListener('change', () => {
    const draft = view.calibrationDraft;
    if (!draft || draft.loading || store.busy.has(draft.id)) return;
    const session = view.calibrationHistory.get(draft.id)?.find((item) => item.id === elements.calibrationHistory.value);
    if (!session) return;
    view.calibrationSessions.set(draft.id, session);
    draft.newSession = false;
    draft.dirty = false;
    elements.calibrationUpload.reset();
    renderCalibration();
  });
  elements.refreshEvents.addEventListener('click', loadEvents);
  elements.diagnostics.addEventListener('toggle', () => { if (elements.diagnostics.open) loadEvents(); });
  for (const element of document.querySelectorAll('[data-close]')) element.addEventListener('click', () => closeSheet(element.dataset.close));
  for (const [kind, dialog] of [['room', elements.roomDialog], ['speakers', elements.speakerDialog], ['calibration', elements.calibrationDialog]]) {
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); closeSheet(kind); });
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !store.authRequired) store.refresh(); });
  window.addEventListener('beforeunload', (event) => {
    if (!draftDirty() && !store.busy.size) return;
    event.preventDefault();
    event.returnValue = '';
  });
  window.addEventListener('pagehide', () => { for (const id of view.speech.keys()) releaseSpeech(id); });
}

function poll() {
  const transitioning = store.snapshot?.rooms.some((room) => ['starting', 'recovering', 'stopping'].includes(room.runtime?.status));
  view.pollTimer = setTimeout(async () => {
    try {
      if (!document.hidden && !store.authRequired) {
        await store.refresh();
        if (elements.diagnostics.open) await loadEvents();
      }
    } finally { poll(); }
  }, transitioning ? 2000 : 6000);
}

function render() {
  elements.pairing.hidden = !store.authRequired;
  elements.dashboard.hidden = store.authRequired;
  elements.refresh.hidden = store.authRequired;
  elements.signOut.hidden = store.authRequired || !store.snapshot || store.snapshot.runtime.simulation;
  if (store.authRequired) {
    elements.roomGrid.replaceChildren();
    elements.eventList.replaceChildren();
    elements.speakerOptions.replaceChildren();
    elements.roomForm.reset();
    elements.roomConnectionStatus.textContent = '';
    elements.roomOwntone.removeAttribute('href');
    view.eventsVersion += 1;
    elements.systemStatus.className = 'system-status';
    elements.systemStatus.textContent = 'Browser not connected';
    view.roomDraft = null;
    view.speakerDraft = null;
    elements.roomDialog.close();
    elements.speakerDialog.close();
    view.calibrationDraft = null;
    view.calibrationSessions.clear();
    view.calibrationHistory.clear();
    elements.calibrationDialog.close();
    elements.calibrationForm.reset();
    elements.calibrationUpload.reset();
    elements.calibrationReference.replaceChildren();
    elements.calibrationReferenceRoom.replaceChildren();
    elements.calibrationTarget.replaceChildren();
    elements.calibrationHistory.replaceChildren();
    delete elements.calibrationHistory.dataset.signature;
    elements.calibrationTitle.textContent = 'Speaker calibration';
    elements.calibrationApplyHelp.textContent = '';
    elements.calibrationResult.replaceChildren();
    elements.calibrationProbe.removeAttribute('href');
    elements.calibrationExport.removeAttribute('href');
    for (const id of view.speech.keys()) releaseSpeech(id);
    return;
  }
  notice(elements.connectionError, store.readError);
  const snapshot = store.snapshot;
  if (!snapshot) {
    elements.systemStatus.textContent = store.readError ? 'Connection unavailable' : 'Connecting…';
    elements.systemStatus.className = `system-status ${store.readError ? 'error' : ''}`;
    return;
  }
  const runtime = snapshot.runtime;
  elements.systemStatus.className = `system-status ${store.readError ? 'error' : runtime.simulation ? 'warning' : runtime.ready ? 'good' : 'warning'}`;
  elements.systemStatus.replaceChildren(node('span', 'status-dot'), node('span', '', store.readError ? 'State may be out of date' : runtime.simulation ? 'Simulation mode' : runtime.ready ? 'Audio service ready' : 'Audio service needs attention'));
  notice(elements.modeNotice, runtime.simulation
    ? 'Simulation mode: room settings and discovery are simulated. No physical speakers are playing.'
    : runtime.error || '', runtime.simulation ? 'warning' : 'error');
  elements.addRoom.disabled = store.busy.has('create');
  const count = snapshot.rooms.length;
  const on = snapshot.rooms.filter((room) => room.enabled).length;
  elements.roomSummary.textContent = count ? `${count} room${count === 1 ? '' : 's'} · ${on} turned on` : 'Music and spoken replies, exactly where you want them.';
  renderCards();
  renderCapabilities(snapshot.capabilities || {});
  renderSheetBusy();
  if (view.speakerDraft) renderSpeakers();
  if (view.calibrationDraft) renderCalibration();
}

function renderCards() {
  if (!store.snapshot || store.authRequired) return;
  if (store.busy.size) {
    // Keep the user's toggled/dragged control in place until its write finishes.
    // Replacing it in the change event also breaks assistive/browser input state.
    view.cardSignature = '';
    for (const card of elements.roomGrid.querySelectorAll('.room-card')) {
      const locked = store.busy.has(card.dataset.roomId);
      card.setAttribute('aria-busy', String(locked));
      for (const control of card.querySelectorAll('input, button')) {
        if (locked) { if (control.dataset.wasDisabled === undefined) control.dataset.wasDisabled = String(control.disabled); control.disabled = true; }
        else if (control.dataset.wasDisabled !== undefined) { control.disabled = control.dataset.wasDisabled === 'true'; delete control.dataset.wasDisabled; }
      }
    }
    return;
  }
  const rooms = store.snapshot.rooms;
  const signature = JSON.stringify([rooms, [...store.busy], [...store.writeErrors], [...view.speech.keys()], store.snapshot.runtime.simulation]);
  const active = document.activeElement;
  if (signature === view.cardSignature || elements.roomGrid.contains(active) && active?.type === 'range') return;
  view.cardSignature = signature;
  if (!rooms.length) {
    const empty = node('div', 'empty-state');
    empty.append(node('span', 'room-symbol', '⌂'), node('h2', '', 'Start with a room'), node('p', '', 'Give a room a name, turn on its audio, then choose the speakers that belong there.'), button('Add your first room', 'primary', () => openRoom()));
    elements.roomGrid.replaceChildren(empty);
    return;
  }
  elements.roomGrid.replaceChildren(...rooms.map(renderCard));
}

function renderCard(room) {
  const card = node('article', 'room-card');
  card.dataset.roomId = room.id;
  const locked = store.busy.has(room.id);
  const header = node('div', 'room-card-heading');
  const title = node('div');
  title.append(node('h2', '', room.name), node('p', 'room-identity', `AirPlay · ${room.airplay_name || room.name}`));
  if (room.nobly_room_id) title.append(node('p', 'room-identity', `Nobly room · ${room.nobly_room_id}`));
  const toggle = node('label', 'switch');
  const input = node('input');
  input.type = 'checkbox'; input.checked = room.enabled; input.disabled = locked;
  input.setAttribute('aria-label', `Turn ${room.name} audio ${room.enabled ? 'off' : 'on'}`);
  input.addEventListener('change', async () => {
    const enabled = input.checked;
    const result = await store.change(room.id, () => patchRoom(room, { enabled }));
    if (result.ok) toast(`${room.name} audio ${enabled ? 'enabled' : 'disabled'}`);
  });
  toggle.append(input, node('span', 'switch-track'));
  header.append(title, toggle);
  const health = roomHealth(room, store.snapshot.runtime);
  const status = node('div', `room-status ${health.tone}`);
  status.append(node('span', 'status-dot'), node('span', '', locked ? 'Saving…' : health.label));
  const speakers = speakerState(room);
  const summary = node('div', 'speaker-summary');
  summary.append(node('strong', '', speakers.saved.length ? `${speakers.saved.length} speaker${speakers.saved.length === 1 ? '' : 's'} assigned` : 'No speakers assigned'));
  summary.append(node('p', '', speakers.saved.length ? speakers.saved.map((speaker) => speaker.name || speaker.id).join(' · ') : 'Choose this room’s speakers to send music and replies here.'));
  const issue = store.writeErrors.get(room.id) || room.runtime?.error || room.outputs_error || (room.enabled && speakers.missing.length ? 'Some saved speakers are not confirmed active. Open Speakers to check them.' : '');
  if (issue) summary.append(node('div', 'card-error', issue));
  const volume = node('div', 'volume');
  const volumeLabel = node('label', 'volume-label');
  const slider = node('input'); slider.type = 'range'; slider.min = '0'; slider.max = '100'; slider.step = '1'; slider.value = String(room.volume); slider.disabled = locked;
  slider.id = `volume-${room.id}`;
  const value = node('output', '', `${room.volume}%`); value.htmlFor = slider.id;
  volumeLabel.htmlFor = slider.id; volumeLabel.append(node('span', '', 'Room volume'), value);
  slider.addEventListener('input', () => { value.textContent = `${slider.value}%`; });
  slider.addEventListener('change', async () => {
    const level = Number(slider.value);
    slider.blur();
    const result = await store.change(room.id, () => patchRoom(room, { volume: level }));
    if (result.ok) toast(`${room.name} volume saved`);
  });
  slider.addEventListener('blur', () => { view.cardSignature = ''; renderCards(); });
  volume.append(volumeLabel, slider);
  const actions = node('div', 'room-actions');
  const speakersButton = button('Speakers', 'quiet', () => openSpeakers(room.id));
  const settingsButton = button('Room settings', 'quiet', () => openRoom(room.id));
  speakersButton.disabled = locked; settingsButton.disabled = locked;
  actions.append(speakersButton, settingsButton);
  const play = button('Play', 'quiet', () => controlPlayer(room.id, 'play'));
  const stop = button('Stop playback', 'quiet', () => controlPlayer(room.id, 'stop'));
  play.disabled = stop.disabled = locked || !room.enabled || room.runtime?.status !== 'running';
  actions.append(play, stop);
  const testButton = button(view.speech.has(room.id) ? 'Stop audio test' : 'Test room audio', 'test-button', () => testSpeech(room.id));
  testButton.disabled = locked || !room.enabled || room.runtime?.status !== 'running' || !health.ready;
  testButton.title = speechSupported() ? 'Play a short, quiet test through this room’s speech path.' : 'This test requires a secure browser context with WebRTC audio support.';
  if (!speechSupported()) testButton.disabled = true;
  actions.append(testButton);
  card.append(header, status, summary, volume, actions);
  return card;
}

function renderCapabilities(capabilities) {
  const list = node('ul');
  list.append(node('li', '', 'Music input: AirPlay. Chromecast receiver input is deferred.'));
  list.append(node('li', '', 'Spoken replies: room-addressed WebRTC audio. Nobly integration uses the room ID you configure.'));
  list.append(node('li', '', 'Outputs: available OwnTone AirPlay and Cast devices; local ALSA audio when configured. Cast timing is approximate.'));
  list.append(node('li', '', 'Bluetooth outputs: pair the main speaker. If its speakers are linked as a group, they share this room’s music and spoken replies.'));
  list.append(node('li', '', 'Calibration: import shared-clock stereo microphone recordings, review stable delay and jitter, then save a correction with the room off. Long-term drift and physical synchronization require further measurements.'));
  if (typeof capabilities.sync === 'string') list.append(node('li', '', capabilities.sync));
  else if (capabilities.sync?.note) list.append(node('li', '', capabilities.sync.note));
  elements.capabilityDetails.replaceChildren(list);
}

async function login(event) {
  event.preventDefault();
  const token = elements.pairingToken.value;
  elements.pairingToken.value = '';
  const submit = elements.pairingForm.querySelector('button');
  submit.disabled = true;
  notice(elements.pairingError, '');
  try {
    await client.request('/session', { method: 'POST', body: { token } });
    store.authRequired = false;
    store.version += 1;
    view.cardSignature = '';
    await store.refresh();
    if (!store.authRequired) toast('Browser connected');
  } catch (error) { notice(elements.pairingError, error.message); }
  finally { submit.disabled = false; }
}

async function logout() {
  if (store.busy.size || !confirmDiscard()) return;
  elements.signOut.disabled = true;
  try {
    await client.request('/session', { method: 'DELETE' });
    store.requireAuth();
  } catch (error) { toast(error.message); }
  finally { elements.signOut.disabled = false; }
}

function draftDirty() { return !!view.roomDraft?.dirty || !!view.speakerDraft?.dirty || !!view.speakerDraft?.offsets.size || !!view.speakerDraft?.balances.size || !!view.calibrationDraft?.dirty; }
function confirmDiscard() { return !draftDirty() || window.confirm('Discard the unsaved room or speaker changes?'); }

function closeSheet(kind, { force = false } = {}) {
  const draft = kind === 'room' ? view.roomDraft : kind === 'calibration' ? view.calibrationDraft : view.speakerDraft;
  if (!force && draft && store.busy.has(draft.id || 'create')) return false;
  if (!force && (draft?.dirty || draft?.offsets?.size || draft?.balances?.size) && !window.confirm('Discard the unsaved changes?')) return false;
  if (kind === 'room') { view.roomDraft = null; elements.roomDialog.close(); }
  else if (kind === 'calibration') { view.calibrationDraft = null; elements.calibrationDialog.close(); }
  else { view.speakerDraft = null; elements.speakerDialog.close(); }
  return true;
}

function openRoom(id = null, { force = false } = {}) {
  if (!force && store.busy.has(id || 'create')) return;
  if (!force && !confirmDiscard()) return;
  closeSheet('speakers', { force: true });
  const room = id ? store.room(id) : null;
  if (id && !room) return;
  const savedDevice = room?.local_audio_device || null;
  view.roomDraft = { id, revision: room?.revision, dirty: false, local: {
    savedDevice, choice: savedDevice ? validLocalDeviceURI(savedDevice) ? 'saved' : 'advanced' : 'none',
    binding: null, devices: [], confirmed: new Map(), edited: false, loading: false, request: 0, error: '',
  } };
  elements.roomDialogTitle.textContent = room ? room.name : 'Add a room';
  elements.roomName.value = room?.name || '';
  elements.airplayName.value = room?.airplay_name || '';
  elements.noblyRoomId.value = room?.nobly_room_id || '';
  elements.localAudioDevice.value = savedDevice && !validLocalDeviceURI(savedDevice) ? savedDevice : '';
  elements.localAudioAdvanced.open = !!elements.localAudioDevice.value;
  renderLocalSpeaker();
  const interfaces = store.snapshot?.interfaces || [];
  const options = room?.interface && !interfaces.includes(room.interface) ? [room.interface, ...interfaces] : interfaces;
  elements.roomInterface.replaceChildren(...options.map((name) => {
    const option = node('option', '', `${name}${interfaces.includes(name) ? '' : ' (unavailable)'}`);
    option.value = name;
    return option;
  }));
  if (!options.length) { const option = node('option', '', 'No speaker network found'); option.value = ''; elements.roomInterface.append(option); }
  elements.roomInterface.value = room?.interface || interfaces[0] || '';
  elements.roomDuck.value = String(reductionPercent(room?.duck_gain ?? .28));
  elements.roomDuck.disabled = !room;
  elements.roomDuckValue.textContent = `${elements.roomDuck.value}%`;
  elements.saveRoom.textContent = room ? 'Save room' : 'Create room';
  elements.deleteRoomBlock.hidden = !room;
  elements.deleteRoom.disabled = !!room?.enabled;
  elements.deleteRoomHelp.textContent = room?.enabled ? 'Turn this room off from the dashboard before deleting it. Saved speaker assignments will be removed.' : 'Deleting removes this room and its saved speaker assignments.';
  elements.roomConnectionDetails.hidden = !room;
  elements.roomConnectionStatus.textContent = room ? `Service: ${room.runtime?.status || 'unknown'} · Network: ${room.interface}${room.runtime?.receiver_ip ? ` · AirPlay address: ${room.runtime.receiver_ip}` : ''}` : '';
  const ownTone = safeHttpUrl(room?.runtime?.owntone_url);
  elements.roomOwntone.hidden = !ownTone;
  if (ownTone) elements.roomOwntone.href = ownTone;
  else elements.roomOwntone.removeAttribute('href');
  elements.reloadRoomDraft.hidden = true;
  notice(elements.roomFormError, '');
  if (!elements.roomDialog.open) elements.roomDialog.showModal();
  elements.roomName.focus();
  loadLocalSpeakers(view.roomDraft);
}

function renderLocalSpeaker() {
  const local = view.roomDraft?.local;
  if (!local) return;
  const choices = [['none', 'Network speakers only']];
  if (validLocalDeviceURI(local.savedDevice)) {
    const saved = local.devices.find((item) => item.bindings.some((binding) => binding.device === local.savedDevice));
    choices.push(['saved', saved ? `Keep saved local speaker · ${saved.label}` : 'Saved local speaker (not currently available)']);
  }
  choices.push(...local.devices.map((device) => [device.selection_id, device.label]));
  if (!choices.some(([value]) => value === local.choice) && !['advanced', 'none'].includes(local.choice)) {
    choices.push([local.choice, 'Selected local speaker (not currently available)']);
  }
  choices.push(['advanced', 'Advanced Bluetooth or existing device']);
  elements.localSpeaker.replaceChildren(...choices.map(([value, label]) => {
    const option = node('option', '', label); option.value = value; return option;
  }));
  elements.localSpeaker.value = local.choice;
  const device = local.devices.find((item) => item.selection_id === local.choice);
  elements.localBindingFields.hidden = !device;
  elements.localSpeakerBinding.replaceChildren(...(device?.can_bind_by || []).map((mode) => {
    const option = node('option', '', localBindingNames[mode]); option.value = mode; return option;
  }));
  elements.localSpeakerBinding.value = local.binding || '';
  elements.localBindingHelp.textContent = localBindingHelp[local.binding] || '';
  if (local.choice === 'advanced') elements.localAudioAdvanced.open = true;
  const retained = validLocalDeviceURI(local.savedDevice) && local.choice === 'saved';
  elements.localSpeakerStatus.textContent = local.loading ? 'Checking connected local speakers…'
    : local.error ? `${local.error}${retained ? ' Your saved speaker is retained.' : ''}`
    : retained ? 'Your saved speaker is retained. Reconnect it or choose a replacement.'
    : device ? 'This speaker is verified when you save. Then select its local output in Room speakers.'
    : !['none', 'advanced'].includes(local.choice) ? 'This selection is no longer available. Refresh devices and choose it again before saving.'
    : local.devices.length ? 'Choose a connected local speaker, or use network speakers only.'
    : 'No local speakers found. You can still use network speakers or a configured Bluetooth device.';
}

async function loadLocalSpeakers(draft) {
  if (!draft || view.roomDraft !== draft || store.busy.has(draft.id || 'create')) return;
  const local = draft.local;
  const request = ++local.request;
  local.loading = true;
  local.error = '';
  renderLocalSpeaker();
  try {
    const result = await client.request('/local-devices');
    if (view.roomDraft !== draft || request !== local.request) return;
    if (!validLocalDeviceInventory(result)) throw new ApiError('Shiri returned an invalid local speaker inventory. Refresh devices.');
    local.devices = result.devices;
    if (!local.edited && validLocalDeviceURI(local.savedDevice)) {
      const device = local.devices.find((item) => item.bindings.some((binding) => binding.device === local.savedDevice));
      local.choice = device?.selection_id || 'saved';
      local.binding = device?.bindings.find((binding) => binding.device === local.savedDevice)?.binding || null;
    }
  } catch (error) {
    if (view.roomDraft !== draft || request !== local.request) return;
    if (error.status === 401) { store.requireAuth(); return; }
    local.error = error.message;
  } finally {
    if (view.roomDraft === draft && request === local.request) { local.loading = false; renderLocalSpeaker(); }
  }
}

function reloadRoom() {
  const id = view.roomDraft?.id;
  if (!id || !confirmDiscard()) return;
  openRoom(id, { force: true });
}

function patchRoom(room, changes) {
  return client.request(`/rooms/${encodeURIComponent(room.id)}`, { method: 'PATCH', body: { expected_revision: room.revision, changes } });
}

async function controlPlayer(id, action) {
  const result = await store.change(id, () => client.request(`/rooms/${encodeURIComponent(id)}/player`, { method: 'POST', body: { action } }));
  if (result.ok) toast(action === 'play' ? 'Playback requested' : 'Playback stopped');
}

async function saveRoom(event) {
  event.preventDefault();
  const draft = view.roomDraft;
  if (!draft) return;
  const changes = {
    name: elements.roomName.value.trim(), airplay_name: elements.airplayName.value.trim() || elements.roomName.value.trim(),
    nobly_room_id: elements.noblyRoomId.value || null, interface: elements.roomInterface.value,
    local_audio_device: null, duck_gain: 1 - Number(elements.roomDuck.value) / 100,
  };
  if (!changes.name || !changes.interface) { notice(elements.roomFormError, 'A room name and speaker network are required.'); return; }
  const local = draft.local;
  const choice = local.choice;
  const device = local.devices.find((item) => item.selection_id === choice);
  const binding = local.binding;
  const knownBinding = device?.bindings.find((item) => item.binding === binding);
  const advancedDevice = elements.localAudioDevice.value.trim() || null;
  if (!['none', 'saved', 'advanced'].includes(choice) && !knownBinding) {
    notice(elements.roomFormError, 'Refresh devices and choose an available local speaker before saving.'); return;
  }
  const result = await store.change(draft.id || 'create', async () => {
    if (choice === 'saved') changes.local_audio_device = local.savedDevice;
    else if (choice === 'advanced') changes.local_audio_device = advancedDevice;
    else if (knownBinding) {
      const confirmedKey = `${device.selection_id}:${binding}`;
      if (local.savedDevice && knownBinding.device === local.savedDevice) changes.local_audio_device = local.savedDevice;
      else if (local.confirmed.has(confirmedKey)) changes.local_audio_device = local.confirmed.get(confirmedKey);
      else {
        const ack = await client.request('/local-devices/bind', { method: 'POST', body: {
          selection_id: device.selection_id, binding, conversion: true,
        } });
        if (!validLocalBindingAck(ack, binding, knownBinding.device)) throw new ApiError('Shiri did not confirm the selected local speaker. Refresh devices before saving.');
        changes.local_audio_device = ack.device;
        // Keep the confirmed binding for a room-save retry without enrolling it again.
        local.confirmed.set(confirmedKey, ack.device);
      }
    }
    if (view.roomDraft !== draft) throw new ApiError('Room setup closed before saving. Open the room and review its settings.');
    const { duck_gain: _duckGain, ...definition } = changes;
    return draft.id
      ? client.request(`/rooms/${encodeURIComponent(draft.id)}`, { method: 'PATCH', body: { expected_revision: draft.revision, changes } })
      : client.request('/rooms', { method: 'POST', body: definition });
  });
  if (result.ok) {
    closeSheet('room', { force: true });
    toast(result.data?.runtime_accepted === false ? 'Room settings saved. Audio changes are pending; check room status.' : draft.id ? 'Room settings saved' : 'Room created. Turn it on, then choose its speakers.');
  } else if (!result.skipped && view.roomDraft === draft) {
    notice(elements.roomFormError, result.error.message);
    elements.reloadRoomDraft.hidden = !result.conflict;
  }
}

async function deleteRoom() {
  const draft = view.roomDraft;
  if (!draft?.id || !window.confirm('Delete this room? Its saved speaker assignments will be removed.')) return;
  const result = await store.change(draft.id, () => client.request(`/rooms/${encodeURIComponent(draft.id)}?expected_revision=${draft.revision}`, { method: 'DELETE' }));
  if (result.ok) { releaseSpeech(draft.id); closeSheet('room', { force: true }); toast('Room deleted'); }
  else if (!result.skipped) { notice(elements.roomFormError, result.error.message); elements.reloadRoomDraft.hidden = !result.conflict; }
}

function openSpeakers(id, { force = false } = {}) {
  if (!force && !confirmDiscard()) return;
  const room = store.room(id);
  if (!room) return;
  closeSheet('room', { force: true });
  view.speakerDraft = { id, revision: room.revision, selected: new Set(room.speakers.map((speaker) => speaker.id)), dirty: false, offsets: new Map(), balances: new Map() };
  elements.speakerDialogTitle.textContent = room.name;
  elements.reloadSpeakerDraft.hidden = true;
  notice(elements.speakerFormError, '');
  renderSpeakers();
  if (!elements.speakerDialog.open) elements.speakerDialog.showModal();
}

function reloadSpeakers() {
  const id = view.speakerDraft?.id;
  if (id && confirmDiscard()) openSpeakers(id, { force: true });
}

function renderSpeakers() {
  const draft = view.speakerDraft;
  if (!draft) return;
  const room = store.room(draft.id);
  if (!room) { notice(elements.speakerFormError, 'This room has been removed. Close this panel to return to your rooms.'); return; }
  elements.speakerDialogTitle.textContent = room.name;
  const locked = store.busy.has(room.id);
  const discoveryError = room.outputs_error;
  notice(elements.speakerDiscoveryStatus, !room.enabled
    ? 'Turn this room on to discover new speakers. You can remove saved assignments while it is off.'
    : discoveryError || (room.runtime?.status !== 'running' ? 'Waiting for this room’s audio service. Saved assignments do not confirm that speakers are online.' : 'Choose the speakers available on this room’s network.'), discoveryError ? 'error' : '');
  const outputs = Array.isArray(room.outputs) ? room.outputs : [];
  const combined = [...outputs];
  for (const saved of room.speakers) {
    if (!combined.some((output) => output.id === saved.id)) combined.push({ ...saved, available: false, selected: false, savedOnly: true });
  }
  // Rebuild discovery rows without replacing a number field someone is editing.
  if (elements.speakerOptions.contains(document.activeElement) && document.activeElement?.type === 'number') return;
  if (!combined.length) {
    elements.speakerOptions.replaceChildren(node('p', 'hint', room.enabled ? 'No speakers found yet. Keep them awake and on the speaker network, then refresh discovery.' : 'No speakers saved yet.'));
  } else {
    elements.speakerOptions.replaceChildren(...combined.map((output) => renderSpeakerOption(room, draft, output, locked)));
  }
  elements.saveSpeakers.disabled = locked || !canSaveSelection(room, draft);
  elements.refreshSpeakers.disabled = locked;
  elements.measureTiming.disabled = locked;
  elements.measureTiming.title = room.speakers.length < 2 ? 'Assign at least two speakers to measure relative arrival.' : 'Measure using two microphones sharing one capture clock.';
}

function renderSpeakerOption(room, draft, output, locked) {
  const row = node('div', 'speaker-option');
  const info = node('div');
  info.append(node('strong', '', output.name || output.id));
  const foreign = output.assigned_room_id && output.assigned_room_id !== room.id;
  const owner = foreign ? store.room(output.assigned_room_id)?.name || output.assigned_room_id : '';
  const unavailable = output.available === false || output.savedOnly;
  const restricted = output.assignable === false || output.requires_auth;
  const state = foreign ? `Assigned to ${owner}. Remove it from that room first.` : output.savedOnly ? 'Saved assignment · Not discovered' : unavailable ? 'Currently unavailable' : restricted ? output.reason || 'Device authorization or setup required' : 'Available';
  info.append(node('small', '', `${protocolName(output.protocol)} · ${state}`));
  if (output.sync_quality || output.synchronization) info.append(node('small', '', `Timing: ${output.sync_quality || output.synchronization}`));
  const check = node('input'); check.type = 'checkbox'; check.checked = draft.selected.has(output.id);
  check.setAttribute('aria-label', `Use ${output.name || output.id} in ${room.name}`);
  check.disabled = locked || !!foreign || (!!restricted || unavailable || !room.enabled || room.runtime?.status !== 'running') && !check.checked;
  check.addEventListener('change', () => {
    if (check.checked) draft.selected.add(output.id); else draft.selected.delete(output.id);
    draft.dirty = true;
    elements.saveSpeakers.disabled = locked || !canSaveSelection(room, draft);
  });
  row.append(info, check);
  const saved = room.speakers.find((speaker) => speaker.id === output.id);
  if (saved) {
    const timing = node('div', 'speaker-timing');
    const label = node('label', '', 'Saved timing offset (ms)');
    const offset = node('input'); offset.type = 'number'; offset.min = '-2000'; offset.max = '2000'; offset.step = '1';
    offset.value = String(draft.offsets.get(output.id) ?? saved.offset_ms ?? 0);
    offset.disabled = locked;
    offset.id = `offset-${room.id}-${output.id}`;
    label.htmlFor = offset.id;
    const save = button('Save offset', 'quiet', () => saveOffset(room.id, output.id));
    save.disabled = locked || !draft.offsets.has(output.id);
    offset.addEventListener('input', () => { draft.offsets.set(output.id, offset.value); save.disabled = !offset.validity.valid || store.busy.has(room.id); });
    const reported = output.savedOnly || output.offset_ms === undefined ? 'Not reported' : `${output.offset_ms} ms`;
    timing.append(label, offset, save, node('p', 'hint', `Saved: ${output.requested_offset_ms ?? saved.offset_ms ?? 0} ms · OwnTone reported: ${reported}. Positive values add delay. Applying a delay briefly restarts playback in this room. Reported settings do not establish measured synchronization.`));
    row.append(timing);
    const balancing = node('div', 'speaker-timing speaker-balance');
    const balanceLabel = node('label', '', 'Saved speaker balance (%)');
    const balance = node('input'); balance.type = 'number'; balance.min = '0'; balance.max = '100'; balance.step = '1';
    balance.value = String(draft.balances.get(output.id) ?? saved.balance_percent ?? 100);
    balance.disabled = locked;
    balance.id = `balance-${room.id}-${output.id}`;
    balanceLabel.htmlFor = balance.id;
    balance.setAttribute('aria-label', `Saved balance for ${output.name || output.id}`);
    const saveBalanceButton = button('Save balance', 'quiet', () => saveBalance(room.id, output.id));
    saveBalanceButton.disabled = locked || !draft.balances.has(output.id);
    balance.addEventListener('input', () => { draft.balances.set(output.id, balance.value); saveBalanceButton.disabled = !balance.validity.valid || store.busy.has(room.id); });
    const reportedBalance = Number.isInteger(output.balance_percent) ? `${output.balance_percent}%` : 'Not reported';
    balancing.append(balanceLabel, balance, saveBalanceButton, node('p', 'hint', `Saved: ${saved.balance_percent ?? 100}% · Applied: ${reportedBalance}. Set this once to balance a louder speaker. 100% keeps its full level; lower values reduce music and speech. Use Room volume for normal listening.`));
    row.append(balancing);
  }
  return row;
}

async function saveSpeakers(event) {
  event.preventDefault();
  const draft = view.speakerDraft;
  if (!draft || !draft.dirty) return;
  const ids = [...draft.selected];
  const result = await store.change(draft.id, () => client.request(`/rooms/${encodeURIComponent(draft.id)}/speakers`, { method: 'PUT', body: { expected_revision: draft.revision, speaker_ids: ids } }));
  if (result.ok) {
    draft.dirty = false;
    draft.revision = result.data?.room?.revision ?? store.room(draft.id)?.revision ?? draft.revision;
    toast(result.data?.runtime_accepted === false ? 'Speaker assignments saved. Playback changes are pending; check room status.' : 'Speaker assignments saved');
    if (!draft.offsets.size && !draft.balances.size) closeSheet('speakers', { force: true });
    else renderSpeakers();
  } else if (!result.skipped && view.speakerDraft === draft) {
    notice(elements.speakerFormError, result.error.message);
    elements.reloadSpeakerDraft.hidden = !result.conflict;
  }
}

async function saveOffset(roomId, speakerId) {
  const draft = view.speakerDraft;
  if (!draft || draft.id !== roomId) return;
  const raw = draft.offsets.get(speakerId);
  const offset = Number(raw);
  if (raw === undefined || raw === '' || !Number.isInteger(offset) || Math.abs(offset) > 2000) { notice(elements.speakerFormError, 'Use a whole timing offset between −2000 and 2000 ms.'); return; }
  const result = await store.change(roomId, () => client.request(`/rooms/${encodeURIComponent(roomId)}/speakers/${encodeURIComponent(speakerId)}/offset`, { method: 'PATCH', body: { expected_revision: draft.revision, offset_ms: offset } }));
  if (result.ok) {
    draft.offsets.delete(speakerId);
    draft.revision = result.data?.room?.revision ?? store.room(roomId)?.revision ?? draft.revision;
    notice(elements.speakerFormError, '');
    toast(result.data?.runtime_accepted === false ? 'Timing offset saved. Live application is pending; check room status.' : 'Timing offset saved. Check the OwnTone reported value.');
    renderSpeakers();
  } else if (!result.skipped && view.speakerDraft === draft) {
    notice(elements.speakerFormError, result.error.message);
    elements.reloadSpeakerDraft.hidden = !result.conflict;
  }
}

async function saveBalance(roomId, speakerId) {
  const draft = view.speakerDraft;
  if (!draft || draft.id !== roomId) return;
  const raw = draft.balances.get(speakerId);
  const balance = Number(raw);
  if (raw === undefined || raw === '' || !Number.isInteger(balance) || balance < 0 || balance > 100) { notice(elements.speakerFormError, 'Use a whole speaker balance between 0 and 100%.'); return; }
  const result = await store.change(roomId, () => client.request(`/rooms/${encodeURIComponent(roomId)}/speakers/${encodeURIComponent(speakerId)}/balance`, { method: 'PATCH', body: { expected_revision: draft.revision, balance_percent: balance } }));
  if (result.ok) {
    draft.balances.delete(speakerId);
    draft.revision = result.data?.room?.revision ?? store.room(roomId)?.revision ?? draft.revision;
    notice(elements.speakerFormError, '');
    toast(result.data?.runtime_accepted === false ? 'Speaker balance saved. Application is pending; check room status.' : 'Speaker balance saved');
    renderSpeakers();
  } else if (!result.skipped && view.speakerDraft === draft) {
    notice(elements.speakerFormError, result.error.message);
    elements.reloadSpeakerDraft.hidden = !result.conflict;
  }
}

function renderSheetBusy() {
  for (const [draft, form, key] of [[view.roomDraft, elements.roomForm, view.roomDraft?.id || 'create'], [view.speakerDraft, elements.speakerForm, view.speakerDraft?.id]]) {
    if (!draft) continue;
    const busy = store.busy.has(key);
    form.setAttribute('aria-busy', String(busy));
    for (const control of form.querySelectorAll('input, select, button')) {
      if (busy) { if (control.dataset.wasDisabled === undefined) control.dataset.wasDisabled = String(control.disabled); control.disabled = true; }
      else if (control.dataset.wasDisabled !== undefined) { control.disabled = control.dataset.wasDisabled === 'true'; delete control.dataset.wasDisabled; }
    }
  }
}

export function validCalibrationSession(session, roomId) {
  return !!session && session.room_id === roomId && typeof session.id === 'string' && session.id.length <= 128
    && typeof session.target_id === 'string' && typeof session.reference_id === 'string'
    && (session.target_id !== session.reference_id || typeof session.reference_room_id === 'string' && session.reference_room_id !== roomId)
    && (session.reference_room_id === undefined || typeof session.reference_room_id === 'string'
      && !!session.reference_configuration && Array.isArray(session.reference_configuration.speakers)
      && Number.isSafeInteger(session.reference_revision) && session.reference_revision >= 1
      && typeof session.playback_context === 'string' && session.playback_context.length > 0 && session.playback_context.length <= 512)
    && Number.isSafeInteger(session.generation) && session.generation >= 1
    && !!session.configuration && Array.isArray(session.configuration.speakers)
    && !!session.result && ['insufficient_evidence', 'candidate_correction'].includes(session.result.status)
    && Array.isArray(session.result.reasons) && session.result.reasons.every((reason) => typeof reason === 'string')
    && ['insufficient_evidence', 'candidate_correction', 'offset_saved', 'measured_at_this_setup', 'verification_failed', 'rolled_back'].includes(session.status)
    && [session.recordings, session.verification_recordings].every((records) => Array.isArray(records) && records.length <= 12
      && records.every((record) => !!record && Array.isArray(record.markers) && record.markers.length <= 8
        && record.markers.every((marker) => !!marker && typeof marker.accepted === 'boolean')))
    && (session.result.status !== 'candidate_correction' || Number.isInteger(session.result.candidate_offset_ms) && Math.abs(session.result.candidate_offset_ms) <= 2000);
}

function calibrationConfiguration(configuration) {
  const speakers = configuration.speakers.map((speaker) => {
    const value = { ...speaker };
    // The default balance preserves stored calibration profiles from before
    // balance settings existed. A changed balance still invalidates evidence.
    if (value.balance_percent === 100) delete value.balance_percent;
    return value;
  });
  return { ...configuration, speakers };
}

export function calibrationMatchesRoom(room, session, referenceRoom = room) {
  if (!room || room.id !== session?.room_id || !session.configuration) return false;
  const expected = { ...session.configuration, speakers: session.configuration.speakers.map((speaker) => ({ ...speaker })) };
  if (session.applied_revision !== null && session.applied_revision !== undefined && session.status !== 'rolled_back') {
    for (const speaker of expected.speakers) if (speaker.id === session.target_id) speaker.offset_ms = session.applied_offset_ms;
  }
  const observed = Object.fromEntries(Object.keys(expected).map((key) => [key, room[key]]));
  if (JSON.stringify(calibrationConfiguration(expected)) !== JSON.stringify(calibrationConfiguration(observed))) return false;
  if ((session.reference_room_id || room.id) === room.id) return true;
  if (!referenceRoom || referenceRoom.id !== session.reference_room_id || !session.reference_configuration) return false;
  const referenceObserved = Object.fromEntries(Object.keys(session.reference_configuration).map((key) => [key, referenceRoom[key]]));
  return JSON.stringify(calibrationConfiguration(referenceObserved)) === JSON.stringify(calibrationConfiguration(session.reference_configuration));
}

export function calibrationEvidenceScope(session) {
  const recordings = Array.isArray(session?.recordings) ? session.recordings : [];
  const verification = Array.isArray(session?.verification_recordings) ? session.verification_recordings : [];
  const backend = Array.isArray(session?.verification_backend) ? session.verification_backend : [];
  const observations = [...recordings, ...verification].map((record) => record?.environment_at_import);
  observations.push(...backend);
  // Today's runtime mode cannot change the scope of retained observations.
  // Missing legacy observations remain unknown, even when the current runtime
  // is real. Neither a real observation nor imported PCM certifies capture origin.
  return {
    simulated: observations.some((observation) => observation?.simulation === true),
    unknown: verification.length !== backend.length
      || session?.status === 'measured_at_this_setup' && !verification.length
      || observations.some((observation) => typeof observation?.simulation !== 'boolean' || observation?.runtime_unavailable === true),
  };
}

const calibrationPath = (roomId, sessionId = '') => `/rooms/${encodeURIComponent(roomId)}/calibration${sessionId ? `/${encodeURIComponent(sessionId)}` : ''}`;
const measuredNumber = (value, suffix = ' ms') => Number.isFinite(value) ? `${value.toFixed(3)}${suffix}` : 'Not measured';

async function openCalibration(roomId) {
  if (!roomId || store.busy.has(roomId) || !confirmDiscard()) return;
  const room = store.room(roomId);
  if (!room) return;
  closeSheet('speakers', { force: true });
  closeSheet('room', { force: true });
  view.calibrationDraft = { id: roomId, revision: room.revision, dirty: false, loading: false };
  elements.calibrationForm.reset();
  elements.calibrationUpload.reset();
  const rooms = store.snapshot?.rooms || [];
  elements.calibrationReferenceRoom.replaceChildren(...rooms.map((item) => {
    const option = node('option', '', item.name); option.value = item.id; return option;
  }));
  elements.calibrationReferenceRoom.value = room.speakers.length < 2 ? (rooms.find((item) => item.id !== room.id && item.speakers.length)?.id || room.id) : room.id;
  elements.calibrationTarget.replaceChildren(...room.speakers.map((speaker) => {
      const option = node('option', '', speaker.name); option.value = speaker.id; return option;
    }));
  elements.calibrationTarget.value = room.speakers[1]?.id || room.speakers[0]?.id || '';
  populateCalibrationReference();
  notice(elements.calibrationError, '');
  renderCalibration();
  if (!elements.calibrationDialog.open) elements.calibrationDialog.showModal();
  await refreshCalibration();
}

function populateCalibrationReference() {
  const draft = view.calibrationDraft;
  if (!draft) return;
  const reference = store.room(elements.calibrationReferenceRoom.value);
  elements.calibrationReference.replaceChildren(...(reference?.speakers || []).map((speaker) => {
    const option = node('option', '', speaker.name); option.value = speaker.id; return option;
  }));
  elements.calibrationContext.required = reference?.id !== draft.id;
}

async function refreshCalibration() {
  const draft = view.calibrationDraft;
  if (!draft || draft.loading || store.authRequired || store.busy.has(draft.id)) return;
  draft.loading = true;
  renderCalibration();
  try {
    const previous = view.calibrationSessions.get(draft.id);
    const data = await client.request(calibrationPath(draft.id));
    if (view.calibrationDraft !== draft || store.authRequired) return;
    if (!Array.isArray(data?.sessions) || data.sessions.length > 128 || data.sessions.some((item) => !validCalibrationSession(item, draft.id))) throw new ApiError('Calibration returned incomplete evidence. Refresh to check the session.');
    view.calibrationHistory.set(draft.id, data.sessions);
    const session = data.sessions.find((item) => item.id === previous?.id) || (!draft.newSession ? data.sessions.at(-1) : undefined);
    if (session) view.calibrationSessions.set(draft.id, session);
    else view.calibrationSessions.delete(draft.id);
  } catch (error) {
    if (error.status === 401) store.requireAuth();
    else if (view.calibrationDraft === draft) {
      if (error.status === 404) view.calibrationSessions.delete(draft.id);
      notice(elements.calibrationError, error.message);
    }
  } finally { draft.loading = false; if (view.calibrationDraft === draft) renderCalibration(); }
}

async function startCalibration(event) {
  event.preventDefault();
  const draft = view.calibrationDraft;
  if (!draft || draft.loading || view.calibrationSessions.has(draft.id)) return;
  const body = { expected_revision: store.room(draft.id)?.revision ?? draft.revision, reference_id: elements.calibrationReference.value,
    target_id: elements.calibrationTarget.value, capture_device: elements.calibrationDevice.value.trim(),
    geometry: elements.calibrationGeometry.value.trim(), max_lag_ms: Number(elements.calibrationMaxLag.value),
    geometry_correction_ms: Number(elements.calibrationGeometryCorrection.value) };
  const reference = store.room(elements.calibrationReferenceRoom.value);
  if (!reference) { notice(elements.calibrationError, 'Choose a currently configured reference zone.'); return; }
  body.reference_room_id = reference.id;
  body.expected_reference_revision = reference.revision;
  if (elements.calibrationContext.value.trim()) body.playback_context = elements.calibrationContext.value.trim();
  if (reference.id === draft.id && body.reference_id === body.target_id) { notice(elements.calibrationError, 'Choose two different room/speaker endpoints.'); return; }
  const result = await store.change(draft.id, async () => {
    const session = await client.request(calibrationPath(draft.id), { method: 'POST', body });
    if (!validCalibrationSession(session, draft.id)) throw new ApiError('Shiri did not confirm the measurement session. Refresh before starting another.', { ambiguous: true });
    return session;
  });
  if (result.ok && !store.authRequired) {
    draft.dirty = false;
    view.calibrationSessions.set(draft.id, result.data);
    view.calibrationHistory.set(draft.id, [...(view.calibrationHistory.get(draft.id) || []), result.data]);
    draft.newSession = false;
    notice(elements.calibrationError, '');
  } else if (!result.skipped && view.calibrationDraft === draft) notice(elements.calibrationError, result.error.message);
  renderCalibration();
}

async function uploadCalibration(event) {
  event.preventDefault();
  const draft = view.calibrationDraft;
  const session = draft && view.calibrationSessions.get(draft.id);
  const file = elements.calibrationFile.files[0];
  if (!session || !file || draft.loading) return;
  if (file.size > 2 * 1024 * 1024 || !file.size) { notice(elements.calibrationError, 'Import a complete stereo PCM WAV no larger than 2 MiB.'); return; }
  const verification = session.applied_revision !== null && session.applied_revision !== undefined;
  if (verification && !elements.calibrationVerifyConfirm.checked) { notice(elements.calibrationError, 'Confirm this is a fresh recording made after applying the saved delay.'); return; }
  const result = await store.change(draft.id, async () => {
    const data = await client.request(`${calibrationPath(draft.id, session.id)}/recordings${verification ? '?verification=true' : ''}`, { method: 'POST', rawBody: file });
    if (!validCalibrationSession(data, draft.id)) throw new ApiError('Shiri did not confirm the analysis. Refresh the session before uploading again.', { ambiguous: true });
    return data;
  });
  if (result.ok && !store.authRequired) {
    view.calibrationSessions.set(draft.id, result.data);
    view.calibrationHistory.set(draft.id, (view.calibrationHistory.get(draft.id) || []).map((item) => item.id === result.data.id ? result.data : item));
    notice(elements.calibrationError, '');
    elements.calibrationUpload.reset();
  } else if (!result.skipped && view.calibrationDraft === draft) notice(elements.calibrationError, result.error.message);
  renderCalibration();
}

async function calibrationRoomToggle(enabled) {
  const draft = view.calibrationDraft;
  const room = draft && store.room(draft.id);
  if (!room || draft.loading) return;
  const result = await store.change(room.id, () => patchRoom(room, { enabled }));
  if (result.ok) notice(elements.calibrationError, result.data?.runtime_accepted === false ? 'Room setting saved; runtime application is pending. Check room status.' : '', 'warning');
  else if (!result.skipped && view.calibrationDraft === draft) notice(elements.calibrationError, result.error.message);
  renderCalibration();
}

async function calibrationReferenceOn() {
  const draft = view.calibrationDraft;
  const session = draft && view.calibrationSessions.get(draft.id);
  const reference = session && store.room(session.reference_room_id);
  if (!reference || reference.id === draft.id) return;
  const result = await store.change(reference.id, () => client.request(`/rooms/${encodeURIComponent(reference.id)}`, { method: 'PATCH', body: { expected_revision: reference.revision, changes: { enabled: true } } }));
  if (result.ok) notice(elements.calibrationError, result.data?.runtime_accepted === false ? 'Reference zone setting saved; runtime application is pending.' : '', 'warning');
  else if (!result.skipped && view.calibrationDraft === draft) notice(elements.calibrationError, result.error.message);
  renderCalibration();
}

async function applyCalibration(rollback) {
  const draft = view.calibrationDraft;
  const room = draft && store.room(draft.id);
  const session = draft && view.calibrationSessions.get(draft.id);
  if (!room || !session || draft.loading || room.enabled) return;
  const result = await store.change(room.id, async () => {
    const reference = store.room(session.reference_room_id || room.id);
    const data = await client.request(`${calibrationPath(room.id, session.id)}/${rollback ? 'rollback' : 'apply'}`, { method: 'POST', body: { expected_revision: room.revision, expected_generation: session.generation, expected_reference_revision: reference?.revision } });
    if (!validCalibrationSession(data?.calibration, room.id)) throw new ApiError('Shiri did not confirm the timing receipt. Refresh before changing it again.', { ambiguous: true });
    return data;
  });
  if (result.ok && !store.authRequired) {
    view.calibrationSessions.set(room.id, result.data.calibration);
    view.calibrationHistory.set(room.id, (view.calibrationHistory.get(room.id) || []).map((item) => item.id === session.id ? result.data.calibration : item));
    notice(elements.calibrationError, '');
    toast(rollback ? 'Previous delay saved. Enable the room to apply it.' : 'Candidate delay saved. Fresh recordings are needed to verify it.');
  } else if (!result.skipped && view.calibrationDraft === draft) notice(elements.calibrationError, result.error.message);
  renderCalibration();
}

async function endCalibration() {
  const draft = view.calibrationDraft;
  const session = draft && view.calibrationSessions.get(draft.id);
  if (!session || draft.loading) return;
  const result = await store.change(draft.id, () => client.request(calibrationPath(draft.id, session.id), { method: 'DELETE' }));
  if (result.ok && !store.authRequired) {
    view.calibrationSessions.delete(draft.id);
    view.calibrationHistory.set(draft.id, (view.calibrationHistory.get(draft.id) || []).filter((item) => item.id !== session.id));
    draft.newSession = true;
    draft.revision = store.room(draft.id)?.revision;
    notice(elements.calibrationError, 'Session evidence removed. Saved speaker delays remain unchanged.', 'warning');
  } else if (!result.skipped && view.calibrationDraft === draft) notice(elements.calibrationError, result.error.message);
  renderCalibration();
}

function renderCalibration() {
  const draft = view.calibrationDraft;
  if (!draft || store.authRequired) return;
  const room = store.room(draft.id);
  if (!room) { notice(elements.calibrationError, 'This room was removed. Close this panel.'); return; }
  elements.calibrationTitle.textContent = `${room.name} · Timing`;
  const session = view.calibrationSessions.get(draft.id);
  const referenceRoom = session && store.room(session.reference_room_id || draft.id);
  const busy = store.busy.has(draft.id) || store.busy.has(referenceRoom?.id) || draft.loading;
  const canAnalyze = store.snapshot?.capabilities?.calibration_analysis !== false;
  const history = view.calibrationHistory.get(draft.id) || [];
  elements.calibrationHistoryControls.hidden = !history.length;
  const signature = JSON.stringify(history.map((item) => [item.id, item.status]));
  if (elements.calibrationHistory.dataset.signature !== signature) {
    elements.calibrationHistory.replaceChildren(node('option', '', 'Choose a retained measurement'), ...history.map((item) => {
      const speaker = room.speakers.find((output) => output.id === item.target_id)?.name || item.target_id;
      const option = node('option', '', `${speaker} · ${item.status.replaceAll('_', ' ')} · ${new Date(item.created_at * 1000).toLocaleString()}`);
      option.value = item.id; return option;
    }));
    elements.calibrationHistory.options[0].value = '';
    elements.calibrationHistory.dataset.signature = signature;
  }
  elements.calibrationHistory.value = session?.id || '';
  elements.calibrationHistory.disabled = elements.calibrationNew.disabled = busy;
  elements.calibrationForm.hidden = !!session || draft.loading;
  elements.calibrationSession.hidden = !session;
  for (const control of elements.calibrationForm.querySelectorAll('input, select, button')) control.disabled = busy;
  elements.calibrationStart.disabled = busy || !canAnalyze || room.speakers.length < 1;
  if (!session && !busy && (!canAnalyze || room.speakers.length < 1)) notice(elements.calibrationError, !canAnalyze ? 'Analysis requires the optional audio dependency. Retained evidence and rollback remain available.' : 'Assign a target speaker before starting a measurement. Retained evidence remains available.', 'warning');
  if (!session) return;
  const applied = session.applied_revision !== null && session.applied_revision !== undefined;
  const rolledBack = session.status === 'rolled_back';
  const matches = calibrationMatchesRoom(room, session, referenceRoom);
  const candidate = session.result.status === 'candidate_correction';
  const labels = { insufficient_evidence: 'More reliable recordings needed', candidate_correction: 'Stable correction ready for review', offset_saved: 'Delay saved · Verification needed', measured_at_this_setup: 'Alignment measured at this setup', verification_failed: 'Correction did not align recorded arrivals', rolled_back: 'Previous delay restored' };
  const simulated = store.snapshot?.runtime.simulation === true;
  const scope = calibrationEvidenceScope(session);
  const verifiedLabel = scope.simulated ? 'Recorded alignment verified · Simulated observations'
    : scope.unknown ? 'Recorded alignment verified · Runtime evidence incomplete'
    : simulated ? 'Recorded alignment verified · Current runtime simulated' : labels.measured_at_this_setup;
  const label = !matches ? 'Settings changed · Evidence is historical' : session.status === 'measured_at_this_setup' ? verifiedLabel : labels[session.status] || 'Measurement status unavailable';
  notice(elements.calibrationStatus, busy ? 'Checking measurement…' : label, !simulated && !scope.simulated && !scope.unknown && matches && session.status === 'measured_at_this_setup' ? 'good' : 'warning');
  elements.calibrationProbe.href = `/api/v1${calibrationPath(room.id, session.id)}/probe.wav`;
  elements.calibrationExport.href = `/api/v1${calibrationPath(room.id, session.id)}/export`;
  elements.calibrationApply.disabled = busy || room.enabled || applied || !candidate || !matches;
  elements.calibrationRollback.disabled = busy || room.enabled || !applied || rolledBack || !matches;
  elements.calibrationOff.hidden = !room.enabled;
  elements.calibrationOff.disabled = busy;
  elements.calibrationOn.hidden = room.enabled || !applied || rolledBack;
  elements.calibrationOn.disabled = busy;
  elements.calibrationReferenceOn.hidden = !applied || rolledBack || !referenceRoom || referenceRoom.id === room.id || referenceRoom.enabled;
  elements.calibrationReferenceOn.disabled = busy;
  elements.calibrationRefresh.disabled = elements.calibrationEnd.disabled = busy;
  elements.calibrationVerifyConfirm.parentElement.hidden = !applied;
  elements.calibrationVerifyConfirm.required = applied;
  for (const control of elements.calibrationUpload.querySelectorAll('input, button')) control.disabled = busy || !canAnalyze || rolledBack || !matches || applied && (!room.enabled || !referenceRoom?.enabled);
  elements.calibrationImport.textContent = applied ? 'Analyze fresh verification' : 'Analyze recording';
  const target = room.speakers.find((speaker) => speaker.id === session.target_id)?.name || session.target_id;
  const reference = referenceRoom?.speakers.find((speaker) => speaker.id === session.reference_id)?.name || session.reference_id;
  const crossZone = (session.reference_room_id || room.id) !== room.id;
  elements.calibrationPlaybackHelp.textContent = crossZone ? `Play this exact WAV from one source through ${room.name} and ${referenceRoom?.name || 'the retained reference zone'}, selected together in the phone's native group. Recreate that group after re-enabling the target. Stop normal music and spoken replies for the probe. Group membership remains operator declared.` : 'Play this exact WAV through the measured room from your music source. Stop normal music and spoken replies for the probe.';
  elements.calibrationApplyHelp.textContent = !matches ? 'Speaker settings changed. Export the evidence and start a new session; this correction is stale.' : session.status === 'verification_failed' ? 'Fresh recordings still show more than 1 ms of residual delay. Turn this room off to restore the previous delay, then export the evidence and start a new session to investigate.' : room.enabled ? 'Turn the room off before saving or restoring a delay. This stops its receiver and music connection.' : 'Timing changes save while this room is off. Re-enable it, confirm OwnTone readback, then import fresh recordings. Calibration never changes live speech or music gain.';
  const analysis = session.verification || session.result;
  const result = node('div');
  if (scope.simulated) result.append(node('p', 'notice warning', 'Retained simulated observations: some runtime observations were simulated when this evidence was imported. Changing the runtime mode does not upgrade that evidence or prove physical playback or synchronization.'));
  if (scope.unknown) result.append(node('p', 'notice warning', 'Retained runtime observations are incomplete. The imported waveforms can show recorded alignment, but this evidence does not establish physical playback or synchronization.'));
  if (simulated) result.append(node('p', 'notice warning', 'Current runtime simulation: new speaker selection and offset readback are simulated. This does not change the scope of earlier recordings; physical playback and synchronization remain unproven.'));
  result.append(node('h3', '', `${target}${crossZone ? ` (${room.name})` : ''} relative to ${reference}${crossZone ? ` (${referenceRoom?.name || session.reference_room_id})` : ''}`));
  result.append(node('p', 'hint', `Playback context: ${session.playback_context || 'Same-room playback'}. Operator declared; imported audio does not certify the phone’s native grouping.`));
  const metrics = node('dl');
  for (const [label, value] of [['Median arrival lag', measuredNumber(analysis.median_lag_ms)], ['Arrival variation (MAD)', measuredNumber(analysis.mad_ms)], ['Observed uncertainty', measuredNumber(analysis.observed_uncertainty_ms)], ['5th / 95th percentile', `${measuredNumber(analysis.p05_ms)} / ${measuredNumber(analysis.p95_ms)}`], ['Valid steady-state markers', `${analysis.accepted_markers ?? 0} · ${analysis.rejected_markers ?? 0} rejected`], ['Lowest correlation', measuredNumber(analysis.minimum_confidence, '')]]) metrics.append(node('dt', '', label), node('dd', '', value));
  result.append(metrics, node('p', 'hint', 'Positive arrival lag means the target is later. A positive offset delays the target. Correlation is a waveform-match score, not a probability of correct speaker identity. A constant correction cannot fix variable network jitter or clock drift.'));
  if (candidate) result.append(node('p', '', `Reviewed candidate for ${target}: ${session.previous_offset_ms} ms → ${session.result.candidate_offset_ms} ms. ${applied ? 'Saved; compare fresh verification evidence.' : 'No offset has been changed.'}`));
  const reasons = node('ul');
  for (const reason of analysis.reasons || []) reasons.append(node('li', '', reason));
  if (reasons.childElementCount) result.append(reasons);
  for (const drift of analysis.drift_estimates || []) result.append(node('p', 'hint', `Short-window drift: ${measuredNumber(drift.ms_per_minute, ' ms/min')} ± ${measuredNumber(drift.uncertainty_ms_per_minute, ' ms/min')} over ${measuredNumber(drift.span_seconds, ' seconds')}. Long-term drift remains untested.`));
  const details = node('details', 'room-connection-details');
  details.append(node('summary', '', 'Recording evidence and rejected markers'));
  for (const [index, record] of [...session.recordings, ...session.verification_recordings].entries()) {
    const accepted = record.markers?.filter((marker) => marker.accepted).length || 0;
    details.append(node('p', '', `Take ${index + 1}: ${record.sample_rate} Hz · ${accepted}/8 markers accepted · PCM hash ${record.pcm_sha256?.slice(0, 12) || 'unavailable'}`));
    for (const marker of record.markers || []) if (!marker.accepted) details.append(node('p', 'hint', `Marker ${marker.marker + 1}: ${marker.reason}`));
  }
  const retention = Number.isFinite(session.expires_at) ? `Evidence retained until ${new Date(session.expires_at * 1000).toLocaleString()}, subject to the 128-session limit.` : 'Export evidence you need to keep.';
  result.append(details, node('p', 'hint', `Capture: ${session.capture_device}. Geometry: ${session.geometry}. ${retention} Saved correction receipts and summaries survive API restarts. Deleting evidence also removes this session’s rollback receipt; saved delays remain unchanged. Raw recordings are not retained. Physical phone, speaker and long-term synchronization tests are still required.`));
  elements.calibrationResult.replaceChildren(result);
}

async function loadEvents() {
  if (store.authRequired) return;
  if (view.eventsRequest) return view.eventsRequest;
  const version = ++view.eventsVersion;
  view.eventsRequest = (async () => {
    try {
      const data = await client.request('/events');
      if (version !== view.eventsVersion || store.authRequired) return;
      const events = Array.isArray(data) ? data : data?.events;
      if (!Array.isArray(events)) throw new ApiError('Activity could not be read. Refresh to try again.');
      notice(elements.eventsError, '');
      elements.eventList.replaceChildren(...events.slice(-100).reverse().map((entry) => {
        const row = node('li', entry.level === 'error' ? 'error' : '');
        const rawTime = entry.at ?? entry.timestamp ?? entry.created_at;
        const date = typeof rawTime === 'number' ? new Date(rawTime * 1000) : new Date(rawTime);
        const time = node('time', '', Number.isNaN(date.valueOf()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
        row.append(time, node('span', '', entry.message || entry.error || entry.type || 'Service event'));
        return row;
      }));
      if (!events.length) elements.eventList.append(node('li', '', 'No recent activity.'));
    } catch (error) {
      if (error.status === 401) store.requireAuth();
      else notice(elements.eventsError, error.message);
    } finally { view.eventsRequest = null; }
  })();
  return view.eventsRequest;
}

function speechSupported() { return !!globalThis.isSecureContext && typeof globalThis.RTCPeerConnection === 'function' && typeof (globalThis.AudioContext || globalThis.webkitAudioContext) === 'function'; }

async function testSpeech(id) {
  if (view.speech.has(id)) { await stopSpeech(id); return; }
  const room = store.room(id);
  if (!room || !speechSupported()) return;
  const Audio = globalThis.AudioContext || globalThis.webkitAudioContext;
  const context = new Audio();
  const destination = context.createMediaStreamDestination();
  const gain = context.createGain(); gain.gain.value = .07; gain.connect(destination);
  const oscillator = context.createOscillator(); oscillator.frequency.value = 660; oscillator.connect(gain);
  const peer = new RTCPeerConnection({ iceServers: [] });
  const sessionId = crypto.randomUUID();
  const session = { peer, context, oscillator, sessionId, timer: null, ready: false, offered: false, closed: false };
  view.speech.set(id, session);
  renderCards();
  try {
    await context.resume();
    for (const track of destination.stream.getAudioTracks()) peer.addTrack(track, destination.stream);
    await peer.setLocalDescription(await peer.createOffer());
    await waitForIce(peer, 5000);
    if (session.closed) return;
    session.offered = true;
    const answer = await client.request(`/rooms/${encodeURIComponent(id)}/speech`, { method: 'POST', body: { action: 'offer', session_id: sessionId, request_id: crypto.randomUUID(), sdp: peer.localDescription.sdp, type: 'offer' } });
    session.ready = true;
    if (session.closed) { await stopSpeech(id, session); return; }
    await peer.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    await waitForConnection(peer, 10000);
    if (session.closed) return;
    const now = context.currentTime;
    gain.gain.setValueAtTime(0, now);
    gain.gain.linearRampToValueAtTime(.07, now + .05);
    gain.gain.setValueAtTime(.07, now + .65);
    gain.gain.linearRampToValueAtTime(0, now + .8);
    oscillator.start(now);
    oscillator.stop(now + .85);
    toast(`Audio test sent to ${room.name}. Confirm you hear it in that room.`);
    session.timer = setTimeout(() => stopSpeech(id, session), 1600);
  } catch (error) {
    if (session.closed) return;
    if (error.status === 401) store.requireAuth();
    else toast(error.message || 'This browser could not establish the room audio test.');
    await stopSpeech(id, session);
  }
}

function releaseSpeech(id, expected = view.speech.get(id)) {
  if (!expected) return;
  expected.closed = true;
  clearTimeout(expected.timer);
  expected.peer.close();
  try { expected.oscillator.stop(); } catch {}
  expected.context.close().catch(() => {});
  if (view.speech.get(id) === expected) view.speech.delete(id);
  view.cardSignature = '';
}

async function stopSpeech(id, expected = view.speech.get(id)) {
  if (!expected) return;
  releaseSpeech(id, expected);
  renderCards();
  if (!expected.offered) return;
  try { await client.request(`/rooms/${encodeURIComponent(id)}/speech`, { method: 'POST', body: { action: 'close', session_id: expected.sessionId, request_id: crypto.randomUUID() } }); }
  catch (error) { if (!store.authRequired) toast(error.message); }
}

function waitForIce(peer, deadline) {
  if (peer.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error('Browser audio discovery timed out. Try the test again.')), deadline);
    const listener = () => { if (peer.iceGatheringState === 'complete') finish(); };
    function finish(error) { clearTimeout(timer); peer.removeEventListener('icegatheringstatechange', listener); error ? reject(error) : resolve(); }
    peer.addEventListener('icegatheringstatechange', listener);
    listener();
  });
}

function waitForConnection(peer, deadline) {
  if (peer.connectionState === 'connected') return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error('Room audio did not connect in time. Check the room network.')), deadline);
    const listener = () => {
      if (peer.connectionState === 'connected') finish();
      if (['failed', 'closed'].includes(peer.connectionState)) finish(new Error('The room audio connection failed.'));
    };
    function finish(error) { clearTimeout(timer); peer.removeEventListener('connectionstatechange', listener); error ? reject(error) : resolve(); }
    peer.addEventListener('connectionstatechange', listener);
    listener();
  });
}
