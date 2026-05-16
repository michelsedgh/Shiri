export function makeTestToneBase64({
    seconds = 0.42,
    frequency = 880,
    sampleRate = 44100,
    volume = 0.24,
} = {}) {
    const channels = 2;
    const bitsPerSample = 16;
    const bytesPerSample = bitsPerSample / 8;
    const frameCount = Math.max(1, Math.floor(seconds * sampleRate));
    const dataSize = frameCount * channels * bytesPerSample;
    const buffer = new ArrayBuffer(44 + dataSize);
    const view = new DataView(buffer);

    writeAscii(view, 0, 'RIFF');
    view.setUint32(4, 36 + dataSize, true);
    writeAscii(view, 8, 'WAVE');
    writeAscii(view, 12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, channels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * channels * bytesPerSample, true);
    view.setUint16(32, channels * bytesPerSample, true);
    view.setUint16(34, bitsPerSample, true);
    writeAscii(view, 36, 'data');
    view.setUint32(40, dataSize, true);

    let offset = 44;
    for (let frame = 0; frame < frameCount; frame += 1) {
        const t = frame / sampleRate;
        const fade = envelope(frame, frameCount);
        const sample = Math.round(Math.sin(2 * Math.PI * frequency * t) * volume * fade * 32767);
        view.setInt16(offset, sample, true);
        view.setInt16(offset + 2, sample, true);
        offset += 4;
    }

    return bytesToBase64(new Uint8Array(buffer));
}

function envelope(frame, frameCount) {
    const edge = Math.max(1, Math.floor(frameCount * 0.08));
    if (frame < edge) return frame / edge;
    if (frame > frameCount - edge) return (frameCount - frame) / edge;
    return 1;
}

function writeAscii(view, offset, text) {
    for (let index = 0; index < text.length; index += 1) {
        view.setUint8(offset + index, text.charCodeAt(index));
    }
}

function bytesToBase64(bytes) {
    let binary = '';
    const chunkSize = 0x8000;
    for (let index = 0; index < bytes.length; index += chunkSize) {
        const chunk = bytes.subarray(index, index + chunkSize);
        binary += String.fromCharCode(...chunk);
    }
    return btoa(binary);
}
