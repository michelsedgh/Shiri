import { Api, ApiError } from './api.js';
import {
    activityText,
    clampNumber,
    debounce,
    escapeHtml,
    formatPct,
    nowRequestId,
    roomLabel,
    selectedSpeakerText,
    statusClass,
} from './utils.js';

const state = {
    dashboard: null,
    activeRoomId: null,
    activeDrawerTab: 'setup',
    diagnosticsOpen: false,
    logsPaused: false,
    socket: null,
    testClips: [],
};

const els = {};
const refreshSoon = debounce(() => loadDashboard({ quiet: true }), 700);

document.addEventListener('DOMContentLoaded', init);

async function init() {
    bindElements();
    bindEvents();
    connectSocket();
    await loadDashboard();
    window.setInterval(() => loadDashboard({ quiet: true }), 8000);
}

function bindElements() {
    for (const id of [
        'room-count',
        'shiri-status',
        'lionos-status',
        'default-room',
        'console-subtitle',
        'refresh-dashboard',
        'open-diagnostics',
        'open-settings',
        'open-injector',
        'global-error',
        'room-list',
        'room-drawer',
        'drawer-room-source',
        'drawer-room-name',
        'drawer-setup',
        'drawer-speakers',
        'drawer-advanced',
        'close-room-drawer',
        'diagnostics-panel',
        'diag-room-filter',
        'diag-type-filter',
        'toggle-live-logs',
        'refresh-logs',
        'close-diagnostics',
        'log-feed',
        'inject-panel',
        'inject-built-in-form',
        'inject-upload-form',
        'inject-room',
        'inject-clip',
        'inject-file',
        'close-injector',
        'settings-panel',
        'settings-form',
        'lionos-base-url',
        'settings-zones',
        'refresh-settings',
        'create-zone-form',
        'new-zone-name',
        'new-zone-room-id',
        'new-zone-room-name',
        'new-zone-interface',
        'new-zone-autostart',
        'new-zone-default',
        'close-settings',
        'toast',
    ]) {
        els[toCamel(id)] = document.getElementById(id);
    }
}

function bindEvents() {
    els.refreshDashboard.addEventListener('click', () => loadDashboard());
    els.openInjector.addEventListener('click', () => openInjector());
    els.closeInjector.addEventListener('click', closeInjector);
    els.injectBuiltInForm.addEventListener('submit', onInjectBuiltIn);
    els.injectUploadForm.addEventListener('submit', onInjectUpload);
    els.openSettings.addEventListener('click', openSettings);
    els.closeSettings.addEventListener('click', closeSettings);
    els.openDiagnostics.addEventListener('click', openDiagnostics);
    els.closeDiagnostics.addEventListener('click', closeDiagnostics);
    els.closeRoomDrawer.addEventListener('click', closeRoomDrawer);
    els.refreshLogs.addEventListener('click', loadLogs);
    els.toggleLiveLogs.addEventListener('click', toggleLiveLogs);
    els.diagRoomFilter.addEventListener('change', loadLogs);
    els.diagTypeFilter.addEventListener('change', loadLogs);
    els.refreshSettings.addEventListener('click', renderSettings);
    els.settingsForm.addEventListener('submit', onSaveSettings);
    els.createZoneForm.addEventListener('submit', onCreateZone);

    els.roomList.addEventListener('click', onRoomListClick);
    els.roomList.addEventListener('input', onRangeInput);
    els.roomList.addEventListener('change', onRoomListChange);

    els.roomDrawer.addEventListener('click', onDrawerClick);
    els.roomDrawer.addEventListener('input', onRangeInput);
    els.roomDrawer.addEventListener('change', onDrawerChange);

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        closeRoomDrawer();
        closeDiagnostics();
        closeInjector();
        closeSettings();
    });
}

async function loadDashboard({ quiet = false } = {}) {
    try {
        const dashboard = await Api.dashboard();
        state.dashboard = dashboard;
        renderDashboard();
        if (state.activeRoomId) renderRoomDrawer();
        if (state.diagnosticsOpen) renderDiagnosticsFilters();
        if (els.injectPanel?.classList.contains('open')) renderInjector(els.injectRoom.value);
        if (!quiet) showToast('Dashboard refreshed');
    } catch (error) {
        showError(error);
    }
}

