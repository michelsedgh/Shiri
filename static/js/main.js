import { Api, ApiError } from './api.js';
import {
    bindingText,
    clampNumber,
    debounce,
    escapeHtml,
    selectedSpeakerText,
    statusClass,
    zoneLabel,
} from './utils.js';

const state = {
    dashboard: null,
    activeZoneId: null,
    activeDrawerTab: 'setup',
    diagnosticsOpen: false,
    logsPaused: false,
    socket: null,
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
        'settings-panel',
        'settings-form',
        'settings-zones',
        'refresh-settings',
        'create-zone-form',
        'new-zone-name',
        'new-zone-interface',
        'new-zone-autostart',
        'close-settings',
        'toast',
    ]) {
        els[toCamel(id)] = document.getElementById(id);
    }
}

function bindEvents() {
    els.refreshDashboard.addEventListener('click', () => loadDashboard());
    els.openSettings.addEventListener('click', openSettings);
    els.closeSettings.addEventListener('click', closeSettings);
    els.openDiagnostics.addEventListener('click', openDiagnostics);
    els.closeDiagnostics.addEventListener('click', closeDiagnostics);
    els.closeRoomDrawer.addEventListener('click', closeZoneDrawer);
    els.refreshLogs.addEventListener('click', loadLogs);
    els.toggleLiveLogs.addEventListener('click', toggleLiveLogs);
    els.diagRoomFilter.addEventListener('change', loadLogs);
    els.diagTypeFilter.addEventListener('change', loadLogs);
    els.refreshSettings.addEventListener('click', renderSettings);
    els.settingsForm.addEventListener('submit', onSaveSettings);
    els.createZoneForm.addEventListener('submit', onCreateZone);

    els.roomList.addEventListener('click', onZoneListClick);
    els.roomList.addEventListener('input', onRangeInput);
    els.roomList.addEventListener('change', onZoneListChange);

    els.roomDrawer.addEventListener('click', onDrawerClick);
    els.roomDrawer.addEventListener('input', onRangeInput);
    els.roomDrawer.addEventListener('change', onDrawerChange);

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        closeZoneDrawer();
        closeDiagnostics();
        closeSettings();
    });
}

async function loadDashboard({ quiet = false } = {}) {
    try {
        const dashboard = await Api.dashboard();
        state.dashboard = dashboard;
        renderDashboard();
        if (state.activeZoneId) renderZoneDrawer();
        if (state.diagnosticsOpen) renderDiagnosticsFilters();
        if (!quiet) showToast('Dashboard refreshed');
    } catch (error) {
        showError(error);
    }
}

function renderDashboard() {
    const dashboard = state.dashboard;
    if (!dashboard) return;
    const zones = dashboard.zones || [];
    const running = dashboard.system?.running_zones ?? 0;
    const total = dashboard.system?.zone_count ?? 0;

    els.roomCount.textContent = `${zones.length} zone${zones.length === 1 ? '' : 's'}`;
    els.consoleSubtitle.textContent = `Updated ${new Date((dashboard.generated_at || Date.now() / 1000) * 1000).toLocaleTimeString()}`;
    renderStatusPill(els.shiriStatus, total ? `${running}/${total} zones running` : 'No zones', total ? (running ? 'good' : 'warn') : 'bad');
    renderStatusPill(els.lionosStatus, 'LionOS owns rooms', 'good');
    renderDefaultBinding();
    els.globalError.hidden = true;
    els.globalError.textContent = '';

    els.roomList.innerHTML = zones.length
        ? zones.map(renderZoneRow).join('')
        : '<div class="empty-state">No zones found</div>';
}

function renderStatusPill(el, text, tone) {
    el.className = `status-pill ${tone}`;
    el.innerHTML = `<span class="dot"></span><span>${escapeHtml(text)}</span>`;
}

