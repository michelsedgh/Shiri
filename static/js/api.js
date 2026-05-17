export class ApiError extends Error {
    constructor(message, status, payload) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        this.payload = payload;
    }
}

export async function api(path, options = {}) {
    const { method = 'GET', body = null, headers = {} } = options;
    const request = {
        method,
        headers: { ...headers },
    };

    if (body !== null && body !== undefined) {
        request.headers['Content-Type'] = 'application/json';
        request.body = JSON.stringify(body);
    }

    const response = await fetch(`/api${path}`, request);
    const text = await response.text();
    let payload = null;
    if (text) {
        try {
            payload = JSON.parse(text);
        } catch {
            payload = { error: text };
        }
    }

    if (!response.ok) {
        const message = payload?.error || response.statusText || 'Request failed';
        throw new ApiError(message, response.status, payload);
    }
    return payload;
}

export const Api = {
    dashboard: () => api('/dashboard'),
    settings: () => api('/settings'),
    saveSettings: (body) => api('/settings', { method: 'PUT', body }),
    interfaces: () => api('/system/interfaces'),
    createZone: (body) => api('/zones', { method: 'POST', body }),
    updateZone: (zoneId, body) => api(`/zones/${encodeURIComponent(zoneId)}`, { method: 'PUT', body }),
    deleteZone: (zoneId) => api(`/zones/${encodeURIComponent(zoneId)}`, { method: 'DELETE' }),
    startZone: (zoneId) => api(`/zones/${encodeURIComponent(zoneId)}/start`, { method: 'POST' }),
    stopZone: (zoneId) => api(`/zones/${encodeURIComponent(zoneId)}/stop`, { method: 'POST' }),
    bindRoom: (roomId, body) => api(`/rooms/${encodeURIComponent(roomId)}/binding`, { method: 'PUT', body }),
    setRoomVolume: (roomId, volume) => api(`/rooms/${encodeURIComponent(roomId)}/volume`, {
        method: 'PUT',
        body: { volume },
    }),
    setRoomTtsPolicy: (roomId, body) => api(`/rooms/${encodeURIComponent(roomId)}/tts-policy`, {
        method: 'PUT',
        body,
    }),
    getSpeakers: (zoneId) => api(`/zones/${encodeURIComponent(zoneId)}/speakers`),
    setSpeakers: (zoneId, speakerIds) => api(`/zones/${encodeURIComponent(zoneId)}/speakers`, {
        method: 'PUT',
        body: { speaker_ids: speakerIds },
    }),
    setSpeakerVolume: (zoneId, speakerId, volume) => api(
        `/zones/${encodeURIComponent(zoneId)}/speakers/${encodeURIComponent(speakerId)}/volume`,
        { method: 'PUT', body: { volume } },
    ),
    logs: ({ roomId = '', zoneId = '', type = 'all', lines = 240 } = {}) => {
        const params = new URLSearchParams({ type, lines: String(lines) });
        if (roomId) params.set('room_id', roomId);
        if (zoneId) params.set('zone_id', zoneId);
        return api(`/logs?${params.toString()}`);
    },
};