function renderDashboard() {
    const dashboard = state.dashboard;
    if (!dashboard) return;
    const rooms = dashboard.rooms || [];
    const running = dashboard.system?.running_zones ?? 0;
    const total = dashboard.system?.zone_count ?? 0;

    els.roomCount.textContent = `${rooms.length} room${rooms.length === 1 ? '' : 's'}`;
    els.consoleSubtitle.textContent = `Updated ${new Date((dashboard.generated_at || Date.now() / 1000) * 1000).toLocaleTimeString()}`;
    renderStatusPill(els.shiriStatus, total ? `${running}/${total} zones running` : 'No zones', total ? (running ? 'good' : 'warn') : 'bad');
    renderLionosStatus();
    renderDefaultRoom();

    const lionError = dashboard.lionos?.online ? '' : dashboard.lionos?.error;
    if (lionError && !dashboard.lionos?.cached) {
        els.globalError.hidden = false;
        els.globalError.textContent = `LionOS offline: ${lionError}`;
    } else {
        els.globalError.hidden = true;
        els.globalError.textContent = '';
    }

    if (!rooms.length) {
        els.roomList.innerHTML = '<div class="empty-state">No rooms or zones found</div>';
        return;
    }
    els.roomList.innerHTML = rooms.map(renderRoomRow).join('');
}

function renderStatusPill(el, text, tone) {
    el.className = `status-pill ${tone}`;
    el.innerHTML = `<span class="dot"></span><span>${escapeHtml(text)}</span>`;
}

function renderLionosStatus() {
    const lionos = state.dashboard?.lionos || {};
    if (lionos.online) {
        renderStatusPill(els.lionosStatus, 'LionOS online', 'good');
    } else if (lionos.cached) {
        renderStatusPill(els.lionosStatus, 'LionOS cached', 'warn');
    } else {
        renderStatusPill(els.lionosStatus, 'LionOS offline', 'bad');
    }
}

function renderDefaultRoom() {
    const roomId = state.dashboard?.default_room_id;
    const room = findRoom(roomId);
    els.defaultRoom.className = `status-pill ${room ? 'good' : 'muted'}`;
    els.defaultRoom.innerHTML = `<span>${room ? `Default: ${escapeHtml(roomLabel(room))}` : 'Default room unset'}</span>`;
}

function renderRoomRow(room) {
    const binding = room.binding;
    const policy = policyFor(room);
    const roomSettings = policy.room || {};
    const mode = policy.mode || 'room';
    const volume = binding?.volume ?? binding?.player?.volume ?? 50;
    const isRunning = binding?.status === 'running';
    const disabled = !binding || !isRunning;
    const status = binding?.status || 'unbound';
    const speakers = binding?.speakers || [];
    const volumeValue = clampNumber(volume, 0, 100, 50);
    const ttsLevel = clampNumber(roomSettings.tts_level_pct, 5, 200, 100);
    const reduction = clampNumber(roomSettings.reduction_pct, 0, 95, 72);
    const activity = activityText(room);
    const roomTtsDisabled = !binding || mode === 'speaker';
    const ttsLabel = mode === 'speaker' ? 'Room fallback' : 'Room TTS';
    const reductionLabel = mode === 'speaker' ? 'Fallback duck' : 'Reduction';

    return `
        <article class="room-row ${binding ? '' : 'unbound'}" data-room-id="${escapeHtml(room.room_id)}">
            <div class="room-cell">
                <div class="room-title">
                    <h3 title="${escapeHtml(roomLabel(room))}">${escapeHtml(roomLabel(room))}</h3>
                    <span class="source-badge">${room.source === 'lionos' ? 'LionOS' : 'Shiri'}</span>
                </div>
                <div class="room-meta">
                    <span>${escapeHtml(activity)}</span>
                    <span>${escapeHtml(room.room_id)}</span>
                </div>
            </div>
            <div class="room-cell">
                <div class="route-line">
                    <span class="state-badge ${statusClass(status)}">${escapeHtml(status)}</span>
                    <strong title="${escapeHtml(binding?.zone_name || 'Unbound')}">${escapeHtml(binding?.zone_name || 'Unbound')}</strong>
                </div>
                <div class="speaker-summary" title="${escapeHtml(selectedSpeakerText(speakers))}">
                    ${escapeHtml(selectedSpeakerText(speakers))}
                </div>
            </div>
            <div class="room-cell">
                <div class="control-bank">
                    <div class="control">
                        <label for="vol-${escapeHtml(room.room_id)}">Playback</label>
                        <div class="range-line">
                            <input id="vol-${escapeHtml(room.room_id)}" type="range" min="0" max="100" value="${volumeValue}" data-action="room-volume" data-room-id="${escapeHtml(room.room_id)}" ${disabled ? 'disabled' : ''}>
                            <output>${volumeValue}%</output>
                        </div>
                    </div>
                    <div class="control">
                        <label for="tts-${escapeHtml(room.room_id)}">${ttsLabel}</label>
                        <div class="range-line">
                            <input id="tts-${escapeHtml(room.room_id)}" type="range" min="5" max="200" value="${ttsLevel}" data-action="room-tts-level" data-room-id="${escapeHtml(room.room_id)}" ${roomTtsDisabled ? 'disabled' : ''}>
                            <output>${ttsLevel}%</output>
                        </div>
                    </div>
                    <div class="control">
                        <label for="duck-${escapeHtml(room.room_id)}">${reductionLabel}</label>
                        <div class="range-line">
                            <input id="duck-${escapeHtml(room.room_id)}" type="range" min="0" max="95" value="${reduction}" data-action="room-reduction" data-room-id="${escapeHtml(room.room_id)}" ${roomTtsDisabled ? 'disabled' : ''}>
                            <output>${reduction}%</output>
                        </div>
                    </div>
                    <div class="control">
                        <label>Scope</label>
                        <div class="segmented" data-room-id="${escapeHtml(room.room_id)}">
                            <button data-action="tts-mode" data-mode="room" class="${mode === 'room' ? 'active' : ''}" ${binding ? '' : 'disabled'}>Room</button>
                            <button data-action="tts-mode" data-mode="speaker" class="${mode === 'speaker' ? 'active' : ''}" ${binding ? '' : 'disabled'}>Speaker</button>
                        </div>
                    </div>
                </div>
            </div>
            <div class="room-cell">
                <div class="row-actions">
                    <button class="small-btn" data-action="zone-start" data-zone-id="${escapeHtml(binding?.zone_id || '')}" ${binding?.can_start ? '' : 'disabled'}>Start</button>
                    <button class="small-btn" data-action="zone-stop" data-zone-id="${escapeHtml(binding?.zone_id || '')}" ${binding?.can_stop ? '' : 'disabled'}>Stop</button>
                    <button class="small-btn" data-action="test-tts" data-room-id="${escapeHtml(room.room_id)}" ${isRunning ? '' : 'disabled'}>Inject</button>
                    <button class="small-btn" data-action="room-details" data-room-id="${escapeHtml(room.room_id)}">Details</button>
                </div>
            </div>
        </article>
    `;
}

