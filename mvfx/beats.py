"""Beat, onset and energy analysis - pure standard library.

Music videos are cut on the beat, so mvfx needs to know where the beats are.
This module gets them by:

1. splitting the track into three frequency bands with ffmpeg
   (kick < 160 Hz, body 200-2000 Hz, sparkle > 2500 Hz)
2. building a short-time energy envelope for each band (~100 Hz)
3. taking a log-compressed positive difference as the "novelty" function
4. peak-picking onsets against a local adaptive threshold
5. autocorrelating the novelty function to estimate tempo, then fitting a
   beat grid by searching for the phase that lines up best with the onsets

No numpy/scipy/librosa: everything runs on ``array``/``memoryview`` so mvfx
stays install-free.
"""

from __future__ import annotations

import array
import json
import hashlib
import math
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .ffmpeg import ensure_ffmpeg, probe

__all__ = ["Track", "analyze", "load_cache", "save_cache"]

#: Envelope resolution (frames per second) used for onset detection.
ENV_RATE = 100.0
#: Energy curve resolution used for edit pacing decisions.
ENERGY_RATE = 8.0

_BANDS = (
    # name, ffmpeg audio filter, target sample rate, weight
    ("kick", "lowpass=f=160", 1000, 1.0),
    ("body", "highpass=f=200,lowpass=f=2000", 4000, 0.75),
    ("sparkle", "highpass=f=2500", 8000, 0.5),
)


