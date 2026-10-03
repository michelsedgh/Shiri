"""Bounded, reviewable relative-arrival measurements; never a playback scheduler.

Two simultaneously recorded channels must share one ADC clock. Their microphone
geometry is part of the result. Raw PCM is analyzed and discarded, never retained
by the server. Browser timestamps and a mono acoustic sum cannot identify output
delays. NumPy is loaded only when this optional analysis feature is used.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import secrets
import statistics
import time
import wave
from dataclasses import dataclass, field

from pydantic import Field, ValidationError

from shiri.domain import Conflict, NotFound, Room, StrictModel, ValidationIssue

ANALYSIS_VERSION = "shared-adc-probe-v1"
RATE = 48000
MARKERS = 8
START = 0.4
INTERVAL = 0.65
BURST_SECONDS = 0.12
PROBE_SECONDS = 5.6
MAX_WAV_BYTES = 2 * 1024 * 1024
MAX_RECORDINGS = 12
SESSION_SECONDS = 3600
HISTORY_SECONDS = 90 * 24 * 3600
MAX_HISTORY = 128
MAX_ACTIVE = 16
MAX_EVIDENCE_BYTES = 256 * 1024
MIN_VALID = 20
MIN_RECORDINGS = 3


def numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise ValidationIssue("Calibration analysis requires Shiri's audio dependency (NumPy)") from exc
    return np


def available():
    try:
        numpy()
        return True
    except ValidationIssue:
        return False


def patterns(seed: str, rate: int):
    """Different deterministic, band-limited markers; no repeating-tone alias."""
    np = numpy()
    count = round(BURST_SECONDS * RATE)
    result = []
    for index in range(MARKERS):
        digest = hashlib.sha256(f"{seed}:{index}".encode()).digest()
        rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
        signal = rng.normal(size=count)
        frequencies = np.fft.rfftfreq(count, 1 / RATE)
        spectrum = np.fft.rfft(signal)
        spectrum[(frequencies < 600) | (frequencies > 6500)] = 0
        signal = np.fft.irfft(spectrum, n=count) * np.hanning(count)
        signal *= 0.12 / max(float(np.max(np.abs(signal))), 1e-12)
        if rate != RATE:
            signal = np.interp(np.arange(round(BURST_SECONDS * rate)) * RATE / rate,
                               np.arange(count), signal)
        result.append(signal)
    return result


def probe_pcm(seed: str, rate: int = RATE):
    np = numpy()
    signal = np.zeros(round(PROBE_SECONDS * rate))
    for index, marker in enumerate(patterns(seed, rate)):
        start = round((START + index * INTERVAL) * rate)
        signal[start:start + len(marker)] = marker
    return np.rint(signal * 32767).astype("<i2")


def probe_wav(seed: str):
    pcm = probe_pcm(seed)
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(pcm.tobytes())
    return output.getvalue()


def read_wav(data: bytes):
    np = numpy()
    if not data or len(data) > MAX_WAV_BYTES:
        raise ValidationIssue("Import a PCM WAV no larger than 2 MiB")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            rate, channels, frames = audio.getframerate(), audio.getnchannels(), audio.getnframes()
            if audio.getsampwidth() != 2 or audio.getcomptype() != "NONE" or channels != 2:
                raise ValidationIssue("Recording must be uncompressed signed 16-bit stereo WAV, with one microphone per channel")
            if not 16000 <= rate <= 96000 or not 3 * rate <= frames <= 16 * rate:
                raise ValidationIssue("Use a 16–96 kHz recording lasting 3–16 seconds")
            raw = audio.readframes(frames)
            if len(raw) != frames * 4:
                raise ValidationIssue("Recording contains truncated stereo frames")
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        raise ValidationIssue("Recording is not a complete supported WAV file") from exc
    return np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float64) / 32768, rate


def matched(channel, marker, begin=0, end=None):
    """Normalized valid correlation and an independent competing-peak check."""
    np = numpy()
    end = len(channel) if end is None else end
    region = channel[begin:end]
    count = len(marker)
    if len(region) < count:
        return None, "incomplete marker window"
    fft_size = 1 << (len(region) + count - 2).bit_length()
    correlation = np.fft.irfft(np.fft.rfft(region, fft_size) * np.fft.rfft(marker[::-1], fft_size), fft_size)
    correlation = correlation[count - 1:len(region)]
    energy = np.concatenate(([0.0], np.cumsum(region * region)))
    energy = energy[count:] - energy[:-count]
    denominator = np.sqrt(np.maximum(energy, 0) * float(np.dot(marker, marker)))
    score = np.divide(np.abs(correlation), denominator, out=np.zeros_like(correlation), where=denominator > 1e-10)
    peak = int(np.argmax(score))
    confidence = float(score[peak])
    # Nearby samples belong to one band-limited arrival; distinct echoes do not.
    exclusion = max(1, round(count / BURST_SECONDS * .001))
    other = score.copy()
    other[max(0, peak - exclusion):peak + exclusion + 1] = 0
    second = float(np.max(other)) if len(other) else 0
    if confidence < .65:
        return None, "probe correlation is weak (noise, missing probe or processing)"
    if second > confidence * .78:
        return None, "multiple plausible arrival peaks (echo or acoustic crosstalk)"
    at = begin + peak
    window = channel[at:at + count]
    if float(np.mean(np.abs(window) >= 32760 / 32768)) > .0005:
        return None, "marker clips the capture input"
    # A lost PCM segment may leave a deceptively strong partial correlation.
    signal_mask = np.abs(marker) > float(np.max(np.abs(marker))) * .15
    if float(np.mean(np.abs(window[signal_mask]) < 1 / 32768)) > .03:
        return None, "marker contains silence or missing samples"
    return {"sample": at, "confidence": confidence, "competing_peak": second,
            "polarity": 1 if correlation[peak] >= 0 else -1}, None


def analyze_wav(data: bytes, seed: str, max_lag_ms=500, geometry_correction_ms=0.0):
    samples, rate = read_wav(data)
    detections = []
    earliest = 0
    for index, marker in enumerate(patterns(seed, rate)):
        # Reference arrival ordering is mandatory, rather than assuming an
        # unrelated phone/browser playback-start timestamp.
        reference, error = matched(samples[:, 0], marker, earliest)
        if reference is None:
            detections.append({"marker": index, "startup": index == 0, "accepted": False, "reason": error})
            continue
        margin = round(max_lag_ms * rate / 1000)
        target, error = matched(samples[:, 1], marker, max(0, reference["sample"] - margin),
                                min(len(samples), reference["sample"] + margin + len(marker)))
        earliest = reference["sample"] + len(marker)
        if target is None:
            detections.append({"marker": index, "startup": index == 0, "accepted": False, "reason": error})
            continue
        lag_samples = target["sample"] - reference["sample"]
        detections.append({"marker": index, "startup": index == 0, "accepted": True,
                           "at_seconds": reference["sample"] / rate, "lag_samples": lag_samples,
                           "lag_ms": lag_samples * 1000 / rate - geometry_correction_ms,
                           "confidence": min(reference["confidence"], target["confidence"]),
                           "reference_polarity": reference["polarity"], "target_polarity": target["polarity"]})
    drift = estimate_drift(detections, rate)
    # Container metadata changes are not independent evidence.
    pcm_hash = hashlib.sha256(samples.astype("<f8").tobytes()).hexdigest()
    return {"sha256": hashlib.sha256(data).hexdigest(), "pcm_sha256": pcm_hash,
            "sample_rate": rate, "frames": len(samples),
            "duration_seconds": len(samples) / rate, "markers": detections, "drift": drift}


def estimate_drift(markers, rate):
    """OLS from retained steady markers; auditable without an audio dependency."""
    valid = [marker for marker in markers if marker["accepted"] and not marker["startup"]]
    if len(valid) < 4:
        return None
    x, y = [marker["at_seconds"] for marker in valid], [marker["lag_ms"] for marker in valid]
    mean_x, mean_y = math.fsum(x) / len(x), math.fsum(y) / len(y)
    centered = [value - mean_x for value in x]
    denominator = math.fsum(value * value for value in centered)
    slope = math.fsum(dx * (dy - mean_y) for dx, dy in zip(centered, y, strict=True)) / denominator if denominator > 0 else 0.0
    residual = [dy - (mean_y + slope * dx) for dx, dy in zip(centered, y, strict=True)]
    se = math.sqrt(math.fsum(value * value for value in residual) / max(1, len(x) - 2) / denominator) if denominator > 0 else 0.0
    span = max(x) - min(x)
    median_residual = statistics.median(residual)
    return {"ms_per_minute": slope * 60, "uncertainty_ms_per_minute": max(se * 2.5 * 60, 60 * 1000 / rate / max(span, .1)),
            "span_seconds": span, "residual_mad_ms": statistics.median(abs(value - median_residual) for value in residual)}


def summary(recordings: list[dict], previous_offset: int, *, verification=False):
    markers = [m for record in recordings for m in record["markers"] if not m["startup"]]
    valid = [marker for marker in markers if marker["accepted"]]
    reasons = []
    values = sorted(marker["lag_ms"] for marker in valid)

    def percentile(percent):
        at = (len(values) - 1) * percent / 100
        lower = math.floor(at)
        return values[lower] + (values[math.ceil(at)] - values[lower]) * (at - lower)
    median = float(statistics.median(values)) if values else None
    mad = float(statistics.median(abs(value - median) for value in values)) if values else None
    spread = float(percentile(95) - percentile(5)) if values else None
    resolution = max((1000 / record["sample_rate"] for record in recordings), default=None)
    if len(recordings) < MIN_RECORDINGS or len(valid) < MIN_VALID:
        reasons.append("Need at least three distinct recordings and twenty valid steady-state markers")
    if markers and len(valid) / len(markers) < .8:
        reasons.append("Too many rejected markers; improve capture quality before applying a correction")
    if mad is not None and (mad > .35 or spread > 1.0):
        reasons.append("Arrival delay varies; one constant offset cannot correct this jitter")
    drifts = [record["drift"] for record in recordings if record["drift"]]
    for drift in drifts:
        if (abs(drift["ms_per_minute"]) > drift["uncertainty_ms_per_minute"]
                and abs(drift["ms_per_minute"]) * drift["span_seconds"] / 60 > .5):
            reasons.append("Delay changes during the recording; investigate clock drift instead of adding a fixed offset")
            break
    # Target later => negative correction; positive offsets delay the target.
    candidate = previous_offset - round(median) if median is not None else None
    if not verification and candidate is not None and not -2000 <= candidate <= 2000:
        reasons.append("Candidate is outside OwnTone's supported offset range")
    return {"status": "insufficient_evidence" if reasons else "stable_measurement" if verification else "candidate_correction", "reasons": reasons,
            "recordings": len(recordings), "accepted_markers": len(valid), "rejected_markers": len(markers) - len(valid),
            "median_lag_ms": median, "mad_ms": mad, "p05_ms": float(percentile(5)) if values else None,
            "p95_ms": float(percentile(95)) if values else None,
            "minimum_confidence": min((m["confidence"] for m in valid), default=None),
            "sample_resolution_ms": resolution,
            "observed_uncertainty_ms": max(resolution, spread / 2) if resolution is not None and spread is not None else None,
            "drift_estimates": drifts, "candidate_offset_ms": candidate if not reasons and not verification else None,
            "sign": "Positive lag means target arrives later; positive offset delays the target",
            "scope": "Relative arrival in user-imported shared-ADC recordings at the declared geometry; short recordings do not validate long-term drift or universal synchronization",
            "confidence_note": "Normalized correlation is a waveform-match score, not a probability of correct speaker identity"}


def fingerprint(room):
    result = room.model_dump(mode="json", exclude={"revision", "enabled", "volume"})
    # Default settings preserve existing calibration evidence across migration.
    for speaker in result["speakers"]:
        if speaker["balance_percent"] == 100:
            speaker.pop("balance_percent")
        if speaker["airplay_timing"] == "auto":
            speaker.pop("airplay_timing")
    return result


class _SavedCalibration(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{32}$")
    room_id: str
    target_id: str
    reference_id: str
    reference_room_id: str | None = None
    reference_revision: int | None = Field(default=None, ge=1)
    reference_configuration: dict | None = None
    playback_context: str = Field(default="Same-room playback through the declared music source", min_length=1, max_length=512)
    room_revision: int = Field(ge=1)
    configuration: dict
    previous_offset_ms: int = Field(ge=-2000, le=2000)
    capture_device: str = Field(min_length=1, max_length=256)
    geometry: str = Field(min_length=1, max_length=512)
    max_lag_ms: int = Field(ge=1, le=2000)
    geometry_correction_ms: float = Field(ge=-100, le=100, allow_inf_nan=False)
    seed: str = Field(pattern=r"^[0-9a-f]{32}$")
    created_at: float = Field(gt=0, allow_inf_nan=False)
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    generation: int = Field(ge=0, le=2**63 - 2)
    analysis_version: str
    probe_metadata: dict
    recordings: list[dict] = Field(max_length=MAX_RECORDINGS)
    verification_recordings: list[dict] = Field(max_length=MAX_RECORDINGS)
    verification_backend: list[dict] = Field(max_length=MAX_RECORDINGS)
    result: dict
    applied_offset_ms: int | None = Field(ge=-2000, le=2000)
    applied_revision: int | None = Field(ge=1)
    application: dict | None
    rolled_back: bool


def validate_record(record):
    """Audit durable JSON without NumPy or trusting cached candidate numbers."""
    try:
        encoded = json.dumps(record, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode()) > MAX_EVIDENCE_BYTES:
            raise ValueError("Measurement summary exceeds its bounded capacity")
        saved = _SavedCalibration.model_validate(record)
        # Older v2 summaries were explicitly same-room measurements. Normalize
        # their absent reference fields without rewriting saved intent.
        if saved.reference_room_id is None:
            if saved.reference_revision is not None or saved.reference_configuration is not None:
                raise ValueError("Incomplete reference identity")
            saved = _SavedCalibration.model_validate({**saved.model_dump(), "reference_room_id": saved.room_id,
                                                      "reference_revision": saved.room_revision,
                                                      "reference_configuration": saved.configuration})
        elif saved.reference_revision is None or saved.reference_configuration is None:
            raise ValueError("Incomplete reference configuration")
        if saved.analysis_version != ANALYSIS_VERSION:
            raise ValueError("Unsupported retained analysis version")
        room = Room.model_validate({**saved.configuration, "enabled": False, "volume": 50, "revision": saved.room_revision})
        reference_room = Room.model_validate({**saved.reference_configuration, "enabled": False, "volume": 50, "revision": saved.reference_revision})
        if (room.id != saved.room_id or fingerprint(room) != saved.configuration
                or reference_room.id != saved.reference_room_id or fingerprint(reference_room) != saved.reference_configuration
                or saved.reference_room_id == saved.room_id and (saved.target_id == saved.reference_id
                    or saved.reference_revision != saved.room_revision or saved.reference_configuration != saved.configuration)
                or saved.expires_at < saved.created_at
                or saved.expires_at > saved.created_at + HISTORY_SECONDS + SESSION_SECONDS + 1):
            raise ValueError("Invalid measurement identity, configuration or retention")
        targets = [speaker for speaker in room.speakers if speaker.id == saved.target_id]
        references = [speaker for speaker in reference_room.speakers if speaker.id == saved.reference_id]
        if len(targets) != 1 or len(references) != 1:
            raise ValueError("Measurement outputs must be exact and unambiguous")
        target = targets[0]
        if target.offset_ms != saved.previous_offset_ms:
            raise ValueError("Previous offset does not match the retained speaker configuration")
        for text in [saved.capture_device, saved.geometry, saved.playback_context]:
            if text != text.strip() or any(ord(char) < 32 or ord(char) == 127 for char in text):
                raise ValueError("Invalid capture description")
        probe = saved.probe_metadata
        if (set(probe) != {"sample_rate", "markers", "duration_seconds", "seed", "marker_seconds", "first_marker_seconds",
                           "interval_seconds", "numpy_version", "sha256"}
                or probe["seed"] != saved.seed or probe["sample_rate"] != RATE or probe["markers"] != MARKERS
                or probe["duration_seconds"] != PROBE_SECONDS or probe["marker_seconds"] != BURST_SECONDS
                or probe["first_marker_seconds"] != START or probe["interval_seconds"] != INTERVAL
                or not isinstance(probe["numpy_version"], str) or not 1 <= len(probe["numpy_version"]) <= 64
                or not isinstance(probe["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", probe["sha256"])):
            raise ValueError("Invalid retained probe identity")
        if ((saved.applied_revision is None) != (saved.applied_offset_ms is None)
                or saved.rolled_back and saved.applied_revision is None
                or saved.applied_revision is None and (saved.application or saved.verification_recordings or saved.verification_backend)
                or len(saved.verification_backend) != len(saved.verification_recordings)):
            raise ValueError("Invalid calibration application or verification stage")
        for recording in saved.recordings + saved.verification_recordings:
            _validate_recording(recording, saved)
        hashes = {(r["sample_rate"], r["pcm_sha256"]) for r in saved.recordings + saved.verification_recordings}
        if len(hashes) != len(saved.recordings) + len(saved.verification_recordings):
            raise ValueError("Duplicate stored PCM measurement")
        if saved.result != summary(saved.recordings, saved.previous_offset_ms):
            raise ValueError("Stored candidate does not match its recorded evidence")
        if saved.applied_revision is not None and (saved.applied_revision < saved.room_revision
                or saved.result["status"] != "candidate_correction" or saved.applied_offset_ms != saved.result["candidate_offset_ms"]):
            raise ValueError("Saved correction does not match its reviewed candidate")
        if saved.application is not None:
            if (set(saved.application) != {"runtime_accepted", "state", "pending_reason", "message"}
                    or type(saved.application["runtime_accepted"]) is not bool or saved.application["state"] != "saved_room_off"
                    or not _bounded_text(saved.application["message"], 512)
                    or saved.application["pending_reason"] is not None and not _bounded_text(saved.application["pending_reason"], 2048)):
                raise ValueError("Invalid application acknowledgment")
        for backend in saved.verification_backend:
            _validate_backend(backend, saved)
        normalized = saved.model_dump(mode="json")
        if len(json.dumps(normalized, allow_nan=False, separators=(",", ":")).encode()) > MAX_EVIDENCE_BYTES:
            raise ValueError("Normalized evidence exceeds its bounded capacity")
    except (ValidationError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise ValidationIssue("Stored calibration evidence is invalid or exceeds its bounds; database preserved") from exc
    return normalized


def _finite(value):
    return type(value) in {int, float} and math.isfinite(value)


def _bounded_text(value, limit):
    return isinstance(value, str) and 1 <= len(value) <= limit and not any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in value)


def _validate_versions(versions):
    if (not isinstance(versions, dict) or len(versions) > 16
            or any(not _bounded_text(key, 64) or not _bounded_text(value, 4096) for key, value in versions.items())):
        raise ValueError("Invalid retained backend version evidence")


def _validate_backend(backend, session):
    allowed = {"room_revision", "simulation", "versions", "reported_offsets", "reference_room_revision", "reported_endpoints"}
    if (not isinstance(backend, dict) or set(backend) - allowed
            or not {"room_revision", "simulation", "versions"} <= backend.keys()
            or type(backend["room_revision"]) is not int or backend["room_revision"] < session.applied_revision
            or type(backend["simulation"]) is not bool):
        raise ValueError("Invalid verification backend evidence")
    reference = next(s for s in session.reference_configuration["speakers"] if s["id"] == session.reference_id)
    if "reported_endpoints" in backend:
        expected = [{"room_id": session.room_id, "output_id": session.target_id, "offset_ms": session.applied_offset_ms},
                    {"room_id": session.reference_room_id, "output_id": session.reference_id, "offset_ms": reference["offset_ms"]}]
        if backend["reported_endpoints"] != expected or any(type(entry["offset_ms"]) is not int for entry in backend["reported_endpoints"]):
            raise ValueError("Verification endpoints do not match their exact room/profile identities")
        if type(backend.get("reference_room_revision")) is not int or backend["reference_room_revision"] < session.reference_revision:
            raise ValueError("Invalid reference-room verification revision")
    elif session.reference_room_id != session.room_id:
        raise ValueError("Cross-room verification requires exact endpoint identities")
    if "reported_offsets" in backend:
        expected_offsets = {session.target_id: session.applied_offset_ms, session.reference_id: reference["offset_ms"]}
        if (session.reference_room_id != session.room_id or backend["reported_offsets"] != expected_offsets
                or any(type(value) is not int for value in backend["reported_offsets"].values())):
            raise ValueError("Verification offsets require unambiguous room-scoped identities")
    elif "reported_endpoints" not in backend:
        raise ValueError("Missing verified offset evidence")
    _validate_versions(backend["versions"])


def _validate_recording(recording, session):
    if (set(recording) - {"sha256", "pcm_sha256", "sample_rate", "frames", "duration_seconds", "markers", "drift", "environment_at_import"}
            or any(not isinstance(recording.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", recording[key]) for key in ["sha256", "pcm_sha256"])
            or type(recording.get("sample_rate")) is not int or not 16000 <= recording["sample_rate"] <= 96000
            or type(recording.get("frames")) is not int or not 3 * recording["sample_rate"] <= recording["frames"] <= 16 * recording["sample_rate"]
            or not _finite(recording.get("duration_seconds")) or recording["duration_seconds"] != recording["frames"] / recording["sample_rate"]
            or not isinstance(recording.get("markers"), list) or len(recording["markers"]) != MARKERS):
        raise ValueError("Invalid recorded PCM summary")
    for index, marker in enumerate(recording["markers"]):
        if (not isinstance(marker, dict) or marker.get("marker") != index or type(marker.get("marker")) is not int
                or marker.get("startup") is not (index == 0) or type(marker.get("accepted")) is not bool):
            raise ValueError("Invalid marker identity")
        if marker["accepted"]:
            if (set(marker) != {"marker", "startup", "accepted", "at_seconds", "lag_samples", "lag_ms", "confidence", "reference_polarity", "target_polarity"}
                    or type(marker["lag_samples"]) is not int or abs(marker["lag_samples"]) > round(session.max_lag_ms * recording["sample_rate"] / 1000)
                    or not _finite(marker["lag_ms"]) or abs(marker["lag_ms"] - (marker["lag_samples"] * 1000 / recording["sample_rate"] - session.geometry_correction_ms)) > 1e-9
                    or not _finite(marker["at_seconds"]) or not 0 <= marker["at_seconds"] < recording["duration_seconds"]
                    or not _finite(marker["confidence"]) or not .65 <= marker["confidence"] <= 1.000000001
                    or type(marker["reference_polarity"]) is not int or marker["reference_polarity"] not in {-1, 1}
                    or type(marker["target_polarity"]) is not int or marker["target_polarity"] not in {-1, 1}):
                raise ValueError("Invalid accepted marker measurement")
        elif (set(marker) != {"marker", "startup", "accepted", "reason"} or not isinstance(marker["reason"], str)
                or not 1 <= len(marker["reason"]) <= 512):
            raise ValueError("Invalid rejected marker measurement")
    drift = recording.get("drift")
    if drift is not None and (not isinstance(drift, dict)
            or set(drift) != {"ms_per_minute", "uncertainty_ms_per_minute", "span_seconds", "residual_mad_ms"}
            or not all(_finite(value) for value in drift.values()) or drift["uncertainty_ms_per_minute"] < 0
            or not 0 < drift["span_seconds"] <= recording["duration_seconds"] or drift["residual_mad_ms"] < 0):
        raise ValueError("Invalid marker drift estimate")
    expected_drift = estimate_drift(recording["markers"], recording["sample_rate"])
    if ((drift is None) != (expected_drift is None)
            or drift is not None and any(not math.isclose(drift[key], value, rel_tol=1e-7, abs_tol=1e-9)
                                         for key, value in expected_drift.items())):
        raise ValueError("Retained drift does not match its steady-state marker measurements")
    environment = recording.get("environment_at_import")
    if environment is not None:
        if (not isinstance(environment, dict)
                or set(environment) - {"room_revision", "volume", "enabled", "observed_at", "provenance", "versions", "simulation", "runtime_unavailable", "reported_offsets", "reported_endpoints", "reference_room_revision", "reference_room_id", "reference_volume", "reference_enabled"}
                or not {"room_revision", "volume", "enabled", "observed_at", "provenance"} <= environment.keys()
                or type(environment["room_revision"]) is not int or environment["room_revision"] < session.room_revision
                or type(environment["volume"]) is not int or not 0 <= environment["volume"] <= 100
                or type(environment["enabled"]) is not bool or not _finite(environment["observed_at"]) or environment["observed_at"] <= 0
                or not _bounded_text(environment["provenance"], 512)
                or "simulation" in environment and type(environment["simulation"]) is not bool
                or "runtime_unavailable" in environment and environment["runtime_unavailable"] is not True):
            raise ValueError("Invalid import-time environment evidence")
        if "versions" in environment:
            _validate_versions(environment["versions"])
        reference_keys = {"reference_room_id", "reference_room_revision", "reference_volume", "reference_enabled"}
        if reference_keys & environment.keys():
            if (not reference_keys <= environment.keys() or environment["reference_room_id"] != session.reference_room_id
                    or type(environment["reference_room_revision"]) is not int or environment["reference_room_revision"] < session.reference_revision
                    or type(environment["reference_volume"]) is not int or not 0 <= environment["reference_volume"] <= 100
                    or type(environment["reference_enabled"]) is not bool):
                raise ValueError("Invalid reference-room import observation")
        if "reported_offsets" in environment or "reported_endpoints" in environment:
            _validate_backend({key: environment[key] for key in ["room_revision", "simulation", "versions", "reported_offsets", "reference_room_revision", "reported_endpoints"] if key in environment}, session)


@dataclass
class CalibrationSession:
    id: str
    room_id: str
    target_id: str
    reference_id: str
    room_revision: int
    configuration: dict
    previous_offset_ms: int
    capture_device: str
    geometry: str
    max_lag_ms: int
    geometry_correction_ms: float
    reference_room_id: str | None = None
    reference_revision: int | None = None
    reference_configuration: dict | None = None
    playback_context: str = "Same-room playback through the declared music source"
    seed: str = field(default_factory=lambda: secrets.token_hex(16))
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + SESSION_SECONDS)
    deadline: float = field(default_factory=lambda: time.monotonic() + SESSION_SECONDS)
    generation: int = 0
    analysis_version: str = ANALYSIS_VERSION
    probe_metadata: dict = field(default_factory=dict)
    recordings: list = field(default_factory=list)
    verification_recordings: list = field(default_factory=list)
    verification_backend: list = field(default_factory=list)
    result: dict | None = None
    applied_offset_ms: int | None = None
    applied_revision: int | None = None
    application: dict | None = None
    rolled_back: bool = False

    def public(self):
        result = self.result or summary([], self.previous_offset_ms)
        verified = summary(self.verification_recordings, self.applied_offset_ms or 0, verification=True) if self.verification_recordings else None
        verified_ok = (verified and verified["status"] == "stable_measurement" and abs(verified["median_lag_ms"]) <= 1.0)
        verification_failed = verified and verified["status"] == "stable_measurement" and not verified_ok
        if verified:
            verified["alignment_status"] = "verified" if verified_ok else "not_aligned" if verification_failed else "insufficient_evidence"
        status = ("rolled_back" if self.rolled_back else "measured_at_this_setup" if verified_ok
                  else "verification_failed" if verification_failed else "offset_saved"
                  if self.applied_revision is not None else result["status"])
        return {"id": self.id, "room_id": self.room_id, "target_id": self.target_id, "reference_id": self.reference_id,
                "reference_room_id": self.reference_room_id or self.room_id, "reference_revision": self.reference_revision or self.room_revision,
                "reference_configuration": self.reference_configuration or self.configuration, "playback_context": self.playback_context,
                "grouping_provenance": "Operator-declared playback context; Shiri has not observed or verified the phone's native grouping",
                "room_revision": self.room_revision, "created_at": self.created_at,
                "expires_at": self.expires_at, "generation": self.generation,
                "expires_in_seconds": max(0, round(self.deadline - time.monotonic())),
                "status": status, "analysis_version": self.analysis_version, "capture_device": self.capture_device,
                "geometry": self.geometry, "geometry_correction_ms": self.geometry_correction_ms,
                "max_lag_ms": self.max_lag_ms, "configuration": self.configuration,
                "previous_offset_ms": self.previous_offset_ms, "result": result,
                "recordings": self.recordings, "applied_offset_ms": self.applied_offset_ms,
                "applied_revision": self.applied_revision, "application": self.application,
                "verification": verified, "verification_recordings": self.verification_recordings,
                "verification_backend": self.verification_backend,
                "probe": self.probe_metadata}

    def record(self):
        return {key: value for key, value in vars(self).items() if key != "deadline"}

    @classmethod
    def from_record(cls, record):
        record = validate_record(record)
        session = cls(**record)
        session.deadline = time.monotonic() + max(0, session.expires_at - time.time())
        return session


class CalibrationSessions:
    """Bounded process-local cache; SQLite owns durable sessions and receipts."""
    def __init__(self, *, capacity=MAX_ACTIVE):
        self.sessions = {}
        self.capacity = capacity

    def prune(self):
        now = time.monotonic()
        for key in list(self.sessions):
            if self.sessions[key].deadline <= now:
                del self.sessions[key]
        for session in sorted(self.sessions.values(), key=lambda item: item.created_at)[:-MAX_HISTORY]:
            del self.sessions[session.id]

    def get(self, room_id, session_id):
        self.prune()
        session = self.sessions.get(session_id)
        if not session or session.room_id != room_id:
            raise NotFound("Calibration session does not exist in this room or has expired")
        return session

    def list(self, room_id):
        self.prune()
        return [session.public() for session in self.sessions.values() if session.room_id == room_id]

    def remove_room(self, room_id):
        for key, session in list(self.sessions.items()):
            if session.room_id == room_id:
                del self.sessions[key]

    def create(self, room, *, target_id, reference_id, capture_device, geometry, max_lag_ms, geometry_correction_ms,
               reference_room=None, playback_context=None, enforce_capacity=True):
        self.prune()
        numpy()
        if enforce_capacity and sum(session.applied_revision is None for session in self.sessions.values()) >= self.capacity:
            raise Conflict("Too many calibration sessions; close an existing session before starting another")
        assigned = {speaker.id: speaker for speaker in room.speakers}
        reference_room = reference_room or room
        reference_outputs = {speaker.id: speaker for speaker in reference_room.speakers}
        if (room.id == reference_room.id and target_id == reference_id or target_id not in assigned
                or reference_id not in reference_outputs):
            raise ValidationIssue("Choose two different exact room/speaker endpoints already assigned to their rooms")
        session = CalibrationSession(secrets.token_urlsafe(24), room.id, target_id, reference_id, room.revision,
                                     fingerprint(room), assigned[target_id].offset_ms, capture_device, geometry,
                                     max_lag_ms, geometry_correction_ms, reference_room_id=reference_room.id,
                                     reference_revision=reference_room.revision, reference_configuration=fingerprint(reference_room),
                                     playback_context=playback_context or "Same-room playback through the declared music source")
        self.sessions[session.id] = session
        session.result = summary([], session.previous_offset_ms)
        session.probe_metadata = {"sample_rate": RATE, "markers": MARKERS, "duration_seconds": PROBE_SECONDS, "seed": session.seed,
                                  "marker_seconds": BURST_SECONDS, "first_marker_seconds": START, "interval_seconds": INTERVAL,
                                  "numpy_version": numpy().__version__, "sha256": hashlib.sha256(probe_wav(session.seed)).hexdigest()}
        return session

    def check_recording(self, session, *, verification=False):
        records = session.verification_recordings if verification else session.recordings
        if session.rolled_back or (session.applied_revision is not None) != verification:
            raise Conflict("This session is not accepting recordings for that measurement stage")
        if len(records) >= MAX_RECORDINGS:
            raise Conflict("Recording limit reached; export this evidence and start a new session")
    def add_result(self, session, result, *, verification=False):
        self.check_recording(session, verification=verification)
        records = session.verification_recordings if verification else session.recordings
        if any(record["pcm_sha256"] == result["pcm_sha256"] and record["sample_rate"] == result["sample_rate"]
               for record in session.recordings + session.verification_recordings):
            raise Conflict("This PCM recording was already analyzed; repetitions require fresh captures")
        records.append(result)
        if not verification:
            session.result = summary(records, session.previous_offset_ms)
        return session.public()