function onRangeInput(event) {
    if (event.target.type !== 'range') return;
    const output = (
        event.target.closest('.range-line')?.querySelector('output')
        || event.target.closest('.speaker-controls')?.querySelector('output')
    );
    if (output) output.textContent = `${event.target.value}%`;
}

async function onRoomListClick(event) {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    try {
        if (action === 'zone-start') {
            await Api.startZone(button.dataset.zoneId);
            showToast('Zone starting');
            refreshSoon();
        } else if (action === 'zone-stop') {
            await Api.stopZone(button.dataset.zoneId);
            showToast('Zone stopping');
            refreshSoon();
        } else if (action === 'room-details') {
            openRoomDrawer(button.dataset.roomId);
        } else if (action === 'test-tts') {
            openInjector(button.dataset.roomId);
        } else if (action === 'tts-mode') {
            await savePolicyMode(button.closest('[data-room-id]')?.dataset.roomId, button.dataset.mode);
        }
    } catch (error) {
        showError(error);
    }
}

async function onRoomListChange(event) {
    const input = event.target;
    if (input.type !== 'range') return;
    const roomId = input.dataset.roomId;
    try {
        if (input.dataset.action === 'room-volume') {
            await Api.setRoomVolume(roomId, clampNumber(input.value, 0, 100, 50));
            showToast('Playback volume saved');
        } else if (input.dataset.action === 'room-tts-level' || input.dataset.action === 'room-reduction') {
            await saveRoomPolicyFromRow(roomId);
        }
        refreshSoon();
    } catch (error) {
        showError(error);
    }
}

async function saveRoomPolicyFromRow(roomId) {
    const room = findRoom(roomId);
    const row = els.roomList.querySelector(`[data-room-id="${cssEscape(roomId)}"]`);
    if (!room || !row) return;
    const policy = policyFor(room);
    if (policy.mode === 'speaker') return;
    policy.room = {
        tts_level_pct: clampNumber(row.querySelector('[data-action="room-tts-level"]')?.value, 5, 200, 100),
        reduction_pct: clampNumber(row.querySelector('[data-action="room-reduction"]')?.value, 0, 95, 72),
    };
    await Api.setRoomTtsPolicy(roomId, policy);
    showToast('TTS levels saved');
}

async function savePolicyMode(roomId, mode) {
    const room = findRoom(roomId);
    if (!room) return;
    const policy = policyFor(room);
    policy.mode = mode;
    await Api.setRoomTtsPolicy(roomId, policy);
    showToast(`TTS scope set to ${mode}`);
    await loadDashboard({ quiet: true });
}

async function openInjector(roomId = '') {
    els.injectPanel.classList.add('open');
    els.injectPanel.setAttribute('aria-hidden', 'false');
    await renderInjector(roomId);
}

function closeInjector() {
    els.injectPanel.classList.remove('open');
    els.injectPanel.setAttribute('aria-hidden', 'true');
}

