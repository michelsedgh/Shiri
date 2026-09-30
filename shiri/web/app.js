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

  async request(path, { method = 'GET', body, deadlineMs = method === 'GET' ? this.deadlineMs : Math.max(this.deadlineMs, 45000) } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), deadlineMs);
    const writing = method !== 'GET';
    try {
      const response = await this.fetcher(`/api/v1${path}`, {
        method, credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
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

const client = new ApiClient();
const store = new RoomStore(client);
const view = { roomDraft: null, speakerDraft: null, toastTimer: null, eventsRequest: null, eventsVersion: 0, speech: new Map(), cardSignature: '', pollTimer: null };
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
  elements.deleteRoom.addEventListener('click', deleteRoom);
  elements.reloadRoomDraft.addEventListener('click', () => reloadRoom());
  elements.speakerForm.addEventListener('submit', saveSpeakers);
  elements.refreshSpeakers.addEventListener('click', async () => { await store.refresh(); renderSpeakers(); });
  elements.reloadSpeakerDraft.addEventListener('click', reloadSpeakers);
  elements.refreshEvents.addEventListener('click', loadEvents);
  elements.diagnostics.addEventListener('toggle', () => { if (elements.diagnostics.open) loadEvents(); });
  for (const element of document.querySelectorAll('[data-close]')) element.addEventListener('click', () => closeSheet(element.dataset.close));
  for (const [kind, dialog] of [['room', elements.roomDialog], ['speakers', elements.speakerDialog]]) {
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
  list.append(node('li', '', 'Music input: AirPlay. Generic Cast receiver input is not provided.'));
  list.append(node('li', '', 'Spoken replies: room-addressed WebRTC audio. Nobly integration uses the room ID you configure.'));
  list.append(node('li', '', 'Outputs: available OwnTone AirPlay and Cast devices; local ALSA audio when configured. Cast timing is approximate.'));
  list.append(node('li', '', 'Bluetooth: a paired local adapter and working ALSA/BlueALSA output are required.'));
  list.append(node('li', '', 'Saved timing offsets are applied by OwnTone and checked against its reported values. Microphone measurement and drift correction are separate steps.'));
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

function draftDirty() { return !!view.roomDraft?.dirty || !!view.speakerDraft?.dirty || !!view.speakerDraft?.offsets.size; }
function confirmDiscard() { return !draftDirty() || window.confirm('Discard the unsaved room or speaker changes?'); }

function closeSheet(kind, { force = false } = {}) {
  const draft = kind === 'room' ? view.roomDraft : view.speakerDraft;
  if (!force && draft && store.busy.has(draft.id || 'create')) return false;
  if (!force && (draft?.dirty || draft?.offsets?.size) && !window.confirm('Discard the unsaved changes?')) return false;
  if (kind === 'room') { view.roomDraft = null; elements.roomDialog.close(); }
  else { view.speakerDraft = null; elements.speakerDialog.close(); }
  return true;
}

function openRoom(id = null, { force = false } = {}) {
  if (!force && store.busy.has(id || 'create')) return;
  if (!force && !confirmDiscard()) return;
  closeSheet('speakers', { force: true });
  const room = id ? store.room(id) : null;
  if (id && !room) return;
  view.roomDraft = { id, revision: room?.revision, dirty: false };
  elements.roomDialogTitle.textContent = room ? room.name : 'Add a room';
  elements.roomName.value = room?.name || '';
  elements.airplayName.value = room?.airplay_name || '';
  elements.noblyRoomId.value = room?.nobly_room_id || '';
  elements.localAudioDevice.value = room?.local_audio_device || '';
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
    local_audio_device: elements.localAudioDevice.value || null, duck_gain: 1 - Number(elements.roomDuck.value) / 100,
  };
  if (!changes.name || !changes.interface) { notice(elements.roomFormError, 'A room name and speaker network are required.'); return; }
  const { duck_gain: _duckGain, ...definition } = changes;
  const result = await store.change(draft.id || 'create', () => draft.id
    ? client.request(`/rooms/${encodeURIComponent(draft.id)}`, { method: 'PATCH', body: { expected_revision: draft.revision, changes } })
    : client.request('/rooms', { method: 'POST', body: definition }));
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
  view.speakerDraft = { id, revision: room.revision, selected: new Set(room.speakers.map((speaker) => speaker.id)), dirty: false, offsets: new Map() };
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
    if (!draft.offsets.size) closeSheet('speakers', { force: true });
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
