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

export function roomLabel(room) {
    return room?.room_name || room?.room_id || 'Room';
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

export function activityText(room) {
    const names = room?.presence_names || [];
    const activities = room?.activities || [];
    if (names.length && activities.length) {
        const activity = activities[0]?.activity || 'present';
        return `${names.join(', ')} - ${activity}`;
    }
    if (names.length) return names.join(', ');
    if (activities.length) return activities.map((item) => item.activity || 'activity').join(', ');
    return 'No presence';
}

export function debounce(fn, delay = 350) {
    let timer = null;
    return (...args) => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => fn(...args), delay);
    };
}

export function formatPct(value, fallback = 0) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return `${fallback}%`;
    return `${Math.round(parsed)}%`;
}

export function nowRequestId(prefix) {
    return `${prefix}_${Date.now().toString(36)}`;
}