async function renderInjector(selectedRoomId = '') {
    if (!state.testClips.length) {
        try {
            const data = await Api.testClips();
            state.testClips = data.clips || [];
        } catch (error) {
            showError(error);
        }
    }
    const rooms = (state.dashboard?.rooms || []).filter((room) => room.binding?.status === 'running');
    const currentRoomId = selectedRoomId || els.injectRoom.value || rooms[0]?.room_id || '';
    els.injectRoom.innerHTML = rooms.map((room) => (
        `<option value="${escapeHtml(room.room_id)}" ${room.room_id === currentRoomId ? 'selected' : ''}>${escapeHtml(roomLabel(room))}</option>`
    )).join('');
    els.injectClip.innerHTML = state.testClips.map((clip) => (
        `<option value="${escapeHtml(clip.id)}">${escapeHtml(clip.name)} (${escapeHtml(`${clip.seconds}s`)})</option>`
    )).join('');
}

async function onInjectBuiltIn(event) {
    event.preventDefault();
    const roomId = els.injectRoom.value;
    const clipId = els.injectClip.value;
    if (!roomId || !clipId) {
        showToast('Select a running room and clip');
        return;
    }
    try {
        await Api.injectTestClip(roomId, {
            clip_id: clipId,
            request_id: nowRequestId(`ui_inject_${clipId}`),
            text: `Injected ${clipId} test clip`,
        });
        showToast('Test clip injected');
    } catch (error) {
        showError(error);
    }
}

async function onInjectUpload(event) {
    event.preventDefault();
    const roomId = els.injectRoom.value;
    const file = els.injectFile.files?.[0];
    if (!roomId || !file) {
        showToast('Select a running room and WAV file');
        return;
    }
    const form = new FormData();
    form.append('audio', file);
    form.append('request_id', nowRequestId('ui_upload'));
    form.append('format', 'wav');
    form.append('text', file.name || 'Uploaded test WAV');
    try {
        await Api.uploadTestClip(roomId, form);
        els.injectFile.value = '';
        showToast('Uploaded WAV injected');
    } catch (error) {
        showError(error);
    }
}

function openRoomDrawer(roomId) {
    state.activeRoomId = roomId;
    state.activeDrawerTab = 'setup';
    els.roomDrawer.classList.add('open');
    els.roomDrawer.setAttribute('aria-hidden', 'false');
    renderRoomDrawer();
}

function closeRoomDrawer() {
    state.activeRoomId = null;
    els.roomDrawer.classList.remove('open');
    els.roomDrawer.setAttribute('aria-hidden', 'true');
}

function renderRoomDrawer() {
    const room = findRoom(state.activeRoomId);
    if (!room) return;
    els.drawerRoomName.textContent = roomLabel(room);
    els.drawerRoomSource.textContent = room.source === 'lionos' ? 'LionOS room' : 'Shiri room';

    els.roomDrawer.querySelectorAll('.tab').forEach((tab) => {
        tab.classList.toggle('active', tab.dataset.drawerTab === state.activeDrawerTab);
    });
    for (const key of ['setup', 'speakers', 'advanced']) {
        els[`drawer${capitalize(key)}`].hidden = key !== state.activeDrawerTab;
    }
    renderDrawerSetup(room);
    renderDrawerSpeakers(room);
    renderDrawerAdvanced(room);
}

function renderDrawerSetup(room) {
    const zones = state.dashboard?.zones || [];
    const selectedZoneId = room.binding?.zone_id || '';
    els.drawerSetup.innerHTML = `
        <div class="drawer-stack">
            <div class="drawer-block">
                <label class="field">
                    <span>Bound Shiri zone</span>
                    <select id="binding-zone">
                        <option value="">Select zone</option>
                        ${zones.map((zone) => `<option value="${escapeHtml(zone.zone_id)}" ${zone.zone_id === selectedZoneId ? 'selected' : ''}>${escapeHtml(zone.zone_name)} (${escapeHtml(zone.status)})</option>`).join('')}
                    </select>
                </label>
                <label class="check-field">
                    <input id="binding-default-room" type="checkbox" ${room.binding?.default_room ? 'checked' : ''}>
                    <span>Default TTS room</span>
                </label>
                <button class="primary-btn" data-action="save-binding" data-room-id="${escapeHtml(room.room_id)}">Save Binding</button>
            </div>
            <div class="drawer-block">
                <div class="advanced-row">
                    <div>
                        <strong>Room id</strong>
                        <span>${escapeHtml(room.room_id)}</span>
                    </div>
                    <span class="mode-badge">${escapeHtml(room.source)}</span>
                </div>
                <div class="advanced-row">
                    <div>
                        <strong>Presence</strong>
                        <span>${escapeHtml(activityText(room))}</span>
                    </div>
                    <span>${room.activities?.length || 0}</span>
                </div>
            </div>
        </div>
    `;
}

