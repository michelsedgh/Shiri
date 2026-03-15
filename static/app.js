/**
 * app.js — Shiri Dashboard Frontend
 *
 * Communicates with shiri_daemon.py via REST API + SocketIO.
 * Manages zone cards, speaker lists, volume sliders, and live logs.
 */

const App = (() => {
    // State
    let zones = {};
    let socket = null;
    let activeDetailZone = null;
    let activeLogType = 'shairport';
    let logAutoScroll = true;
    let volumeDebounceTimers = {};
    let editingZoneId = null;

    // -------------------------------------------------------------------------
    // Initialization
    // -------------------------------------------------------------------------

    function init() {
        connectSocket();
        fetchZones();
        fetchSystemStatus();

        // Periodically refresh system status
        setInterval(fetchSystemStatus, 10000);
        // Periodically refresh zone statuses
        setInterval(fetchZones, 15000);

        // Master volume slider
        const masterVol = document.getElementById('master-volume');
        if (masterVol) {
            masterVol.addEventListener('input', (e) => {
                document.getElementById('master-volume-val').textContent = e.target.value;
            });
            masterVol.addEventListener('change', (e) => {
                if (activeDetailZone) {
                    setMasterVolume(activeDetailZone, parseInt(e.target.value));
                }
            });
        }

        // Settings button
        document.getElementById('btn-settings').addEventListener('click', showSettings);
        document.getElementById('btn-refresh-speakers').addEventListener('click', refreshAllSpeakers);
    }

    // -------------------------------------------------------------------------
    // WebSocket
    // -------------------------------------------------------------------------

    function connectSocket() {
        socket = io();

        socket.on('connect', () => {
            updateSystemDot(true);
        });

        socket.on('disconnect', () => {
            updateSystemDot(false);
        });

        socket.on('zone_status', (data) => {
            zones[data.zone_id] = data;
            renderZoneCard(data);

            // Update detail panel if open for this zone
            if (activeDetailZone === data.zone_id) {
                updateDetailPanel(data);
            }
        });

        socket.on('zone_deleted', (data) => {
            delete zones[data.zone_id];
            const card = document.getElementById(`zone-${data.zone_id}`);
            if (card) card.remove();

            if (activeDetailZone === data.zone_id) {
                hideDetailPanel();
            }
        });

        socket.on('zone_log', (data) => {
            if (activeDetailZone === data.zone_id && activeLogType === data.log_type) {
                appendLogLine(data.line);
            }
        });
    }

    function updateSystemDot(connected) {
        const dot = document.querySelector('#system-status .status-dot');
        const text = document.querySelector('#system-status .status-text');
        if (connected) {
            dot.className = 'status-dot online';
            text.textContent = 'Connected';
        } else {
            dot.className = 'status-dot offline';
            text.textContent = 'Disconnected';
        }
    }

    // -------------------------------------------------------------------------
    // API Calls
    // -------------------------------------------------------------------------

    async function api(path, method = 'GET', body = null) {
        const opts = { method, headers: {} };
        if (body) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(`/api${path}`, opts);
        return resp.json();
    }

    async function fetchZones() {
        try {
            const data = await api('/zones');
            if (data.zones) {
                data.zones.forEach(z => {
                    zones[z.zone_id] = z;
                    renderZoneCard(z);
                });
            }
        } catch (e) {
            console.error('Failed to fetch zones:', e);
        }
    }

    async function fetchSystemStatus() {
        try {
            const data = await api('/system/status');
            updateSystemInfo(data);
        } catch (e) {
            console.error('Failed to fetch system status:', e);
        }
    }

    function updateSystemInfo(data) {
        const dot = document.querySelector('#system-status .status-dot');
        const text = document.querySelector('#system-status .status-text');
        if (data.nqptp_running && data.alsa_ready) {
            dot.className = 'status-dot online';
            text.textContent = `${data.running_zones}/${data.zone_count} zones active`;
        } else if (!data.nqptp_running) {
            dot.className = 'status-dot offline';
            text.textContent = 'nqptp not running';
        } else {
            dot.className = 'status-dot offline';
            text.textContent = 'ALSA not ready';
        }
    }

    // -------------------------------------------------------------------------
    // Zone Card Rendering
    // -------------------------------------------------------------------------

    function renderZoneCard(zone) {
        let card = document.getElementById(`zone-${zone.zone_id}`);
        const isNew = !card;

        if (isNew) {
            card = document.createElement('div');
            card.id = `zone-${zone.zone_id}`;
            card.className = 'zone-card';

            // Insert before the create card
            const createCard = document.getElementById('create-zone-card');
            createCard.parentNode.insertBefore(card, createCard);

            // Build waveform bars
            let waveformBars = '';
            for (let i = 0; i < 16; i++) {
                waveformBars += '<div class="waveform-bar"></div>';
            }

            card.innerHTML = `
                <div class="zone-card-header">
                    <div>
                        <div class="zone-name" id="name-${zone.zone_id}"></div>
                        <div class="zone-interface" id="iface-${zone.zone_id}"></div>
                    </div>
                    <span class="status-badge" id="badge-${zone.zone_id}"></span>
                </div>
                <div class="waveform">${waveformBars}</div>
                <div class="zone-error" id="error-${zone.zone_id}" style="display:none;"></div>
                <div class="zone-speakers-summary">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 6v12M6 12h12"/></svg>
                    <span id="speakers-summary-${zone.zone_id}"></span>
                </div>
                <div class="zone-volume">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>
                    <input type="range" min="0" max="100" value="50" class="volume-slider"
                        id="vol-${zone.zone_id}"
                        oninput="App.onCardVolumeInput('${zone.zone_id}', this.value)"
                        onchange="App.onCardVolumeChange('${zone.zone_id}', this.value)">
                    <span class="volume-val" id="vol-val-${zone.zone_id}">—</span>
                </div>
                <div class="zone-card-actions" id="actions-${zone.zone_id}"></div>
            `;
        }

        // --- UPDATE EXISTING ELEMENTS (Prevents jumping sliders!) ---
        card.className = `zone-card ${zone.status}`;
        
        document.getElementById(`name-${zone.zone_id}`).textContent = zone.config?.name || zone.zone_id;
        document.getElementById(`iface-${zone.zone_id}`).textContent = '⌘ ' + (zone.config?.interface || '—');

        const badge = document.getElementById(`badge-${zone.zone_id}`);
        badge.className = `status-badge ${zone.status}`;
        badge.textContent = zone.status;

        const errorEl = document.getElementById(`error-${zone.zone_id}`);
        if (zone.error_message) {
            errorEl.textContent = zone.error_message;
            errorEl.style.display = 'block';
        } else {
            errorEl.style.display = 'none';
        }

        const summary = document.getElementById(`speakers-summary-${zone.zone_id}`);
        if (zone.status === 'running') {
            if (summary.textContent === 'Start to see speakers' || !summary.textContent) {
                summary.textContent = 'Click details to view speakers';
            }
        } else {
            summary.textContent = 'Start to see speakers';
        }

        const volSlider = document.getElementById(`vol-${zone.zone_id}`);
        if (zone.status !== 'running') {
            volSlider.disabled = true;
        } else {
            volSlider.disabled = false;
        }

        const canStart = zone.status === 'stopped' || zone.status === 'error';
        const canStop = zone.status === 'running' || zone.status === 'starting';

        const actionsEl = document.getElementById(`actions-${zone.zone_id}`);
        
        let startStopButton = '';
        if (canStart) {
            startStopButton = `<button class="btn btn-success btn-sm" onclick="App.startZone('${zone.zone_id}')">▶ Start</button>`;
        } else {
            const disabledAttr = !canStop ? 'disabled' : '';
            startStopButton = `<button class="btn btn-secondary btn-sm" onclick="App.stopZone('${zone.zone_id}')" ${disabledAttr}>■ Stop</button>`;
        }
        
        const editDisabled = !canStart ? 'disabled' : '';

        actionsEl.innerHTML = `
            ${startStopButton}
            <button class="btn btn-secondary btn-sm" onclick="App.showDetail('${zone.zone_id}')">Details</button>
            <button class="btn btn-secondary btn-sm btn-icon" onclick="App.editZone('${zone.zone_id}')" title="Edit zone settings" ${editDisabled}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
            </button>
            <button class="btn btn-danger btn-sm btn-icon" onclick="App.deleteZone('${zone.zone_id}')" title="Delete zone">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            </button>
        `;

        // Only fetch volume if the zone is running AND the user isn't actively sliding it!
        if (zone.status === 'running' && document.activeElement !== volSlider) {
            fetchZoneVolume(zone.zone_id);
        }
    }

    async function fetchZoneVolume(zoneId) {
        try {
            const data = await api(`/zones/${zoneId}/volume`);
            const slider = document.getElementById(`vol-${zoneId}`);
            const val = document.getElementById(`vol-val-${zoneId}`);
            if (slider && data.volume !== undefined) {
                slider.value = data.volume;
                if (val) val.textContent = data.volume;
            }
        } catch (e) { /* ignore */ }
    }

    // -------------------------------------------------------------------------
    // Zone Actions
    // -------------------------------------------------------------------------

    async function startZone(zoneId) {
        await api(`/zones/${zoneId}/start`, 'POST');
    }

    async function stopZone(zoneId) {
        await api(`/zones/${zoneId}/stop`, 'POST');
    }

    async function deleteZone(zoneId) {
        if (!confirm('Delete this zone? This will stop it if running.')) return;
        await api(`/zones/${zoneId}`, 'DELETE');
    }

    // -------------------------------------------------------------------------
    // Create & Edit Zone Dialog
    // -------------------------------------------------------------------------

    async function showZoneDialog(zoneId = null) {
        editingZoneId = zoneId;
        const dialogTitle = document.getElementById('zone-dialog-title');
        const saveBtn = document.getElementById('btn-save-zone');
        const nameInput = document.getElementById('zone-name');
        const autoStartInput = document.getElementById('zone-autostart');
        const latencyGroup = document.getElementById('latency-group');
        const latencySlider = document.getElementById('zone-latency');
        const latencyVal = document.getElementById('zone-latency-val');
        
        document.getElementById('zone-dialog').style.display = 'flex';

        // Setup latency slider input handler
        latencySlider.oninput = () => {
            latencyVal.textContent = parseFloat(latencySlider.value).toFixed(1);
        };

        if (zoneId && zones[zoneId]) {
            const zone = zones[zoneId];
            dialogTitle.textContent = 'Edit Zone';
            saveBtn.textContent = 'Save Changes';
            nameInput.value = zone.config.name || '';
            autoStartInput.checked = !!zone.config.auto_start;
            
            // Show latency slider when editing
            latencyGroup.style.display = 'block';
            const latency = zone.latency_offset ?? zone.config.latency_offset ?? -2.3;
            latencySlider.value = latency;
            latencyVal.textContent = parseFloat(latency).toFixed(1);
        } else {
            dialogTitle.textContent = 'Create New Zone';
            saveBtn.textContent = 'Create Zone';
            nameInput.value = '';
            autoStartInput.checked = false;
            
            // Hide latency slider for new zones (use default)
            latencyGroup.style.display = 'none';
            latencySlider.value = -2.3;
            latencyVal.textContent = '-2.3';
        }

        // Load interfaces
        try {
            const data = await api('/system/interfaces');
            const sel = document.getElementById('zone-interface');
            sel.innerHTML = '';
            if (data.interfaces && data.interfaces.length > 0) {
                data.interfaces.forEach(iface => {
                    sel.innerHTML += `<option value="${escHtml(iface)}">${escHtml(iface)}</option>`;
                });
            } else {
                sel.innerHTML = '<option value="">No interfaces found</option>';
            }
            
            // Set current interface if editing
            if (zoneId && zones[zoneId] && zones[zoneId].config.interface) {
                sel.value = zones[zoneId].config.interface;
            }
        } catch (e) {
            console.error('Failed to load interfaces:', e);
        }
    }
    
    // For backwards compatibility with the create card click
    function showCreateDialog() {
        showZoneDialog(null);
    }
    
    function editZone(zoneId) {
        showZoneDialog(zoneId);
    }

    function hideZoneDialog() {
        document.getElementById('zone-dialog').style.display = 'none';
        editingZoneId = null;
    }

    async function saveZone() {
        const name = document.getElementById('zone-name').value.trim();
        const iface = document.getElementById('zone-interface').value;
        const autoStart = document.getElementById('zone-autostart').checked;
        const latencyOffset = parseFloat(document.getElementById('zone-latency').value);

        if (!name) {
            alert('Please enter a zone name');
            return;
        }
        if (!iface) {
            alert('Please select a network interface');
            return;
        }

        const payload = { name, interface: iface, auto_start: autoStart };
        
        // Include latency_offset when editing existing zones
        if (editingZoneId) {
            payload.latency_offset = latencyOffset;
            await api(`/zones/${editingZoneId}`, 'PUT', payload);
        } else {
            await api('/zones', 'POST', payload);
        }
        
        hideZoneDialog();
        fetchZones();
    }

    // -------------------------------------------------------------------------
    // Detail Panel
    // -------------------------------------------------------------------------

    async function showDetail(zoneId) {
        activeDetailZone = zoneId;
        const zone = zones[zoneId];
        if (!zone) return;

        const panel = document.getElementById('detail-panel');
        panel.style.display = 'block';

        updateDetailPanel(zone);
        switchDetailTab('speakers');

        // Subscribe to logs
        if (socket) {
            socket.emit('subscribe_logs', { zone_id: zoneId });
        }

        // Load speakers if running
        if (zone.status === 'running') {
            loadSpeakers(zoneId);
            loadMasterVolume(zoneId);
        }
    }

    function updateDetailPanel(zone) {
        document.getElementById('detail-zone-name').textContent = zone.config?.name || zone.zone_id;
        const badge = document.getElementById('detail-zone-status');
        badge.textContent = zone.status;
        badge.className = `status-badge ${zone.status}`;

        // Config tab
        document.getElementById('config-zone-id').textContent = zone.zone_id;
        document.getElementById('config-latency').textContent = (zone.latency_offset ?? zone.config?.latency_offset ?? -2.3) + 's';
        document.getElementById('config-shairport-ip').textContent = zone.shairport_ip || '—';
        document.getElementById('config-owntone-ip').textContent = zone.owntone_ip || '—';
        document.getElementById('config-netns').textContent = zone.netns_name || '—';
        document.getElementById('config-subdev').textContent = zone.allocated_subdevice ?? '—';
        document.getElementById('config-interface').textContent = zone.config?.interface || '—';
        
        // OwnTone link
        const owntoneLink = document.getElementById('owntone-link');
        if (zone.owntone_ip && zone.status === 'running') {
            owntoneLink.href = `http://${zone.owntone_ip}:3689`;
            owntoneLink.style.display = 'inline-flex';
        } else {
            owntoneLink.style.display = 'none';
        }
    }

    function hideDetailPanel() {
        if (activeDetailZone && socket) {
            socket.emit('unsubscribe_logs', { zone_id: activeDetailZone });
        }
        activeDetailZone = null;
        document.getElementById('detail-panel').style.display = 'none';
    }

    function switchDetailTab(tabName) {
        // Update tab buttons
        document.querySelectorAll('.detail-tabs .tab').forEach(t => {
            t.classList.toggle('active', t.dataset.tab === tabName);
        });

        // Show/hide tab content
        document.getElementById('tab-speakers').style.display = tabName === 'speakers' ? 'block' : 'none';
        document.getElementById('tab-logs').style.display = tabName === 'logs' ? 'block' : 'none';
        document.getElementById('tab-config').style.display = tabName === 'config' ? 'block' : 'none';

        // Load logs when switching to logs tab
        if (tabName === 'logs' && activeDetailZone) {
            loadLogs(activeDetailZone, activeLogType);
        }
    }

    // -------------------------------------------------------------------------
    // Speakers
    // -------------------------------------------------------------------------

    async function loadSpeakers(zoneId) {
        const list = document.getElementById('speaker-list');
        try {
            const data = await api(`/zones/${zoneId}/speakers`);
            if (!data.speakers || data.speakers.length === 0) {
                list.innerHTML = '<div class="empty-state">No speakers discovered yet. They may take a moment to appear.</div>';
                return;
            }

            list.innerHTML = data.speakers.map(sp => `
                <div class="speaker-item ${sp.selected ? 'selected' : ''}" id="speaker-${sp.id}">
                    <div class="speaker-header">
                        <div>
                            <div class="speaker-name">${escHtml(sp.name)}</div>
                            <span class="speaker-type">${escHtml(sp.type || 'unknown')}</span>
                        </div>
                        <button class="toggle ${sp.selected ? 'on' : ''}"
                            onclick="App.toggleSpeaker('${zoneId}', '${sp.id}', ${!sp.selected})">
                        </button>
                    </div>
                    <div class="speaker-volume">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/></svg>
                        <input type="range" min="0" max="100" value="${sp.volume || 0}" class="volume-slider"
                            oninput="this.nextElementSibling.textContent=this.value"
                            onchange="App.setSpeakerVolume('${zoneId}', '${sp.id}', this.value)">
                        <span class="volume-val">${sp.volume || 0}</span>
                    </div>
                </div>
            `).join('');

            // Also update card summary
            const selectedSpeakers = data.speakers.filter(s => s.selected);
            const summaryEl = document.getElementById(`speakers-summary-${zoneId}`);
            if (summaryEl) {
                if (selectedSpeakers.length > 0) {
                    summaryEl.textContent = selectedSpeakers.map(s => s.name).join(', ');
                } else {
                    summaryEl.textContent = 'No speakers selected';
                }
            }
        } catch (e) {
            list.innerHTML = '<div class="empty-state">Error loading speakers</div>';
        }
    }

    async function toggleSpeaker(zoneId, speakerId, enabled) {
        await api(`/zones/${zoneId}/speakers/${speakerId}/toggle`, 'POST', { enabled });
        // Reload speakers to reflect change
        setTimeout(() => loadSpeakers(zoneId), 500);
    }

    async function setSpeakerVolume(zoneId, speakerId, volume) {
        await api(`/zones/${zoneId}/speakers/${speakerId}/volume`, 'PUT', { volume: parseInt(volume) });
    }

    async function refreshAllSpeakers() {
        for (const zoneId in zones) {
            if (zones[zoneId].status === 'running') {
                loadSpeakers(zoneId);
            }
        }
    }

    // -------------------------------------------------------------------------
    // Volume
    // -------------------------------------------------------------------------

    async function loadMasterVolume(zoneId) {
        try {
            const data = await api(`/zones/${zoneId}/volume`);
            const slider = document.getElementById('master-volume');
            const val = document.getElementById('master-volume-val');
            if (slider && data.volume !== undefined) {
                slider.value = data.volume;
                val.textContent = data.volume;
            }
        } catch (e) { /* ignore */ }
    }

    async function setMasterVolume(zoneId, volume) {
        await api(`/zones/${zoneId}/volume`, 'PUT', { volume });
    }

    function onCardVolumeInput(zoneId, value) {
        const val = document.getElementById(`vol-val-${zoneId}`);
        if (val) val.textContent = value;
    }

    function onCardVolumeChange(zoneId, value) {
        // Debounce volume changes
        clearTimeout(volumeDebounceTimers[zoneId]);
        volumeDebounceTimers[zoneId] = setTimeout(() => {
            api(`/zones/${zoneId}/volume`, 'PUT', { volume: parseInt(value) });
        }, 200);
    }

    // -------------------------------------------------------------------------
    // Logs
    // -------------------------------------------------------------------------

    function switchLogType(logType) {
        activeLogType = logType;

        // Update tab buttons
        document.querySelectorAll('.log-tab').forEach(t => {
            t.classList.toggle('active', t.dataset.logtype === logType);
        });

        // Load logs
        if (activeDetailZone) {
            loadLogs(activeDetailZone, logType);
        }
    }

    async function loadLogs(zoneId, logType) {
        const viewer = document.getElementById('log-viewer');
        viewer.innerHTML = '<div class="log-empty">Loading...</div>';

        try {
            const data = await api(`/zones/${zoneId}/logs/${logType}?lines=200`);
            if (!data.lines || data.lines.length === 0) {
                viewer.innerHTML = '<div class="log-empty">No logs yet</div>';
                return;
            }
            viewer.innerHTML = data.lines.map(line => {
                let cls = 'log-line';
                if (/error|ERROR|fail|FAIL/i.test(line)) cls += ' error';
                else if (/warn|WARNING/i.test(line)) cls += ' warning';
                return `<div class="${cls}">${escHtml(line)}</div>`;
            }).join('');

            // Scroll to bottom
            viewer.scrollTop = viewer.scrollHeight;
        } catch (e) {
            viewer.innerHTML = '<div class="log-empty">Error loading logs</div>';
        }
    }

    function appendLogLine(line) {
        const viewer = document.getElementById('log-viewer');
        if (!viewer) return;

        // Remove "no logs" placeholder
        const empty = viewer.querySelector('.log-empty');
        if (empty) empty.remove();

        let cls = 'log-line';
        if (/error|ERROR|fail|FAIL/i.test(line)) cls += ' error';
        else if (/warn|WARNING/i.test(line)) cls += ' warning';

        const el = document.createElement('div');
        el.className = cls;
        el.textContent = line;
        viewer.appendChild(el);

        // Keep last 500 lines
        const lines = viewer.querySelectorAll('.log-line');
        if (lines.length > 500) {
            lines[0].remove();
        }

        // Auto-scroll
        if (logAutoScroll) {
            viewer.scrollTop = viewer.scrollHeight;
        }
    }

    // -------------------------------------------------------------------------
    // Settings
    // -------------------------------------------------------------------------

    async function showSettings() {
        document.getElementById('settings-panel').style.display = 'flex';

        try {
            const data = await api('/system/status');
            document.getElementById('diag-nqptp').textContent = data.nqptp_running ? `Running (PID ${data.nqptp_pid})` : 'Not running';
            document.getElementById('diag-nqptp').className = `diag-status ${data.nqptp_running ? 'ok' : 'fail'}`;
            document.getElementById('diag-alsa').textContent = data.alsa_ready ? 'Ready' : 'Not ready';
            document.getElementById('diag-alsa').className = `diag-status ${data.alsa_ready ? 'ok' : 'fail'}`;
            document.getElementById('diag-zones').textContent = `${data.running_zones} / ${data.zone_count}`;

            const ifaceList = document.getElementById('interface-list');
            if (data.interfaces && data.interfaces.length > 0) {
                ifaceList.innerHTML = data.interfaces.map(i =>
                    `<div class="interface-item">${escHtml(i)}</div>`
                ).join('');
            } else {
                ifaceList.innerHTML = '<div class="empty-state">No interfaces detected</div>';
            }
        } catch (e) {
            console.error('Failed to load system status:', e);
        }
    }

    function hideSettings() {
        document.getElementById('settings-panel').style.display = 'none';
    }

    // -------------------------------------------------------------------------
    // Utilities
    // -------------------------------------------------------------------------

    function escHtml(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    // -------------------------------------------------------------------------
    // Public API (exposed on window.App)
    // -------------------------------------------------------------------------

    return {
        init,
        showZoneDialog,
        showCreateDialog,
        editZone,
        hideZoneDialog,
        saveZone,
        startZone,
        stopZone,
        deleteZone,
        showDetail,
        hideDetailPanel,
        switchDetailTab,
        switchLogType,
        toggleSpeaker,
        setSpeakerVolume,
        showSettings: showSettings,
        hideSettings,
        onCardVolumeInput,
        onCardVolumeChange,
    };
})();

// Boot on DOM ready
document.addEventListener('DOMContentLoaded', App.init);
