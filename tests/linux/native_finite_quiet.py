"""Finite fixture quiet oracle for its declared mono440Hz carrier.

Only the carrier phase is measured, in a held full-volume PRE-OFFER window.
The quiet window never fits phase/amplitude, a voice endpoint or a time shift.
Its sole nuisance parameter is one bounded onset of the exact native250ms
full-scale restoration slope, with the fixture's immutable wire duck gain.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

import numpy as np

from shiri.runtime.system import RuntimeFailure

RATE = 48000
BASELINE_FRAMES = QUIET_FRAMES = 9600
RESTORE_FRAMES = RATE//4
DUCK_GAIN = .2
WIRE_DUCK = round(DUCK_GAIN*65536)/65536
MAX_FRAMES = RATE*14


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


def basis(first_frame, count):
    # Integer calendar modulo one exact440-cycle second prevents loss of
    # precision on a long capture; it neither fits nor shifts the calendar.
    positions = (first_frame+np.arange(count, dtype=np.int64)) % RATE
    phase = 2*np.pi*440*positions/RATE
    return np.column_stack((np.sin(phase), np.cos(phase)))


@dataclass(frozen=True)
class Carrier:
    first_frame: int
    coefficients: tuple[float, float]
    pcm_sha256: str
    residual_rms: float

    def evidence(self):
        return {'first_frame': self.first_frame, 'frames': BASELINE_FRAMES,
                'coefficients': list(self.coefficients), 'pcm_sha256': self.pcm_sha256,
                'residual_rms': self.residual_rms, 'duck_gain': DUCK_GAIN,
                'wire_duck_gain': WIRE_DUCK, 'restore_step_per_frame': 1/RESTORE_FRAMES,
                'restore_onset_bounds_frames': [-RESTORE_FRAMES, RESTORE_FRAMES],
                'scope': 'Frozen mono440Hz full-volume carrier; exact capture frame calendar; one native fixed-slope restoration onset; caller proves pre-offer admission'}


def freeze_carrier(data, *, first_frame, duck_gain):
    require(type(data) is bytes and len(data) == BASELINE_FRAMES*4
            and type(first_frame) is int and 0 <= first_frame < 2**53
            and type(duck_gain) in (int, float) and duck_gain == DUCK_GAIN,
            'Finite carrier lacks its exact bounded pre-offer frame/duck declaration')
    samples = np.frombuffer(data, dtype='<i2').reshape(-1, 2)
    require(np.array_equal(samples[:, 0], samples[:, 1]), 'Finite pre-offer carrier changed its declared mono channels')
    design = basis(0, BASELINE_FRAMES)
    coefficients = np.linalg.pinv(design) @ samples[:, 0].astype(float)
    residual = float(np.sqrt(np.mean((samples[:, 0]-design @ coefficients)**2)))
    amplitude = float(np.hypot(*coefficients))
    require(8190 <= amplitude <= 8193 and residual <= 4.,
            'Finite pre-offer carrier is not the declared full-volume stationary440Hz music')
    return Carrier(first_frame, tuple(float(value) for value in coefficients), hashlib.sha256(data).hexdigest(), residual)


def verify_quiet(samples, carrier, *, first_frame):
    require(type(carrier) is Carrier and type(first_frame) is int
            and carrier.first_frame+BASELINE_FRAMES <= first_frame < 2**53
            and isinstance(samples, np.ndarray) and samples.dtype == np.dtype('<i2')
            and samples.ndim == 2 and samples.shape[1] == 2
            and QUIET_FRAMES <= len(samples) <= MAX_FRAMES,
            'Finite quiet lost its bounded independent carrier/frame contract')
    require(np.array_equal(samples[:, 0], samples[:, 1]), 'Finite quiet changed its declared mono channels')
    observed = samples[:, 0].astype(float)
    carrier_wave = basis(first_frame-carrier.first_frame, len(samples)) @ np.asarray(carrier.coefficients)
    known, actual = carrier_wave, observed
    residual = actual-WIRE_DUCK*known
    fit_frames = len(samples)
    positions = np.arange(fit_frames, dtype=float)+1
    # All candidate SSEs come from bounded prefix sums, not an onset x PCM
    # matrix. Use the same already-collected continuation to distinguish an
    # onset after quiet; a constant quiet alone cannot identify that onset.
    # Phase, amplitude, slope, duck and voice mapping remain frozen.
    def sums(values):
        return np.r_[0., np.cumsum(values)]
    rc, jrc = sums(residual*known), sums(positions*residual*known)
    c2 = known*known
    cc, jcc, jjcc = sums(c2), sums(positions*c2), sums(positions*positions*c2)
    onsets = np.arange(-RESTORE_FRAMES, RESTORE_FRAMES+1)
    low = np.clip(onsets, 0, fit_frames)
    high = np.clip(np.ceil(onsets+(1-WIRE_DUCK)*RESTORE_FRAMES).astype(int)-1, low, fit_frames)
    cross = (jrc[high]-jrc[low]-onsets*(rc[high]-rc[low]))/RESTORE_FRAMES
    cross += (1-WIRE_DUCK)*(rc[-1]-rc[high])
    power = (jjcc[high]-jjcc[low]-2*onsets*(jcc[high]-jcc[low])+onsets*onsets*(cc[high]-cc[low]))/RESTORE_FRAMES**2
    power += (1-WIRE_DUCK)**2*(cc[-1]-cc[high])
    errors = float(np.sum(residual*residual))-2*cross+power
    onset = int(onsets[int(np.argmin(errors))])
    gain = np.minimum(1., WIRE_DUCK+np.maximum(0, np.arange(len(samples))+1-onset)/RESTORE_FRAMES)
    require(np.all((WIRE_DUCK <= gain) & (gain <= 1.)), 'Finite restoration model exceeds its declared music gain')
    unexplained = observed-carrier_wave*gain
    quiet_rms = float(np.sqrt(np.mean(unexplained[:QUIET_FRAMES]**2)))
    require(quiet_rms <= 4., 'Finite utterance retained stale speech after its exact decoded tail')
    # Any already-collected continuation uses the same phase/onset/slope. A
    # late ghost cannot be hidden by re-fitting each20ms block or its endpoint.
    continuation_max = 0.
    for at in range(QUIET_FRAMES, len(unexplained), 960):
        value = float(np.sqrt(np.mean(unexplained[at:at+960]**2)))
        continuation_max = max(continuation_max, value)
        require(value <= 4., 'Finite restored-music continuation contains unexplained output')
    return {'quiet_carrier_model': carrier.evidence(), 'restore_onset_quiet_frame': onset,
            'quiet_residual_rms': quiet_rms, 'quiet_model_first_capture_frame': first_frame,
            'continuation_verified_frames': len(samples)-QUIET_FRAMES,
            'continuation_max_residual_rms': continuation_max,
            'restored_gain1_verified_frames': int(np.count_nonzero(gain == 1.))}