function renderDefaultBinding() {
    const roomId = state.dashboard?.default_lionos_room_id;
    const zone = (state.dashboard?.zones || []).find((item) => item.lionos_room_id === roomId);
    els.defaultRoom.className = `status-pill ${zone ? 'good' : 'muted'}`;
    els.defaultRoom.innerHTML = `<span>${zone ? `Default: ${escapeHtml(zoneLabel(zone))}` : 'No default binding'}</span>`;
}

function renderZoneRow(zone) {
    const policy = policyForZone(zone);
    const zoneSettings = policy.zone || {};
    const volume = zone.volume ?? zone.player?.volume ?? 50;
    const isRunning = zone.status === 'running';
    const volumeValue = clampNumber(volume, 0, 100, 50);
    const reduction = clampNumber(zoneSettings.reduction_pct, 0, 95, 72);

    return `
        <article class="room-row ${zone.lionos_room_id ? '' : 'unbound'}" data-zone-id="${escapeHtml(zone.zone_id)}">
            <div class="room-cell">
                <div class="room-title">
                    <h3 title="${escapeHtml(zoneLabel(zone))}">${escapeHtml(zoneLabel(zone))}</h3>
                    <span class="source-badge">Zone</span>
                </div>
                <div class="room-meta">
                    <span>${escapeHtml(zone.zone_id)}</span>
                    <span>${escapeHtml(bindingText(zone))}</span>
                </div>
            </div>
            <div class="room-cell">
                <div class="route-line">
                    <span class="state-badge ${statusClass(zone.status)}">${escapeHtml(zone.status)}</span>
                    <strong title="${escapeHtml(zone.interface || 'No interface')}">${escapeHtml(zone.interface || 'No interface')}</strong>
                </div>
                <div class="speaker-summary" title="${escapeHtml(selectedSpeakerText(zone.speakers || []))}">
                    ${escapeHtml(selectedSpeakerText(zone.speakers || []))}
                </div>
            </div>
            <div class="room-cell">
                <div class="control-bank">
                    <div class="control">
                        <label for="vol-${escapeHtml(zone.zone_id)}">Playback</label>
                        <div class="range-line">
                            <input id="vol-${escapeHtml(zone.zone_id)}" type="range" min="0" max="100" value="${volumeValue}" data-action="zone-volume" data-zone-id="${escapeHtml(zone.zone_id)}" ${isRunning ? '' : 'disabled'}>
                            <output>${volumeValue}%</output>
                        </div>
                    </div>
                    <div class="control">
                        <label for="duck-${escapeHtml(zone.zone_id)}">Reduce playing audio</label>
                        <div class="range-line">
                            <input id="duck-${escapeHtml(zone.zone_id)}" type="range" min="0" max="95" value="${reduction}" data-action="zone-reduction" data-zone-id="${escapeHtml(zone.zone_id)}" ${isRunning ? '' : 'disabled'}>
                            <output>${reduction}%</output>
                        </div>
                    </div>
                </div>
            </div>
            <div class="room-cell">
                <div class="row-actions">
                    <button class="small-btn" data-action="zone-start" data-zone-id="${escapeHtml(zone.zone_id)}" ${zone.can_start ? '' : 'disabled'}>Start</button>
                    <button class="small-btn" data-action="zone-stop" data-zone-id="${escapeHtml(zone.zone_id)}" ${zone.can_stop ? '' : 'disabled'}>Stop</button>
                    <button class="small-btn" data-action="zone-details" data-zone-id="${escapeHtml(zone.zone_id)}">Details</button>
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

async function onZoneListClick(event) {
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
        } else if (action === 'zone-details') {
            openZoneDrawer(button.dataset.zoneId);
        }
    } catch (error) {
        showError(error);
    }
}

async function onZoneListChange(event) {
    const input = event.target;
    if (input.type !== 'range') return;
    const zoneId = input.dataset.zoneId;
    try {
        if (input.dataset.action === 'zone-volume') {
            await Api.setZoneVolume(zoneId, clampNumber(input.value, 0, 100, 50));
            showToast('Playback volume saved');
        } else if (input.dataset.action === 'zone-reduction') {
            await saveZonePolicyFromRow(zoneId);
        }
        refreshSoon();
    } catch (error) {
        showError(error);
    }
}

async function saveZonePolicyFromRow(zoneId) {
    const row = els.roomList.querySelector(`[data-zone-id="${cssEscape(zoneId)}"]`);
    if (!row) return;
    const policy = {
        mode: 'zone',
        zone: {
            reduction_pct: clampNumber(row.querySelector('[data-action="zone-reduction"]')?.value, 0, 95, 72),
        },
        speakers: {},
    };
    await Api.setZoneTtsPolicy(zoneId, policy);
    showToast('Ducking saved');
}

function openZoneDrawer(zoneId) {
    state.activeZoneId = zoneId;
    state.activeDrawerTab = 'setup';
    els.roomDrawer.classList.add('open');
    els.roomDrawer.setAttribute('aria-hidden', 'false');
    renderZoneDrawer();
}

function closeZoneDrawer() {
    state.activeZoneId = null;
    els.roomDrawer.classList.remove('open');
    els.roomDrawer.setAttribute('aria-hidden', 'true');
}

function renderZoneDrawer() {
    const zone = findZone(state.activeZoneId);
    if (!zone) return;
    els.drawerRoomName.textContent = zoneLabel(zone);
    els.drawerRoomSource.textContent = 'Shiri zone';

    els.roomDrawer.querySelectorAll('.tab').forEach((tab) => {
        tab.classList.toggle('active', tab.dataset.drawerTab === state.activeDrawerTab);
    });
    for (const key of ['setup', 'speakers', 'advanced']) {
        els[`drawer${capitalize(key)}`].hidden = key !== state.activeDrawerTab;
    }
    renderDrawerSetup(zone);
    renderDrawerSpeakers(zone);
    renderDrawerAdvanced(zone);
}

function renderDrawerSetup(zone) {
    els.drawerSetup.innerHTML = `
        <div class="drawer-stack">
            <div class="drawer-block">
                <label class="field">
                    <span>LionOS room id</span>
                    <input id="binding-lionos-room-id" type="text" value="${escapeHtml(zone.lionos_room_id || '')}" autocomplete="off">
                </label>
                <label class="field">
                    <span>LionOS room name</span>
                    <input id="binding-lionos-room-name" type="text" value="${escapeHtml(zone.lionos_room_name || '')}" autocomplete="off">
                </label>
                <label class="check-field">
                    <input id="binding-default-zone" type="checkbox" ${zone.default_lionos_room ? 'checked' : ''}>
                    <span>Default LionOS audio binding</span>
                </label>
                <div class="row-actions">
                    <button class="primary-btn" data-action="save-binding" data-zone-id="${escapeHtml(zone.zone_id)}">Save Binding</button>
                    <button class="small-btn" data-action="clear-binding" data-zone-id="${escapeHtml(zone.zone_id)}" ${zone.lionos_room_id ? '' : 'disabled'}>Clear</button>
                </div>
            </div>
            <div class="drawer-block">
                <div class="advanced-row">
                    <div>
                        <strong>Zone id</strong>
                        <span>${escapeHtml(zone.zone_id)}</span>
                    </div>
                    <span class="mode-badge">${escapeHtml(zone.status)}</span>
                </div>
                <div class="advanced-row">
                    <div>
                        <strong>Binding owner</strong>
                        <span>LionOS writes this metadata; Shiri plays by zone id.</span>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function renderDrawerSpeakers(zone) {
    const speakers = zone.speakers || [];
    if (!speakers.length) {
        els.drawerSpeakers.innerHTML = '<div class="empty-state">No speakers discovered or saved</div>';
        return;
    }
    const enabledSpeakers = speakers.filter((speaker) => speaker.selected);
    els.drawerSpeakers.innerHTML = `
        <div class="drawer-stack">
            <div class="drawer-block">
                <div class="section-title">
                    <h3>Routing</h3>
                    <span class="mode-badge">${enabledSpeakers.length}/${speakers.length} enabled</span>
                </div>
                <div class="speaker-route-list">
                    ${speakers.map((speaker) => renderSpeakerRouteRow(zone, speaker)).join('')}
                </div>
                <button class="primary-btn" data-action="save-speakers" data-zone-id="${escapeHtml(zone.zone_id)}">Save Routing</button>
            </div>
        </div>
    `;
}

function renderSpeakerRouteRow(zone, speaker) {
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
                    <input type="range" min="0" max="100" value="${volume}" data-action="speaker-volume" data-zone-id="${escapeHtml(zone.zone_id)}" data-speaker-id="${escapeHtml(speakerId)}" ${zone.status === 'running' ? '' : 'disabled'}>
                    <output>${volume}%</output>
                </div>
            ` : ''}
        </div>
    `;
}

function renderDrawerAdvanced(zone) {
    const interfaces = state.dashboard?.system?.interfaces || [];
    const ownTonePort = zone.owntone_port ?? 3689;
    els.drawerAdvanced.innerHTML = `
        <div class="drawer-stack">
            <label class="field">
                <span>AirPlay name</span>
                <input id="advanced-zone-name" type="text" value="${escapeHtml(zone.zone_name)}">
            </label>
            <label class="field">
                <span>Network interface</span>
                <select id="advanced-zone-interface">
                    ${interfaces.map((iface) => `<option value="${escapeHtml(iface)}" ${iface === zone.interface ? 'selected' : ''}>${escapeHtml(iface)}</option>`).join('')}
                </select>
            </label>
            <label class="field">
                <span>Latency offset</span>
                <input id="advanced-zone-latency" type="number" min="-0.25" max="0.25" step="0.01" value="${escapeHtml(zone.latency_offset ?? 0)}">
            </label>
            <label class="check-field">
                <input id="advanced-zone-autostart" type="checkbox" ${zone.auto_start ? 'checked' : ''}>
                <span>Auto-start</span>
            </label>
            <button class="primary-btn" data-action="save-zone-advanced" data-zone-id="${escapeHtml(zone.zone_id)}">Save Zone</button>
            <div class="advanced-row">
                <div>
                    <strong>OwnTone</strong>
                    <span>${zone.owntone_ip ? `${escapeHtml(zone.owntone_ip)}:${escapeHtml(ownTonePort)}` : 'not running'}</span>
                </div>
                ${zone.owntone_ip ? `<a class="small-btn" href="http://${escapeHtml(zone.owntone_ip)}:${escapeHtml(ownTonePort)}" target="_blank" rel="noreferrer">Open</a>` : '<span></span>'}
            </div>
            <div class="advanced-row">
                <div>
                    <strong>Runtime</strong>
                    <span>host ports / subdev ${escapeHtml(zone.allocated_subdevice ?? '-')}</span>
                </div>
                <span class="state-badge ${statusClass(zone.status)}">${escapeHtml(zone.status)}</span>
            </div>
            <button class="danger-btn" data-action="delete-zone" data-zone-id="${escapeHtml(zone.zone_id)}">Delete Zone</button>
        </div>
    `;
}

async function onDrawerClick(event) {
    const tab = event.target.closest('[data-drawer-tab]');
    if (tab) {
        state.activeDrawerTab = tab.dataset.drawerTab;
        renderZoneDrawer();
        return;
    }

    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    try {
        if (action === 'save-binding') await saveBinding(button.dataset.zoneId);
        if (action === 'clear-binding') await clearBinding(button.dataset.zoneId);
        if (action === 'save-speakers') await saveSpeakers(button.dataset.zoneId);
        if (action === 'save-zone-advanced') await saveZoneAdvanced(button.dataset.zoneId);
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

async function saveBinding(zoneId) {
    const lionosRoomId = document.getElementById('binding-lionos-room-id')?.value?.trim();
    if (!lionosRoomId) throw new Error('LionOS room id is required');
    await Api.bindZone(zoneId, {
        lionos_room_id: lionosRoomId,
        lionos_room_name: document.getElementById('binding-lionos-room-name')?.value?.trim() || lionosRoomId,
        default: document.getElementById('binding-default-zone')?.checked,
    });
    showToast('Binding saved');
    await loadDashboard({ quiet: true });
}

async function clearBinding(zoneId) {
    await Api.clearZoneBinding(zoneId);
    showToast('Binding cleared');
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

async function saveZoneAdvanced(zoneId) {
    await Api.updateZone(zoneId, {
        name: document.getElementById('advanced-zone-name')?.value?.trim(),
        interface: document.getElementById('advanced-zone-interface')?.value,
        latency_offset: Number(document.getElementById('advanced-zone-latency')?.value),
        auto_start: document.getElementById('advanced-zone-autostart')?.checked,
    });
    showToast('Zone saved');
    await loadDashboard({ quiet: true });
}

async function deleteZone(zoneId) {
    if (!window.confirm('Delete this Shiri zone?')) return;
    await Api.deleteZone(zoneId);
    showToast('Zone deleted');
    closeZoneDrawer();
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
    await renderInterfaceOptions();
    els.settingsZones.innerHTML = (dashboard.zones || []).map((zone) => `
        <div class="settings-row">
            <div>
                <strong>${escapeHtml(zoneLabel(zone))}</strong>
                <span>${escapeHtml(bindingText(zone))} / ${escapeHtml(zone.status)} / ${escapeHtml(zone.interface || 'no interface')}</span>
            </div>
            <button class="small-btn" type="button" data-settings-zone="${escapeHtml(zone.zone_id)}">Open</button>
        </div>
    `).join('') || '<div class="empty-state">No zones</div>';
    els.settingsZones.querySelectorAll('[data-settings-zone]').forEach((button) => {
        button.addEventListener('click', () => {
            closeSettings();
            openZoneDrawer(button.dataset.settingsZone);
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
    showToast('Settings saved');
}

async function onCreateZone(event) {
    event.preventDefault();
    try {
        await Api.createZone({
            name: els.newZoneName.value.trim(),
            interface: els.newZoneInterface.value,
            auto_start: els.newZoneAutostart.checked,
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
    const zones = state.dashboard?.zones || [];
    const selected = els.diagRoomFilter.value;
    els.diagRoomFilter.innerHTML = `<option value="">All zones</option>${zones.map((zone) => `<option value="${escapeHtml(zone.zone_id)}">${escapeHtml(zoneLabel(zone))}</option>`).join('')}`;
    els.diagRoomFilter.value = selected || '';
}

async function loadLogs() {
    if (!state.diagnosticsOpen) return;
    try {
        const data = await Api.logs({
            zoneId: els.diagRoomFilter.value,
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
            <span>${escapeHtml(entry.zone_name || entry.zone_id || '')}</span>
            <span>${escapeHtml(entry.category || entry.log_type || '')}</span>
            <span class="log-line">${escapeHtml(entry.line || '')}</span>
        </div>
    `;
}

function appendLogEntry(entry) {
    if (!state.diagnosticsOpen || state.logsPaused) return;
    if (els.diagRoomFilter.value && entry.zone_id !== els.diagRoomFilter.value) return;
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

function policyForZone(zone) {
    const raw = zone?.tts_policy || {};
    return {
        mode: 'zone',
        zone: {
            reduction_pct: clampNumber(raw.zone?.reduction_pct ?? raw.room?.reduction_pct, 0, 95, 72),
        },
        speakers: {},
    };
}

function findZone(zoneId) {
    if (!zoneId) return null;
    return (state.dashboard?.zones || []).find((zone) => zone.zone_id === zoneId) || null;
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