@dataclass
class Track:
    """Analysis result for one piece of music."""

    path: str = ""
    duration: float = 0.0
    bpm: float = 0.0
    beat_period: float = 0.0
    offset: float = 0.0
    beats: List[float] = field(default_factory=list)
    onsets: List[float] = field(default_factory=list)
    energy: List[float] = field(default_factory=list)
    energy_rate: float = ENERGY_RATE

    # ---------------------------------------------------------------- #
    def bar(self, index: int) -> float:
        """Time of bar ``index`` (4/4 assumed)."""
        return self.offset + index * self.beat_period * 4

    @property
    def bars(self) -> List[float]:
        out = []
        i = 0
        while True:
            t = self.bar(i)
            if t > self.duration:
                break
            out.append(t)
            i += 1
        return out

    def beat_index(self, t: float) -> int:
        return int(round((t - self.offset) / self.beat_period)) if self.beat_period else 0

    def energy_at(self, t: float) -> float:
        """Overall loudness (0..1) near time ``t``."""
        if not self.energy:
            return 0.5
        idx = int(t * self.energy_rate)
        idx = max(0, min(len(self.energy) - 1, idx))
        window = self.energy[max(0, idx - 2) : idx + 3]
        return sum(window) / len(window)

    def quantize(self, t: float, division: float = 1.0) -> float:
        """Snap ``t`` to the nearest beat (or ``division`` of a beat)."""
        if not self.beat_period:
            return t
        step = self.beat_period * division
        k = round((t - self.offset) / step)
        snapped = self.offset + k * step
        return max(0.0, snapped)

    def beats_in(self, start: float, end: float) -> List[float]:
        return [b for b in self.beats if start <= b < end]

    def to_json(self) -> str:
        return json.dumps(
            {
                "path": self.path,
                "duration": self.duration,
                "bpm": self.bpm,
                "beat_period": self.beat_period,
                "offset": self.offset,
                "beats": self.beats,
                "onsets": self.onsets,
                "energy": self.energy,
                "energy_rate": self.energy_rate,
            },
            indent=1,
        )

    @classmethod
    def from_json(cls, raw: str) -> "Track":
        data = json.loads(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
def _decode_band(path: str, afilter: str, rate: int, ffmpeg: str | None) -> array.array:
    """Decode one band-filtered mono channel to signed 16-bit samples."""
    binary = ensure_ffmpeg(ffmpeg)
    cmd = [
        binary,
        "-v",
        "error",
        "-i",
        path,
        "-map",
        "0:a:0",
        "-af",
        f"{afilter},aresample={rate}:resampler=soxr",
        "-ac",
        "1",
        "-ar",
        str(rate),
        "-f",
        "s16le",
        "-c:a",
        "pcm_s16le",
        "-",
    ]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
    if out.returncode != 0:
        raise RuntimeError(
            f"could not decode audio for analysis:\n{out.stderr.decode('utf-8', 'replace')[-800:]}"
        )
    samples = array.array("h")
    samples.frombytes(out.stdout[: len(out.stdout) // 2 * 2])
    return samples


def _envelope(samples: array.array, rate: int, env_rate: float) -> List[float]:
    """Short-time mean-absolute amplitude."""
    hop = max(1, int(round(rate / env_rate)))
    mv = memoryview(samples)
    n = len(mv)
    env: List[float] = []
    append = env.append
    for i in range(0, n - hop + 1, hop):
        total = 0
        for value in mv[i : i + hop]:
            total += value if value >= 0 else -value
        append(total / hop)
    return env


def _novelty(env: Sequence[float]) -> List[float]:
    """Log-compressed positive difference of an energy envelope."""
    if not env:
        return []
    mean = sum(env) / len(env) or 1.0
    norm = [e / mean for e in env]
    out = [0.0] * len(norm)
    prev_log = math.log1p(3.0 * norm[0])
    for i in range(1, len(norm)):
        cur = math.log1p(3.0 * norm[i])
        diff = cur - prev_log
        out[i] = diff if diff > 0 else 0.0
        prev_log = cur
    return out


def _local_mean(values: Sequence[float], radius: int) -> List[float]:
    """Centred moving average via prefix sums."""
    n = len(values)
    if n == 0:
        return []
    prefix = [0.0] * (n + 1)
    for i, v in enumerate(values):
        prefix[i + 1] = prefix[i] + v
    out = [0.0] * n
    for i in range(n):
        lo = max(0, i - radius)
        hi = min(n, i + radius + 1)
        out[i] = (prefix[hi] - prefix[lo]) / (hi - lo)
    return out


def _pick_peaks(
    novelty: Sequence[float],
    *,
    rate: float,
    sensitivity: float,
    min_gap: float,
) -> List[float]:
    """Peak-pick onsets against a local adaptive threshold."""
    if not novelty:
        return []
    global_mean = sum(novelty) / len(novelty) or 1e-9
    radius = int(0.35 * rate)
    local = _local_mean(novelty, radius)
    min_gap_frames = max(1, int(min_gap * rate))
    peaks: List[float] = []
    last = -10**9
    for i in range(1, len(novelty) - 1):
        value = novelty[i]
        if value < novelty[i - 1] or value < novelty[i + 1]:
            continue
        threshold = max(local[i] * sensitivity, global_mean * 0.6)
        if value <= threshold:
            continue
        if i - last < min_gap_frames:
            if peaks and value > novelty[last]:
                peaks[-1] = i / rate
                last = i
            continue
        peaks.append(i / rate)
        last = i
    return peaks


def _autocorr(values: Sequence[float], lag: int) -> float:
    n = len(values)
    if lag <= 0 or lag >= n:
        return 0.0
    total = 0.0
    count = n - lag
    for i in range(count):
        total += values[i] * values[i + lag]
    return total / max(1, count)


def estimate_tempo(
    novelty: Sequence[float],
    rate: float = ENV_RATE,
    min_bpm: float = 60.0,
    max_bpm: float = 200.0,
) -> tuple[float, float]:
    """Return ``(bpm, confidence)`` from a novelty function."""
    if not novelty:
        return 0.0, 0.0
    mean = sum(novelty) / len(novelty)
    centred = [v - mean for v in novelty]

    min_lag = max(2, int(rate * 60.0 / max_bpm))
    max_lag = min(len(centred) - 2, int(rate * 60.0 / min_bpm))
    best_bpm, best_score = 0.0, 0.0
    scores: Dict[int, float] = {}
    for lag in range(min_lag, max_lag + 1):
        score = _autocorr(centred, lag)
        score += 0.5 * _autocorr(centred, lag * 2)
        score += 0.25 * _autocorr(centred, lag * 3)
        score += 0.5 * _autocorr(centred, int(round(lag / 2)))
        scores[lag] = score
        bpm = 60.0 * rate / lag
        # gentle tempo prior: most popular music sits near 90-150 BPM
        if 90.0 <= bpm <= 150.0:
            score *= 1.15
        elif 70.0 <= bpm < 90.0 or 150.0 < bpm <= 180.0:
            score *= 1.0
        else:
            score *= 0.85
        if score > best_score:
            best_score, best_bpm, best_lag = score, bpm, lag

    if not scores:
        return 0.0, 0.0
    peak = max(scores.values())
    confidence = 0.0 if peak <= 0 else min(1.0, (best_score / max(peak, 1e-9)))
    return best_bpm, confidence


def fit_grid(
    novelty: Sequence[float],
    bpm: float,
    rate: float = ENV_RATE,
) -> tuple[float, float, float]:
    """Fit an even beat grid: return ``(refined_bpm, period, offset_seconds)``."""
    if bpm <= 0:
        return 0.0, 0.0, 0.0
    period = 60.0 / bpm
    best_offset, best_score = 0.0, -1.0
    step = 0.01  # seconds
    n_frames = len(novelty)
    span = period * 4  # search one bar of phase, score over the whole track
    offset = 0.0
    while offset < period:
        score = 0.0
        t = offset
        while t < n_frames / rate:
            idx = int(round(t * rate))
            for j in range(max(0, idx - 1), min(n_frames, idx + 2)):
                score += novelty[j]
            t += period
        if score > best_score:
            best_score, best_offset = score, offset
        offset += step

    # refine the period around the estimate (+-6%) holding the phase
    best_period = period
    best_score = -1.0
    factor = 0.94
    while factor <= 1.06001:
        p = period * factor
        score = 0.0
        t = best_offset
        while t < n_frames / rate:
            idx = int(round(t * rate))
            if 0 <= idx < n_frames:
                score += novelty[idx]
            t += p
        if score > best_score:
            best_score, best_period = score, p
        factor += 0.0025
    return 60.0 / best_period, best_period, best_offset


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def analyze(
    path: str | Path,
    *,
    ffmpeg: str | None = None,
    sensitivity: float = 1.5,
    min_bpm: float = 60.0,
    max_bpm: float = 200.0,
    progress: bool = False,
) -> Track:
    """Analyze ``path`` and return a :class:`Track`.

    Args:
        path: audio (or video) file to analyse.
        sensitivity: higher = fewer, stronger onsets (1.0-3.0).
        min_bpm / max_bpm: tempo search range.
    """
    path = str(path)
    info = probe(path, ffmpeg)
    track = Track(path=path, duration=info.duration)

    novelty: List[float] = []
    for name, afilter, rate, weight in _BANDS:
        if progress:
            print(f"  analysing {name} band …", flush=True)
        samples = _decode_band(path, afilter, rate, ffmpeg)
        if not samples:
            continue
        env = _envelope(samples, rate, ENV_RATE)
        band_nov = _novelty(env)
        if not novelty:
            novelty = [0.0] * len(band_nov)
        length = min(len(novelty), len(band_nov))
        for i in range(length):
            novelty[i] += weight * band_nov[i]

        if name == "body":  # broad-band loudness curve for edit pacing
            hop = max(1, int(round(ENV_RATE / ENERGY_RATE)))
            raw = []
            for i in range(0, len(env), hop):
                chunk = env[i : i + hop]
                raw.append(sum(chunk) / len(chunk))
            peak = max(raw) or 1.0
            track.energy = [min(1.0, v / peak) for v in raw]

    if not novelty:
        return track

    track.onsets = _pick_peaks(novelty, rate=ENV_RATE, sensitivity=sensitivity, min_gap=0.08)
    bpm, confidence = estimate_tempo(novelty, min_bpm=min_bpm, max_bpm=max_bpm)
    bpm, period, offset = fit_grid(novelty, bpm)
    track.bpm = round(bpm, 2)
    track.beat_period = period
    track.offset = offset
    if period > 0:
        track.beats = []
        t = offset
        while t < track.duration:
            if t >= 0:
                track.beats.append(round(t, 4))
            t += period
    return track


def cache_path(audio: str | Path, cache_dir: str | Path = ".mvfx") -> Path:
    # NOTE: hash() is salted per process, so it would produce a different name
    # on every run - use a stable digest instead.
    path = Path(audio)
    try:
        stat = path.stat()
        payload = f"{path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
    except OSError:
        payload = str(path.resolve())
    digest = hashlib.md5(payload.encode("utf-8")).hexdigest()[:12]
    return Path(cache_dir) / f"{path.stem}.{digest}.beats.json"


def save_cache(track: Track, cache_dir: str | Path = ".mvfx") -> Path:
    path = cache_path(track.path, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(track.to_json())
    return path


def load_cache(
    audio: str | Path, cache_dir: str | Path = ".mvfx"
) -> Optional[Track]:
    path = cache_path(audio, cache_dir)
    if path.is_file():
        try:
            return Track.from_json(path.read_text())
        except (json.JSONDecodeError, TypeError, KeyError):
            return None
    return None
