"""AutoEQ measurement pipeline for the SUB3030's built-in room correction.

How AutoEQ works: the subwoofer plays a test signal from its own generator, a
microphone measures it, and the controller sends 16 band levels to the subwoofer.
The subwoofer firmware computes and applies the correction itself.

Device side (command ids):
  0174 type 1 [00, step]          start the generator (step 0 = near field, 1 = far field)
  0176 notification [.., step]    "generator status updated" -> app queries 0177
  0177 type 0 [00, step] -> reply [00, step, state]; state 0 = off, 1 = playing, 2 = done
  0175 type 1 (no payload)        stop / reset the generator
  0048 type 1 [00, step] + 16 x int16 BE (dB x 10)   upload the measurement
  0048 reply: 16 x int16 BE (dB x 10) at payload offset 2   correction per band
                                  (SUB3030 fw v1.1.956.23 instead: short ack [00 01], then 0170 = 1
                                   announced ~3 s later; the correction stays inside the subwoofer)
  0170 = calibrated flag, 0172 = AutoEQ on/off (type 1, 0/1; app allows it only when calibrated)

Measurement side (chosen so the subwoofer gets the band levels it expects):
  * microphone at 8000 Hz, 16-bit mono, no automatic gain control
  * every sample through a 2nd-order Butterworth low-pass at 500 Hz (RBJ biquad, fs 8000)
  * a rolling 1 s buffer (8000 samples), analysed every 400 new samples once full
  * decimated to 1024 points by linear interpolation -> FFT bins are exactly 1 Hz apart
  * Blackman window, 1024-point FFT, level = 20*log10(|X| / 512)
  * max-hold per bin while the generator is playing
  * bands: the max-hold value at 16 frequencies (BANDS_HZ), sent as int16(dB * 10)

This module is pure Python so it needs no extra dependency. The browser GUI contains a
JavaScript copy of the same pipeline; tests check that both give the same numbers.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

APP_RATE = 8000            # Hz, analysis sample rate
BUFFER = 8000              # samples (1 s) kept for each analysis
HOP = 400                  # samples between analyses
FFT_N = 1024               # FFT length after decimation -> 1 Hz per bin
LPF_HZ = 500.0
LPF_Q = 1.0 / math.sqrt(2.0)
FLOOR_DB = -1000.0         # initial max-hold value
BANDS_HZ = (20, 23, 26, 30, 34, 39, 45, 52, 60, 69, 79, 91, 105, 121, 139, 160)
STEP_NEAR, STEP_FAR = 0, 1
GEN_OFF, GEN_PLAYING, GEN_DONE = 0, 1, 2


# ------------------------------------------------------------------ filters ----
def lpf_coefficients(fs: float = APP_RATE, fc: float = LPF_HZ, q: float = LPF_Q) -> tuple[float, float, float, float, float]:
    """RBJ low-pass, normalised by a0, order (b0, b1, b2, a1, a2)."""
    w0 = 2.0 * math.pi * fc / fs
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    a0 = 1.0 + alpha
    return ((1.0 - cos_w0) / 2.0 / a0, (1.0 - cos_w0) / a0, (1.0 - cos_w0) / 2.0 / a0,
            -2.0 * cos_w0 / a0, (1.0 - alpha) / a0)


class Biquad:
    """Direct form I."""

    def __init__(self, coeffs: tuple[float, float, float, float, float]):
        self.b0, self.b1, self.b2, self.a1, self.a2 = coeffs
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def __call__(self, x: float) -> float:
        y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2
        self.x2, self.x1 = self.x1, x
        self.y2, self.y1 = self.y1, y
        return y


# ---------------------------------------------------------------------- FFT ----
_WINDOW = [0.42 - 0.5 * math.cos(2 * math.pi * i / (FFT_N - 1)) + 0.08 * math.cos(4 * math.pi * i / (FFT_N - 1))
           for i in range(FFT_N)]
_TW_RE = [math.cos(-2 * math.pi * i / FFT_N) for i in range(FFT_N // 2)]
_TW_IM = [math.sin(-2 * math.pi * i / FFT_N) for i in range(FFT_N // 2)]


def _fft_in_place(re: list[float], im: list[float]) -> None:
    """Iterative radix-2 FFT."""
    n = len(re)
    bits = n.bit_length() - 1
    j = 0
    for i in range(1, n - 1):
        k = n // 2
        while j >= k:
            j -= k
            k //= 2
        j += k
        if i < j:
            re[i], re[j] = re[j], re[i]
            im[i], im[j] = im[j], im[i]
    size = 1
    for stage in range(bits):
        step = size * 2
        tw = 0
        for m in range(size):
            c, s = _TW_RE[tw], _TW_IM[tw]
            tw += 1 << (bits - stage - 1)
            for a in range(m, n, step):
                b = a + size
                tr = re[b] * c - im[b] * s
                ti = re[b] * s + im[b] * c
                re[b], im[b] = re[a] - tr, im[a] - ti
                re[a], im[a] = re[a] + tr, im[a] + ti
        size = step


def frame_spectrum(buffer: Sequence[float]) -> list[float]:
    """dB level per 1 Hz bin (0..511) for one 1 s buffer of 8 kHz samples scaled to [-1, 1]."""
    if len(buffer) != BUFFER:
        raise ValueError(f"buffer must hold {BUFFER} samples")
    ratio = APP_RATE / FFT_N
    re = [0.0] * FFT_N
    for k in range(FFT_N):
        pos = ratio * k
        i = int(pos)
        frac = pos - i
        re[k] = (buffer[i + 1] * frac + buffer[i] * (1.0 - frac)) * _WINDOW[k]
    im = [0.0] * FFT_N
    _fft_in_place(re, im)
    half = FFT_N // 2
    out = []
    for k in range(half):
        mag = math.sqrt(re[k] * re[k] + im[k] * im[k]) / half
        out.append(20.0 * math.log10(mag) if mag > 0 else FLOOR_DB)
    return out


# ----------------------------------------------------------------- analyser ----
class Analyzer:
    """Feed microphone samples (floats in [-1, 1]) at ``fs``; collects the max-hold spectrum.

    ``fs`` must be a whole multiple of 8000 Hz (8, 16, 32, 48, 96 kHz): the input is
    block-averaged down to 8 kHz, then runs the chain described above.
    """

    def __init__(self, fs: int = APP_RATE):
        if fs % APP_RATE:
            raise ValueError("sample rate must be a multiple of 8000 Hz")
        self.ratio = fs // APP_RATE
        self._acc = 0.0
        self._acc_n = 0
        self.lpf = Biquad(lpf_coefficients())
        self.buffer = [0.0] * BUFFER
        self.filled = 0
        self.since_frame = 0
        self.holding = True
        self.max_hold = [FLOOR_DB] * (FFT_N // 2)
        self.last = None
        self.frames = 0
        self.peak = 0.0

    def feed(self, samples: Iterable[float]) -> None:
        for s in samples:
            a = abs(s)
            if a > self.peak:
                self.peak = a
            self._acc += s
            self._acc_n += 1
            if self._acc_n < self.ratio:
                continue
            x = self._acc / self.ratio
            self._acc, self._acc_n = 0.0, 0
            self.buffer.append(self.lpf(x))
            del self.buffer[0]
            self.filled += 1
            self.since_frame += 1
            if self.since_frame >= HOP:
                self.since_frame = 0
                if self.filled > BUFFER:  # wait until the buffer has been filled once
                    self._analyse()

    def _analyse(self) -> None:
        spec = frame_spectrum(self.buffer)
        self.last = spec
        self.frames += 1
        if self.holding:
            for k, v in enumerate(spec):
                if v > self.max_hold[k]:
                    self.max_hold[k] = v

    def reset_hold(self) -> None:
        self.max_hold = [FLOOR_DB] * (FFT_N // 2)

    def bands(self) -> list[float]:
        return [self.max_hold[f] for f in BANDS_HZ]


# ------------------------------------------------------- repeated runs ----
# Not in the ELAC app: it measures each step once. The max-hold above keeps the loudest moment of a
# run, so one door slam or cough during the test signal becomes a fake peak. Measuring a step
# several times and combining the runs band by band removes that, and at the listening position
# (microphone moved a little between runs) it also averages over the seat instead of one point.
VALID_DB = -200.0          # band values at or below this mean "no data" (max-hold never updated)
OUTLIER_DB = {"same": 6.0, "moved": 10.0}   # a run this far from the band median is dropped
SPREAD_WARN_DB = {"same": 3.0, "moved": 8.0}  # kept runs differing more than this -> warn


def _median(values: Sequence[float]) -> float:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def combine_runs(runs: Sequence[Sequence[float]], mode: str = "same") -> dict:
    """Combine several band measurements of one step.

    ``mode`` "same": the microphone stayed put (near field) -> median per band.
    ``mode`` "moved": the microphone moved between runs (listening position) -> runs far from the
    band median are dropped as disturbances, the rest are averaged as power (10^(dB/10)).

    Returns ``levels`` (what to upload), ``spread`` (max - min of the runs kept, dB),
    ``dropped`` (runs dropped per band) and ``warn`` (indices of bands whose spread is too large).
    """
    if mode not in OUTLIER_DB:
        raise ValueError("mode must be 'same' or 'moved'")
    if not runs:
        raise ValueError("need at least one run")
    n_bands = len(runs[0])
    if any(len(r) != n_bands for r in runs):
        raise ValueError("all runs need the same number of bands")
    levels, spread, dropped, warn = [], [], [], []
    for b in range(n_bands):
        vals = [float(r[b]) for r in runs if float(r[b]) > VALID_DB]
        if not vals:
            levels.append(FLOOR_DB); spread.append(0.0); dropped.append(0)
            continue
        med = _median(vals)
        kept = [v for v in vals if abs(v - med) <= OUTLIER_DB[mode]] if len(vals) >= 3 else vals
        if mode == "same":
            level = _median(kept)
        else:
            level = 10.0 * math.log10(sum(10.0 ** (v / 10.0) for v in kept) / len(kept))
        sp = max(kept) - min(kept)
        levels.append(level); spread.append(sp); dropped.append(len(vals) - len(kept))
        if sp > SPREAD_WARN_DB[mode]:
            warn.append(b)
    return {"levels": levels, "spread": spread, "dropped": dropped, "warn": warn}


# ---------------------------------------------------------------- protocol ----
def _java_short(value: float) -> int:
    """(short)(double): truncate toward zero, saturate to int, then wrap to 16 bits."""
    if math.isnan(value):
        return 0
    i = int(value)  # truncates toward zero
    i = max(-(2 ** 31), min(2 ** 31 - 1, i))
    i &= 0xFFFF
    return i - 0x10000 if i >= 0x8000 else i


def encode_measurement(step: int, levels_db: Sequence[float]) -> bytes:
    """Payload for 0048 type 1: [00, step] + 16 x int16 BE of dB x 10."""
    if step not in (STEP_NEAR, STEP_FAR):
        raise ValueError("step must be 0 (near field) or 1 (far field)")
    if len(levels_db) != len(BANDS_HZ):
        raise ValueError(f"need {len(BANDS_HZ)} band levels")
    out = bytearray((0, step))
    for v in levels_db:
        out += _java_short(float(v) * 10.0).to_bytes(2, "big", signed=True)
    return bytes(out)


def decode_correction(payload: bytes) -> list[float]:
    """16 correction values (dB) from the subwoofer's 0048 reply (int16 BE x 10 at offset 2)."""
    need = 2 + 2 * len(BANDS_HZ)
    if len(payload) < need:
        raise ValueError(f"0048 reply too short: {len(payload)} bytes, need {need}")
    return [int.from_bytes(payload[2 + 2 * i: 4 + 2 * i], "big", signed=True) / 10.0 for i in range(len(BANDS_HZ))]


def generator_state(payload: bytes) -> int | None:
    """State byte from a 0177 reply [00, step, state]."""
    return payload[2] if len(payload) >= 3 else None
