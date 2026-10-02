"""Real AV resampler tails and exact AudioWorker speech retirement."""
from array import array
import asyncio
from contextlib import asynccontextmanager
import sys

from aiortc.mediastreams import MediaStreamError
from av import AudioFrame, AudioResampler
import pytest

from shiri.runtime.audio import AudioWorker, SpeechSession


def frame(rate):
    result = AudioFrame(format="s16", layout="mono", samples=rate // 50)
    result.sample_rate = rate
    result.planes[0].update(array("h", [1000] * result.samples).tobytes())
    return result


def reference(rate):
    converter = AudioResampler(format="s16", layout="mono", rate=48000)
    immediate = converter.resample(frame(rate))
    delayed = converter.resample(None)
    def pcm(frames):
        return b"".join(bytes(item.planes[0])[:item.samples * 2] for item in frames)
    return pcm(immediate), pcm(delayed)


class Peer:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


class Track:
    def __init__(self, rate):
        self.queue = asyncio.Queue()
        self.queue.put_nowait(frame(rate))
        self.waiting = asyncio.Event()
        self.calls = 0

    async def recv(self):
        self.calls += 1
        if self.calls > 1:
            self.waiting.set()
        item = await self.queue.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def eof(self):
        self.queue.put_nowait(MediaStreamError())


class Mixer:
    def __init__(self):
        self.frames = []
        self.gains = []
        self.gain = None
        self.closed = False

    def set_speech_gain(self, gain):
        self.gain = gain

    def push_speech(self, data, samples):
        assert len(data) == samples * 2
        self.frames.append(data)
        self.gains.append(self.gain)

    def close(self):
        self.closed = True

    @property
    def pcm(self):
        return b"".join(self.frames)


@asynccontextmanager
async def receiving(rate, *, idle_seconds=1):
    mixer = Mixer()
    worker = AudioWorker(mixer, idle_seconds=idle_seconds)
    session = SpeechSession("first", "first-request", Peer(), 0.1)
    track = Track(rate)
    worker.session = session
    session.receiver = asyncio.create_task(worker._receive(session, track))
    try:
        await asyncio.wait_for(track.waiting.wait(), timeout=1)
        yield worker, mixer, session, track
    finally:
        await asyncio.wait_for(worker.close(), timeout=1)


@pytest.mark.parametrize("rate", [8000, 16000, 44100, 48000])
async def test_natural_eof_emits_the_complete_actual_av_resampler_tail(rate):
    immediate, delayed = reference(rate)
    assert len(immediate + delayed) == 960 * 2
    assert bool(delayed) is (rate != 48000)
    async with receiving(rate) as (worker, mixer, session, track):
        assert mixer.pcm == immediate
        track.eof()
        await asyncio.wait_for(session.receiver, timeout=1)
        assert mixer.pcm == immediate + delayed
        assert all(gain == 0.1 for gain in mixer.gains)
        assert worker.session is None and session.last_audible > 0
        values = array("h", mixer.pcm)
        if sys.byteorder != "little":
            values.byteswap()
        assert len(values) == 960 and max(abs(value - 1000) for value in values) <= 1


@pytest.mark.parametrize("rate", [8000, 16000, 44100])
async def test_old_track_eof_cannot_drain_history_into_a_successor(rate):
    immediate, delayed = reference(rate)
    assert delayed
    async with receiving(rate) as (worker, mixer, session, track):
        successor = SpeechSession("second", "second-request", Peer(), 0.8)
        worker.session = successor
        track.eof()
        await asyncio.wait_for(session.receiver, timeout=1)
        assert mixer.pcm == immediate and mixer.gains == [0.1]
        assert worker.session is successor


async def test_explicit_close_cancels_receive_without_emitting_the_av_tail():
    immediate, delayed = reference(8000)
    assert delayed
    async with receiving(8000) as (worker, mixer, session, _track):
        await asyncio.wait_for(worker.dispatch("speech", {
            "action": "close", "session_id": "first", "request_id": "close-first",
        }), timeout=1)
        assert mixer.pcm == immediate and session.receiver.cancelled()
        assert worker.session is None and session.peer.closed == 1


async def test_cancelled_receiver_does_not_flush_or_mask_its_cancellation():
    immediate, delayed = reference(16000)
    assert delayed
    async with receiving(16000) as (_worker, mixer, session, _track):
        session.receiver.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session.receiver
        assert mixer.pcm == immediate


async def test_stopping_worker_cannot_drain_even_a_natural_eof():
    immediate, delayed = reference(44100)
    assert delayed
    async with receiving(44100) as (worker, mixer, session, track):
        worker._closing = True
        track.eof()
        await asyncio.wait_for(session.receiver, timeout=1)
        assert mixer.pcm == immediate and worker.session is None


async def test_idle_deadline_discards_history_instead_of_presenting_unreceived_tail():
    immediate, delayed = reference(8000)
    assert delayed
    async with receiving(8000, idle_seconds=0.03) as (worker, mixer, session, _track):
        await asyncio.wait_for(session.receiver, timeout=1)
        assert mixer.pcm == immediate and worker.session is None