function renderDrawerSpeakers(room) {
    const binding = room.binding;
    if (!binding) {
        els.drawerSpeakers.innerHTML = '<div class="empty-state">Bind a Shiri zone first</div>';
        return;
    }
    const speakers = binding.speakers || [];
    const policy = policyFor(room);
    if (!speakers.length) {
        els.drawerSpeakers.innerHTML = '<div class="empty-state">No speakers discovered or saved</div>';
        return;
    }
    const enabledSpeakers = speakers.filter((speaker) => speaker.selected);
    const modeSummary = policy.mode === 'speaker'
        ? `${enabledSpeakers.length} speaker${enabledSpeakers.length === 1 ? '' : 's'} using overrides`
        : 'Main row room controls active';
    const ttsBlock = policy.mode === 'speaker'
        ? renderSpeakerTtsBlock(room, binding, policy, enabledSpeakers)
        : '';
    els.drawerSpeakers.innerHTML = `
        <div class="drawer-stack">
            <div class="drawer-block speaker-mode-block">
                <div>
                    <strong>TTS scope</strong>
                    <span>${policy.mode === 'speaker' ? 'Per speaker' : 'Per room'}</span>
                </div>
                <span class="mode-badge">${escapeHtml(modeSummary)}</span>
            </div>
            <div class="drawer-block">
                <div class="section-title">
                    <h3>Routing</h3>
                    <span class="mode-badge">${enabledSpeakers.length}/${speakers.length} enabled</span>
                </div>
                <div class="speaker-route-list">
                    ${speakers.map((speaker) => renderSpeakerRouteRow(binding, speaker)).join('')}
                </div>
                <button class="primary-btn" data-action="save-speakers" data-zone-id="${escapeHtml(binding.zone_id)}">Save Routing</button>
            </div>
            ${ttsBlock}
        </div>
    `;
}

function renderSpeakerRouteRow(binding, speaker) {
    const speakerId = String(speaker.id ?? '');
    const volume = clampNumber(speaker.volume, 0, 100, 100);
    const selected = !!speaker.selected;
    return `
        <div class="speaker-row speaker-route-row" data-speaker-id="${escapeHtml(speakerId)}" data-speaker-name="${escapeHtml(speaker.name || '')}">
            <div>
                <strong>${escapeHtml(speaker.name || speakerId || 'Speaker')}</strong>
                <span>${selected ? 'enabled' : 'available'} / ${escapeHtml(speakerId || 'no id')}</span>
            </div>
            <label class="check-field">
                <input type="checkbox" data-field="selected" ${selected ? 'checked' : ''}>
                <span>Route</span>
            </label>
            ${selected ? `
                <div class="speaker-controls">
                    <span>Volume</span>
                    <input type="range" min="0" max="100" value="${volume}" data-action="speaker-volume" data-zone-id="${escapeHtml(binding.zone_id)}" data-speaker-id="${escapeHtml(speakerId)}" ${binding.status === 'running' ? '' : 'disabled'}>
                    <output>${volume}%</output>
                </div>
            ` : ''}
        </div>
    `;
}

function renderSpeakerTtsBlock(room, binding, policy, enabledSpeakers) {
    if (!enabledSpeakers.length) {
        return `
            <div class="drawer-block">
                <div class="section-title">
                    <h3>Per-Speaker TTS</h3>
                    <span class="mode-badge">0 enabled</span>
                </div>
                <div class="empty-state">No enabled speakers in this room</div>
            </div>
        `;
    }
    return `
        <div class="drawer-block">
            <div class="section-title">
                <h3>Per-Speaker TTS</h3>
                <span class="mode-badge">${enabledSpeakers.length} enabled</span>
            </div>
            <div class="speaker-tts-list">
                ${enabledSpeakers.map((speaker) => renderSpeakerTtsRow(binding, policy, speaker)).join('')}
            </div>
            <button class="primary-btn" data-action="save-speaker-tts" data-room-id="${escapeHtml(room.room_id)}">Save Per-Speaker TTS</button>
        </div>
    `;
}

