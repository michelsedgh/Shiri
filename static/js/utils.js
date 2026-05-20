export function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value ?? '';
    return div.innerHTML;
}

export function clampNumber(value, min, max, fallback) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(Math.max(Math.round(parsed), min), max);
}

export function zoneLabel(zone) {
    return zone?.zone_name || zone?.zone_id || 'Zone';
}

export function statusClass(status) {
    if (status === 'running') return 'running';
    if (status === 'starting' || status === 'stopping') return status;
    if (status === 'error') return 'error';
    return 'stopped';
}

export function selectedSpeakerText(speakers = []) {
    const selected = speakers.filter((speaker) => speaker.selected);
    const items = selected.length ? selected : speakers;
    if (!items.length) return 'No speakers';
    return items.map((speaker) => speaker.name || speaker.id || 'Speaker').join(', ');
}

export function bindingText(zone) {
    if (!zone?.lionos_room_id) return 'No LionOS binding';
    return zone.lionos_room_name
        ? `${zone.lionos_room_name} / ${zone.lionos_room_id}`
        : zone.lionos_room_id;
}

export function debounce(fn, delay = 350) {
    let timer = null;
    return (...args) => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => fn(...args), delay);
    };
}
