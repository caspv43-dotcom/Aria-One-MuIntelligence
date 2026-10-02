"""Turn a music track into an edit decision list (the shot list).

This is the "editor brain" of mvfx: given the beats of a song and a pile of
clips it decides *when to cut*, *what to show* and *what effect to use*, using
the rules real music videos follow:

* cut on the beat - short shots (1-2 beats) when the track is loud, longer
  ones (4-8 beats) when it breathes
* never repeat the same clip back to back
* land a flash / hard cut on the downbeat of a bar
* sprinkle in slow motion, reverses, freezes and stutters as accents rather
  than constantly, so the accents stay accents
* keep one consistent look (grade) across the whole piece

Everything is seeded, so the same command always produces the same edit.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .beats import Track
from .effects import Applied, Ctx, P, parse_effects
from .ffmpeg import MediaInfo

__all__ = ["Shot", "Timeline", "plan", "expected_out_dur"]


@dataclass
class Shot:
    """One cut in the timeline."""

    src: int  # index into the source list
    src_start: float  # where to start reading the source (seconds)
    src_dur: float  # how much source time to read (seconds)
    out_dur: float  # how long the shot lasts on the timeline (seconds)
    effects: List[Applied] = field(default_factory=list)
    start: float = 0.0  # timeline position (filled in by :func:`plan`)
    label: str = ""

    @property
    def end(self) -> float:
        return self.start + self.out_dur

    def describe(self) -> str:
        fx = ", ".join(a.name for a in self.effects) or "-"
        return (
            f"{self.start:7.2f}s ->{self.end:7.2f}s  src#{self.src} "
            f"[{self.src_start:6.2f}+{self.src_dur:5.2f}s]  {fx}"
        )


@dataclass
class Timeline:
    """A complete shot list plus the conform settings it was planned for."""

    shots: List[Shot]
    sources: List[MediaInfo]
    w: int = 1920
    h: int = 1080
    fps: float = 30.0
    transition: str = "cut"
    transition_dur: float = 0.0
    duration: float = 0.0

    def __len__(self) -> int:
        return len(self.shots)

    @property
    def shot_count(self) -> int:
        return len(self.shots)


# Effects that change how long a shot ends up on the timeline.
_DURATION_EFFECTS = {
    "slowmo": lambda p: 1.0 / max(0.05, p.f("factor", 0.5)),
    "stutter": lambda p: 1.0,
    "freeze": lambda p: 1.0,
    "speed_ramp": lambda p: 1.0,  # normalised to keep the shot length
}


def expected_out_dur(effects: Sequence[Applied], src_dur: float) -> float:
    """Timeline length of a shot given the speed effects applied to it."""
    factor = 1.0
    extra = 0.0
    for applied in effects:
        if applied.name in _DURATION_EFFECTS:
            factor *= _DURATION_EFFECTS[applied.name](applied.params)
        if applied.name == "freeze":
            extra += applied.params.f("duration", 0.35)
    return src_dur * factor + extra


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def _pick_motion(rng: random.Random, ctx: Ctx, shot_index: int, intensity: float) -> List[Applied]:
    """Choose the motion treatment for one shot."""
    from .effects import get_effect

    def fx(name: str, **params) -> Applied:
        return get_effect(name).with_params(**params)

    roll = rng.random()
    effects: List[Applied] = []

    # base movement - almost every music-video shot moves a little
    if roll < 0.42:
        effects.append(
            fx("zoom_punch", amount=round(rng.uniform(0.06, 0.10 + 0.10 * intensity), 3),
               mode=rng.choice(["in", "out"]))
        )
    elif roll < 0.62:
        effects.append(fx("drift", zoom=round(rng.uniform(0.05, 0.12), 3),
                          panx=round(rng.uniform(-0.03, 0.03), 3),
                          pany=round(rng.uniform(-0.02, 0.02), 3)))
    elif roll < 0.70:
        effects.append(fx("zoom_pulse", amount=round(rng.uniform(0.05, 0.10), 3),
                          period=round(60.0 / max(60.0, ctx.fps and 120.0), 3)))
    elif roll < 0.78:
        effects.append(fx("shake", amp=round(rng.uniform(4, 9 + 6 * intensity), 2),
                          freq=round(rng.uniform(2.0, 4.5), 2)))

    # accents
    if rng.random() < 0.10 * (0.5 + intensity):
        factor = round(rng.uniform(0.35, 0.65), 3)
        effects.append(fx("slowmo", factor=factor))
    if rng.random() < 0.07:
        effects.append(fx("reverse"))
    if rng.random() < 0.07 * (0.5 + intensity):
        effects.append(fx("stutter", rate=rng.choice([4.0, 5.0, 6.0, 8.0])))
    if rng.random() < 0.06:
        effects.append(fx("speed_ramp", start=round(rng.uniform(0.25, 0.5), 3),
                          end=round(rng.uniform(1.3, 2.0), 3)))
    return effects


def plan(
    track: Track,
    sources: Sequence[MediaInfo],
    *,
    w: int = 1280,
    h: int = 720,
    fps: float = 30.0,
    start: float = 0.0,
    duration: Optional[float] = None,
    intensity: float = 0.7,
    seed: int = 1,
    look: Sequence[Applied] = (),
    transition: str = "cut",
    transition_dur: float = 0.0,
    min_shot_beats: int = 1,
    max_shot_beats: int = 8,
    flash_on_downbeat: bool = True,
) -> Timeline:
    """Build a :class:`Timeline` from a track and a set of source clips."""
    if not sources:
        raise ValueError("plan() needs at least one source clip")

    end = track.duration if duration is None else min(start + duration, track.duration)
    if not track.beat_period:
        track.beat_period = 0.5
        track.offset = 0.0
        track.beats = [i * 0.5 for i in range(int(end * 2))]

    rng = random.Random(seed)
    ctx = Ctx(w=w, h=h, fps=fps)

    shots: List[Shot] = []
    t = track.quantize(max(0.0, start)) or max(0.0, start)
    previous_src = -1
    beat_period = track.beat_period

    while t < end - 0.05:
        energy = track.energy_at(t)
        # loud sections cut fast, quiet sections breathe
        if energy > 0.78:
            beats = 1 if rng.random() < 0.35 else 2
        elif energy > 0.55:
            beats = 2 if rng.random() < 0.7 else 4
        elif energy > 0.3:
            beats = 4
        else:
            beats = 8
        beats = max(min_shot_beats, min(max_shot_beats, beats))
        out_dur = beats * beat_period
        if t + out_dur > end:
            out_dur = end - t
        if out_dur < 0.12:
            break

        # pick a source, never the same one twice in a row
        choices = [i for i in range(len(sources)) if i != previous_src] or list(range(len(sources)))
        src_index = rng.choice(choices)
        previous_src = src_index
        src = sources[src_index]

        effects = _pick_motion(rng, ctx, len(shots), intensity)

        # how much source time we need to cover out_dur given speed effects
        speed_factor = expected_out_dur(effects, 1.0)
        need = out_dur / max(0.05, speed_factor)

        available = src.duration or 10.0
        if need > available * 0.98:
            # clip is too short: slow it down so it still fills the shot
            fill = available * 0.98
            factor = max(0.2, fill / need)
            effects = [e for e in effects if e.name != "slowmo"]
            effects.insert(0, parse_effects([f"slowmo(factor={factor:.3f})"])[0])
            speed_factor = expected_out_dur(effects, 1.0)
            need = out_dur / max(0.05, speed_factor)

        src_start = rng.uniform(0, max(0.0, available - need - 0.02))

        if flash_on_downbeat and abs((t - track.offset) % (beat_period * 4)) < beat_period * 0.5:
            effects.append(parse_effects([f"flash(duration=0.07:strength=0.85:at=0)"])[0])

        if rng.random() < 0.05:
            effects.append(parse_effects(["freeze(duration=0.3:where=end)"])[0])

        shots.append(
            Shot(
                src=src_index,
                src_start=round(src_start, 3),
                src_dur=round(need, 3),
                out_dur=round(expected_out_dur(effects, need), 3),
                effects=effects,
                start=round(t, 3),
            )
        )
        t += shots[-1].out_dur

    timeline = Timeline(
        shots=shots,
        sources=list(sources),
        w=w,
        h=h,
        fps=fps,
        transition=transition,
        transition_dur=transition_dur,
        duration=sum(s.out_dur for s in shots)
        - (transition_dur * (len(shots) - 1) if transition != "cut" else 0.0),
    )
    return timeline


# --------------------------------------------------------------------------- #
# Simple (non-beat) timelines
# --------------------------------------------------------------------------- #
def single_shot(
    info: MediaInfo,
    *,
    w: int,
    h: int,
    fps: float,
    effects: Sequence[Applied] = (),
    start: float = 0.0,
    duration: Optional[float] = None,
) -> Timeline:
    """A one-shot timeline: used by ``mvfx apply``."""
    total = info.duration or 0.0
    src_start = min(start, max(0.0, total - 0.05))
    src_dur = (duration if duration else total - src_start) or total
    src_dur = max(0.05, min(src_dur, max(0.0, total - src_start)))
    shot = Shot(
        src=0,
        src_start=src_start,
        src_dur=src_dur,
        out_dur=expected_out_dur(effects, src_dur),
        effects=list(effects),
        start=0.0,
    )
    return Timeline(
        shots=[shot],
        sources=[info],
        w=w,
        h=h,
        fps=fps,
        duration=shot.out_dur,
    )