function renderSpeakerTtsRow(binding, policy, speaker) {
    const speakerId = String(speaker.id ?? '');
    const override = policy.speakers?.[speakerId] || policy.room || {};
    const tts = clampNumber(override.tts_level_pct, 5, 200, 100);
    const reduction = clampNumber(override.reduction_pct, 0, 95, 72);
    return `
        <div class="speaker-row speaker-tts-row" data-speaker-id="${escapeHtml(speakerId)}" data-speaker-name="${escapeHtml(speaker.name || '')}">
            <div>
                <strong>${escapeHtml(speaker.name || speakerId || 'Speaker')}</strong>
                <span>enabled / ${escapeHtml(speakerId || 'no id')}</span>
            </div>
            <div class="speaker-tts-grid">
                <div class="control">
                    <label>TTS</label>
                    <div class="range-line">
                        <input type="range" min="5" max="200" value="${tts}" data-field="tts_level_pct">
                        <output>${tts}%</output>
                    </div>
                </div>
                <div class="control">
                    <label>Reduction</label>
                    <div class="range-line">
                        <input type="range" min="0" max="95" value="${reduction}" data-field="reduction_pct">
                        <output>${reduction}%</output>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function renderDrawerAdvanced(room) {
    const binding = room.binding;
    if (!binding) {
        els.drawerAdvanced.innerHTML = '<div class="empty-state">Bind a Shiri zone first</div>';
        return;
    }
    const interfaces = state.dashboard?.system?.interfaces || [];
    const ownTonePort = binding.owntone_port ?? 3689;
    els.drawerAdvanced.innerHTML = `
        <div class="drawer-stack">
            <label class="field">
                <span>AirPlay name</span>
                <input id="advanced-zone-name" type="text" value="${escapeHtml(binding.zone_name)}">
            </label>
            <label class="field">
                <span>Network interface</span>
                <select id="advanced-zone-interface">
                    ${interfaces.map((iface) => `<option value="${escapeHtml(iface)}" ${iface === binding.interface ? 'selected' : ''}>${escapeHtml(iface)}</option>`).join('')}
                </select>
            </label>
            <label class="field">
                <span>Latency offset</span>
                <input id="advanced-zone-latency" type="number" min="-10" max="5" step="0.1" value="${escapeHtml(binding.latency_offset ?? -2.3)}">
            </label>
            <label class="check-field">
                <input id="advanced-zone-autostart" type="checkbox" ${binding.auto_start ? 'checked' : ''}>
                <span>Auto-start</span>
            </label>
            <button class="primary-btn" data-action="save-zone-advanced" data-zone-id="${escapeHtml(binding.zone_id)}" data-room-id="${escapeHtml(room.room_id)}">Save Zone</button>
            <div class="advanced-row">
                <div>
                    <strong>OwnTone</strong>
                    <span>${binding.owntone_ip ? `${escapeHtml(binding.owntone_ip)}:${escapeHtml(ownTonePort)}` : 'not running'}</span>
                </div>
                ${binding.owntone_ip ? `<a class="small-btn" href="http://${escapeHtml(binding.owntone_ip)}:${escapeHtml(ownTonePort)}" target="_blank" rel="noreferrer">Open</a>` : '<span></span>'}
            </div>
            <div class="advanced-row">
                <div>
                    <strong>Runtime</strong>
                    <span>host ports / subdev ${escapeHtml(binding.allocated_subdevice ?? '-')}</span>
                </div>
                <span class="state-badge ${statusClass(binding.status)}">${escapeHtml(binding.status)}</span>
            </div>
            <button class="danger-btn" data-action="delete-zone" data-zone-id="${escapeHtml(binding.zone_id)}">Delete Zone</button>
        </div>
    `;
}

async function onDrawerClick(event) {
    const tab = event.target.closest('[data-drawer-tab]');
    if (tab) {
        state.activeDrawerTab = tab.dataset.drawerTab;
        renderRoomDrawer();
        return;
    }

    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    try {
        if (action === 'save-binding') await saveBinding(button.dataset.roomId);
        if (action === 'save-speakers') await saveSpeakers(button.dataset.zoneId);
        if (action === 'save-speaker-tts') await saveSpeakerTts(button.dataset.roomId);
        if (action === 'save-zone-advanced') await saveZoneAdvanced(button.dataset.zoneId, button.dataset.roomId);
        if (action === 'delete-zone') await deleteZone(button.dataset.zoneId);
    } catch (error) {
        showError(error);
    }
}

async function onDrawerChange(event) {
    const input = event.target;
    if (input.dataset.action === 'speaker-volume') {
        try {
            await Api.setSpeakerVolume(input.dataset.zoneId, input.dataset.speakerId, clampNumber(input.value, 0, 100, 100));
            showToast('Speaker volume saved');
        } catch (error) {
            showError(error);
        }
    }
}

async function saveBinding(roomId) {
    const zoneId = document.getElementById('binding-zone')?.value;
    if (!zoneId) throw new Error('Select a Shiri zone');
    const room = findRoom(roomId);
    await Api.bindRoom(roomId, {
        zone_id: zoneId,
        room_name: roomLabel(room),
        default_room: document.getElementById('binding-default-room')?.checked,
    });
    showToast('Room binding saved');
    await loadDashboard({ quiet: true });
}

async function saveSpeakers(zoneId) {
    const speakerIds = [...els.drawerSpeakers.querySelectorAll('.speaker-route-row')]
        .filter((row) => row.querySelector('[data-field="selected"]')?.checked)
        .map((row) => row.dataset.speakerId)
        .filter(Boolean);
    await Api.setSpeakers(zoneId, speakerIds);
    showToast('Speaker selection saved');
    await loadDashboard({ quiet: true });
}

async function saveSpeakerTts(roomId) {
    const room = findRoom(roomId);
    const policy = policyFor(room);
    const speakers = {};
    els.drawerSpeakers.querySelectorAll('.speaker-tts-row').forEach((row) => {
        if (!row.dataset.speakerId) return;
        speakers[row.dataset.speakerId] = {
            name: row.dataset.speakerName || undefined,
            tts_level_pct: clampNumber(row.querySelector('[data-field="tts_level_pct"]')?.value, 5, 200, 100),
            reduction_pct: clampNumber(row.querySelector('[data-field="reduction_pct"]')?.value, 0, 95, 72),
        };
    });
    policy.mode = 'speaker';
    policy.speakers = speakers;
    await Api.setRoomTtsPolicy(roomId, policy);
    showToast('Speaker TTS saved');
    await loadDashboard({ quiet: true });
}

async function saveZoneAdvanced(zoneId, roomId) {
    await Api.updateZone(zoneId, {
        name: document.getElementById('advanced-zone-name')?.value?.trim(),
        interface: document.getElementById('advanced-zone-interface')?.value,
        latency_offset: Number(document.getElementById('advanced-zone-latency')?.value),
        auto_start: document.getElementById('advanced-zone-autostart')?.checked,
        room_id: roomId,
        room_name: roomLabel(findRoom(roomId)),
    });
    showToast('Zone saved');
    await loadDashboard({ quiet: true });
}

async function deleteZone(zoneId) {
    if (!window.confirm('Delete this Shiri zone?')) return;
    await Api.deleteZone(zoneId);
    showToast('Zone deleted');
    closeRoomDrawer();
    await loadDashboard({ quiet: true });
}

async function openSettings() {
    els.settingsPanel.classList.add('open');
    els.settingsPanel.setAttribute('aria-hidden', 'false');
    await renderSettings();
}

function closeSettings() {
    els.settingsPanel.classList.remove('open');
    els.settingsPanel.setAttribute('aria-hidden', 'true');
}

async function renderSettings() {
    const dashboard = state.dashboard || await Api.dashboard();
    state.dashboard = dashboard;
    els.lionosBaseUrl.value = dashboard.settings?.lionos_base_url || '';
    await renderInterfaceOptions();
    els.settingsZones.innerHTML = (dashboard.zones || []).map((zone) => `
        <div class="settings-row">
            <div>
                <strong>${escapeHtml(zone.zone_name)}</strong>
                <span>${escapeHtml(zone.room_id)} / ${escapeHtml(zone.status)} / ${escapeHtml(zone.interface || 'no interface')}</span>
            </div>
            <button class="small-btn" type="button" data-settings-zone="${escapeHtml(zone.zone_id)}">Open</button>
        </div>
    `).join('') || '<div class="empty-state">No zones</div>';
    els.settingsZones.querySelectorAll('[data-settings-zone]').forEach((button) => {
        button.addEventListener('click', () => {
            const zone = (state.dashboard?.zones || []).find((item) => item.zone_id === button.dataset.settingsZone);
            const room = (state.dashboard?.rooms || []).find((item) => item.binding?.zone_id === zone?.zone_id);
            if (room) {
                closeSettings();
                openRoomDrawer(room.room_id);
            }
        });
    });
}

async function renderInterfaceOptions() {
    const data = await Api.interfaces();
    const interfaces = data.interfaces || [];
    els.newZoneInterface.innerHTML = interfaces.map((iface) => `<option value="${escapeHtml(iface)}">${escapeHtml(iface)}</option>`).join('');
}

async function onSaveSettings(event) {
    event.preventDefault();
    try {
        await Api.saveSettings({ lionos_base_url: els.lionosBaseUrl.value.trim() });
        showToast('Settings saved');
        await loadDashboard({ quiet: true });
    } catch (error) {
        showError(error);
    }
}

async function onCreateZone(event) {
    event.preventDefault();
    try {
        await Api.createZone({
            name: els.newZoneName.value.trim(),
            interface: els.newZoneInterface.value,
            room_id: els.newZoneRoomId.value.trim() || els.newZoneName.value.trim(),
            room_name: els.newZoneRoomName.value.trim() || els.newZoneName.value.trim(),
            auto_start: els.newZoneAutostart.checked,
            default_room: els.newZoneDefault.checked,
        });
        event.target.reset();
        showToast('Zone created');
        await loadDashboard({ quiet: true });
        await renderSettings();
    } catch (error) {
        showError(error);
    }
}

function openDiagnostics() {
    state.diagnosticsOpen = true;
    state.logsPaused = false;
    els.toggleLiveLogs.textContent = 'Pause';
    els.diagnosticsPanel.classList.add('open');
    els.diagnosticsPanel.setAttribute('aria-hidden', 'false');
    renderDiagnosticsFilters();
    subscribeLogs(true);
    loadLogs();
}

function closeDiagnostics() {
    state.diagnosticsOpen = false;
    els.diagnosticsPanel.classList.remove('open');
    els.diagnosticsPanel.setAttribute('aria-hidden', 'true');
    subscribeLogs(false);
}

function renderDiagnosticsFilters() {
    const rooms = state.dashboard?.rooms || [];
    const selected = els.diagRoomFilter.value;
    els.diagRoomFilter.innerHTML = `<option value="">All rooms</option>${rooms.map((room) => `<option value="${escapeHtml(room.room_id)}">${escapeHtml(roomLabel(room))}</option>`).join('')}`;
    els.diagRoomFilter.value = selected || '';
}

async function loadLogs() {
    if (!state.diagnosticsOpen) return;
    try {
        const data = await Api.logs({
            roomId: els.diagRoomFilter.value,
            type: els.diagTypeFilter.value,
            lines: 280,
        });
        const entries = data.entries || [];
        els.logFeed.innerHTML = entries.length
            ? entries.map(renderLogEntry).join('')
            : '<div class="empty-state">No matching log lines</div>';
        els.logFeed.scrollTop = els.logFeed.scrollHeight;
    } catch (error) {
        showError(error);
    }
}

function toggleLiveLogs() {
    state.logsPaused = !state.logsPaused;
    els.toggleLiveLogs.textContent = state.logsPaused ? 'Resume' : 'Pause';
}

function renderLogEntry(entry) {
    const time = entry.timestamp ? new Date(entry.timestamp * 1000).toLocaleTimeString() : '';
    return `
        <div class="log-entry ${escapeHtml(entry.severity || 'info')}">
            <span>${escapeHtml(time || entry.log_type || '')}</span>
            <span>${escapeHtml(entry.room_name || entry.room_id || '')}</span>
            <span>${escapeHtml(entry.category || entry.log_type || '')}</span>
            <span class="log-line">${escapeHtml(entry.line || '')}</span>
        </div>
    `;
}

function appendLogEntry(entry) {
    if (!state.diagnosticsOpen || state.logsPaused) return;
    if (els.diagRoomFilter.value && entry.room_id !== els.diagRoomFilter.value) return;
    const filter = els.diagTypeFilter.value;
    if (filter === 'errors' && entry.severity === 'info') return;
    if (!['all', 'errors'].includes(filter) && entry.category !== filter && entry.log_type !== filter) return;
    const empty = els.logFeed.querySelector('.empty-state');
    if (empty) empty.remove();
    els.logFeed.insertAdjacentHTML('beforeend', renderLogEntry(entry));
    while (els.logFeed.children.length > 500) {
        els.logFeed.firstElementChild?.remove();
    }
    els.logFeed.scrollTop = els.logFeed.scrollHeight;
}

function connectSocket() {
    if (!window.io) return;
    state.socket = window.io();
    state.socket.on('connect', () => subscribeLogs(state.diagnosticsOpen));
    state.socket.on('zone_status', () => refreshSoon());
    state.socket.on('zone_deleted', () => refreshSoon());
    state.socket.on('zone_log', appendLogEntry);
}

function subscribeLogs(enabled) {
    if (!state.socket?.connected) return;
    state.socket.emit(enabled ? 'subscribe_logs' : 'unsubscribe_logs', { all: true });
}

function policyFor(room) {
    const raw = room?.binding?.tts_policy || {};
    return {
        mode: raw.mode === 'speaker' ? 'speaker' : 'room',
        room: {
            tts_level_pct: clampNumber(raw.room?.tts_level_pct, 5, 200, 100),
            reduction_pct: clampNumber(raw.room?.reduction_pct, 0, 95, 72),
        },
        speakers: { ...(raw.speakers || {}) },
    };
}

function findRoom(roomId) {
    if (!roomId) return null;
    return (state.dashboard?.rooms || []).find((room) => room.room_id === roomId) || null;
}

function showError(error) {
    const message = error instanceof ApiError ? error.message : error?.message || String(error);
    els.globalError.hidden = false;
    els.globalError.textContent = message;
    showToast(message);
}

function showToast(message) {
    els.toast.textContent = message;
    els.toast.hidden = false;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => {
        els.toast.hidden = true;
    }, 2600);
}

function toCamel(value) {
    return value.replace(/-([a-z])/g, (_, char) => char.toUpperCase());
}

function capitalize(value) {
    return value.charAt(0).toUpperCase() + value.slice(1);
}

function cssEscape(value) {
    if (window.CSS?.escape) return window.CSS.escape(value);
    return String(value).replace(/"/g, '\\"');
}
