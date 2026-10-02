"""The mvfx effect library.

Every effect is a small function that emits **ffmpeg filter-graph nodes**::

    def my_effect(ctx, p, inp, out) -> list[str]:
        return [f"[{inp}]somefilter=option=1[{out}]"]

* ``inp``/``out`` are filter-graph link labels supplied by the renderer
* ``p`` is a :class:`P` (dict + typed getters) holding user parameters
* ``ctx`` is a :class:`Ctx` describing the frame geometry and the shot

Effects are grouped into *stages* so that, whatever order the user lists them
in, the chain is built in a sensible order: geometry -> grade -> blends ->
artifacts -> light -> framing -> text.

That ordering is what makes presets such as ``cinematic`` or ``vhs_retro``
look right instead of, say, putting film grain *under* a bloom.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence

__all__ = [
    "P",
    "Ctx",
    "Effect",
    "EFFECTS",
    "PRESETS",
    "PRESET_HELP",
    "STAGE_NAMES",
    "get_effect",
    "parse_effects",
    "resolve_preset",
    "list_effects",
    "list_presets",
]

# --------------------------------------------------------------------------- #
# Stage constants (ordering of the filter chain)
# --------------------------------------------------------------------------- #
STAGE_GEOMETRY = 10  # crop / zoom / shake / speed / reverse
STAGE_GRADE = 20  # colour grading, curves, mono
STAGE_BLEND = 30  # double exposure, texture blends
STAGE_ARTIFACT = 40  # grain, scanlines, glitch, chroma
STAGE_LIGHT = 50  # bloom, leaks, flares, vignette
STAGE_FRAME = 60  # letterbox, flashes, fades
STAGE_TEXT = 70  # titles, lyrics

STAGE_NAMES = {
    STAGE_GEOMETRY: "geometry",
    STAGE_GRADE: "grade",
    STAGE_BLEND: "blend",
    STAGE_ARTIFACT: "artifact",
    STAGE_LIGHT: "light",
    STAGE_FRAME: "frame",
    STAGE_TEXT: "text",
}


class P(dict):
    """Parameter bag with typed getters (``p.f('x', 1.0)``)."""

    def f(self, key: str, default: float) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def i(self, key: str, default: int) -> int:
        try:
            return int(float(self.get(key, default)))
        except (TypeError, ValueError):
            return default

    def b(self, key: str, default: bool) -> bool:
        value = self.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def s(self, key: str, default: str) -> str:
        value = self.get(key, default)
        return default if value is None else str(value)


@dataclass
class Ctx:
    """Frame geometry + placement context handed to every effect."""

    w: int = 1920
    h: int = 1080
    fps: float = 30.0
    dur: float = 0.0  # length of the current shot, 0 when unknown
    shot: int = 0
    seed: int = 1
    #: prepended to every generated label so two shots never collide
    prefix: str = ""
    _uid: int = 0
    ffmpeg_filters: frozenset = field(default_factory=frozenset)

    def uid(self, tag: str = "x") -> str:
        """Return a unique filter-graph label fragment.

        Label names must be unique across the *whole* filter graph, so every
        shot gets its own ``prefix``.
        """
        self._uid += 1
        return f"{self.prefix}{tag}{self._uid}"

    @property
    def frames(self) -> int:
        """Number of frames in the current shot (at least 1s worth)."""
        if self.dur and self.fps:
            return max(1, int(round(self.dur * self.fps)))
        return max(1, int(round(self.fps)))

    def rng(self) -> random.Random:
        return random.Random(self.seed + self.shot * 7919)

    def has(self, name: str) -> bool:
        """True when the running ffmpeg build exposes filter ``name``."""
        return (not self.ffmpeg_filters) or (name in self.ffmpeg_filters)


EffectFn = Callable[[Ctx, "P", str, str], List[str]]


@dataclass
class Effect:
    """A named, parameterised video effect."""

    name: str
    fn: EffectFn
    stage: int
    params: Dict[str, Any]
    help: str
    tags: Sequence[str] = ()
    aliases: Sequence[str] = ()

    def with_params(self, **overrides) -> "Applied":
        params = dict(self.params)
        params.update(overrides)
        return Applied(effect=self, params=P(params))


@dataclass
class Applied:
    """An effect bound to concrete parameters."""

    effect: Effect
    params: P = field(default_factory=P)

    @property
    def name(self) -> str:
        return self.effect.name

    @property
    def stage(self) -> int:
        return self.effect.stage

    def build(self, ctx: Ctx, inp: str, out: str) -> List[str]:
        return self.effect.fn(ctx, self.params, inp, out)


#: Filters that the running ffmpeg build actually provides.  Populated once by
#: :func:`set_available_filters`; empty means "assume everything is available".
_FILTER_CACHE: set = set()


def set_available_filters(names) -> None:
    """Record which filters this ffmpeg build supports (for graceful skipping)."""
    _FILTER_CACHE.clear()
    _FILTER_CACHE.update(names)


EFFECTS: Dict[str, Effect] = {}


def effect(
    name: str,
    *,
    stage: int,
    help: str,
    tags: Sequence[str] = (),
    aliases: Sequence[str] = (),
    **defaults: Any,
) -> Callable[[EffectFn], EffectFn]:
    """Register a function as an effect with default parameters."""

    def deco(fn: EffectFn) -> EffectFn:
        if name in EFFECTS:
            raise ValueError(f"duplicate effect name: {name}")
        EFFECTS[name] = Effect(
            name=name,
            fn=fn,
            stage=stage,
            params=dict(defaults),
            help=help,
            tags=tuple(tags),
            aliases=tuple(aliases),
        )
        for alias in aliases:
            EFFECTS[alias] = EFFECTS[name]
        return fn

    return deco


def get_effect(name: str) -> Effect:
    try:
        return EFFECTS[name]
    except KeyError:
        close = [n for n in EFFECTS if n.startswith(name[:3])]
        hint = f" (did you mean: {', '.join(sorted(close)[:5])}?)" if close else ""
        raise KeyError(f"unknown effect '{name}'{hint}. Run `mvfx ls` to list them.") from None


# =========================================================================== #
# GEOMETRY / MOTION
# =========================================================================== #
@effect(
    "zoom_punch",
    stage=STAGE_GEOMETRY,
    tags=("motion", "beat", "classic"),
    help="Slow push-in (or pull-out) on the shot. The #1 music-video move.",
    amount=0.12,
    mode="in",  # in | out | in-out
    center="0.5:0.5",
)
def zoom_punch(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    amount = p.f("amount", 0.12)
    mode = p.s("mode", "in").lower()
    frames = ctx.frames
    if mode == "out":
        ramp = f"(1-on/{frames})"
    elif mode in {"in-out", "inout", "both"}:
        ramp = f"(1-abs(1-2*on/{frames}))"
    else:
        ramp = f"(on/{frames})"
    cx, cy = (p.s("center", "0.5:0.5").split(":") + ["0.5", "0.5"])[:2]
    z = f"1+{amount:.4f}*{ramp}"
    return [
        f"[{inp}]zoompan=z='{z}':x='iw*{cx}-(iw/zoom/2)':y='ih*{cy}-(ih/zoom/2)'"
        f":d=1:s={ctx.w}x{ctx.h}:fps={ctx.fps:g}[{out}]"
    ]


@effect(
    "zoom_pulse",
    stage=STAGE_GEOMETRY,
    tags=("motion", "beat"),
    help="Zoom that snaps in on every pulse - punchy, rhythmic zoom hits.",
    amount=0.09,
    period=0.5,
)
def zoom_pulse(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    amount = p.f("amount", 0.09)
    period = max(0.05, p.f("period", 0.5))
    cx, cy = (p.s("center", "0.5:0.5").split(":") + ["0.5", "0.5"])[:2]
    # Decaying saw: full at the start of each pulse, easing back out.
    phase = f"(mod(on/{ctx.fps:g}\\,{period:.4f})/{period:.4f})"
    z = f"1+{amount:.4f}*(1-{phase})"
    return [
        f"[{inp}]zoompan=z='{z}':x='iw*{cx}-(iw/zoom/2)':y='ih*{cy}-(ih/zoom/2)'"
        f":d=1:s={ctx.w}x{ctx.h}:fps={ctx.fps:g}[{out}]"
    ]


@effect(
    "shake",
    stage=STAGE_GEOMETRY,
    tags=("motion", "camera"),
    help="Handheld-style camera shake (offset + rotation wobble).",
    amp=8.0,
    freq=3.0,
    roll=0.5,
)
def shake(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    amp = min(p.f("amp", 8.0), 0.02 * ctx.w)
    freq = p.f("freq", 3.0)
    roll = p.f("roll", 0.5) * math.pi / 180.0
    zoom = 1.0 + 2.6 * amp / max(ctx.w, 1) + 0.02
    sw, sh = int(round(ctx.w * zoom)) // 2 * 2, int(round(ctx.h * zoom)) // 2 * 2
    x = f"(iw-{ctx.w})/2+{amp:.2f}*sin(2*PI*{freq:.3f}*t)"
    y = f"(ih-{ctx.h})/2+{amp * 0.72:.2f}*sin(2*PI*{freq * 0.77:.3f}*t+1.3)"
    chain = (
        f"[{inp}]scale={sw}:{sh}:flags=bicubic,"
        f"crop={ctx.w}:{ctx.h}:x='{x}':y='{y}'"
    )
    if roll:
        chain += f",rotate=a='{roll:.5f}*sin(2*PI*{freq * 0.53:.3f}*t)':ow={ctx.w}:oh={ctx.h}:c=black"
    chain += f",setsar=1[{out}]"
    return [chain]


@effect(
    "drift",
    stage=STAGE_GEOMETRY,
    tags=("motion",),
    help="Slow continuous Ken-Burns drift (pan + zoom) across the shot.",
    zoom=0.08,
    panx=0.02,
    pany=0.015,
)
def drift(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    amount = p.f("zoom", 0.08)
    frames = ctx.frames
    panx, pany = p.f("panx", 0.02), p.f("pany", 0.015)
    z = f"1+{amount:.4f}*(on/{frames})"
    x = f"(iw-iw/zoom)*(0.5+{panx:.4f}*(on/{frames}))"
    y = f"(ih-ih/zoom)*(0.5+{pany:.4f}*(on/{frames}))"
    return [
        f"[{inp}]zoompan=z='{z}':x='{x}':y='{y}'"
        f":d=1:s={ctx.w}x{ctx.h}:fps={ctx.fps:g}[{out}]"
    ]


@effect(
    "slowmo",
    stage=STAGE_GEOMETRY,
    tags=("motion", "classic", "speed"),
    help="Slow motion. factor<1 slows down (0.5 = half speed).",
    factor=0.5,
    smooth=False,
)
def slowmo(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    factor = max(0.05, p.f("factor", 0.5))
    chain = f"[{inp}]setpts={1.0 / factor:.6f}*PTS"
    if p.b("smooth", False):
        chain += f",minterpolate=fps={ctx.fps:g}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
    chain += f"[{out}]"
    return [chain]


@effect(
    "speed_ramp",
    stage=STAGE_GEOMETRY,
    tags=("motion", "speed", "classic"),
    help="Ramp from slow to fast (or back) across the shot, keeping its length.",
    start=0.35,
    end=1.6,
)
def speed_ramp(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    a = max(0.05, p.f("start", 0.35))
    b = max(0.05, p.f("end", 1.6))
    d = max(ctx.dur, 0.001)
    slope = (b - a) / d
    # output_pts(u) = k * (a*u + slope*u^2/2), k chosen so the shot keeps its length
    k = 2.0 / (a + b)
    expr = f"{k:.6f}*({a:.6f}*T+{slope / 2:.6f}*T*T)/TB"
    return [f"[{inp}]setpts='{expr}',fps={ctx.fps:g}[{out}]"]


@effect(
    "stutter",
    stage=STAGE_GEOMETRY,
    tags=("motion", "glitch", "beat"),
    help="Frame-repeat judder (holds one frame every 1/rate second).",
    rate=6.0,
)
def stutter(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    rate = max(1.0, p.f("rate", 6.0))
    return [
        f"[{inp}]setpts='trunc(T*{rate:.4f})/{rate:.4f}/TB',fps={ctx.fps:g}[{out}]"
    ]


@effect(
    "reverse",
    stage=STAGE_GEOMETRY,
    tags=("motion", "classic"),
    help="Play the shot backwards (the rewind cut).",
)
def reverse(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    return [f"[{inp}]reverse[{out}]"]


@effect(
    "freeze",
    stage=STAGE_GEOMETRY,
    tags=("motion", "beat"),
    help="Freeze the first (or last) frame for `duration` seconds.",
    duration=0.35,
    where="end",
)
def freeze(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    d = max(0.01, p.f("duration", 0.35))
    if p.s("where", "end").lower().startswith("start"):
        return [f"[{inp}]tpad=start_mode=clone:start_duration={d:.3f}[{out}]"]
    return [f"[{inp}]tpad=stop_mode=clone:stop_duration={d:.3f}[{out}]"]


@effect(
    "mirror",
    stage=STAGE_GEOMETRY,
    tags=("spatial", "classic"),
    help="Mirror one half of the frame into the other (symmetry shot).",
    axis="x",
)
def mirror(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    if p.s("axis", "x").lower().startswith("y"):
        a, b, c, d = ctx.uid("m"), ctx.uid("m"), ctx.uid("m"), ctx.uid("m")
        return [
            f"[{inp}]split=2[{a}][{b}]",
            f"[{b}]crop=iw:ih/2:0:0,vflip[{c}]",
            f"[{a}][{c}]vstack=inputs=2:shortest=1[{d}]",
            f"[{d}]crop=iw:{ctx.h}:0:ih-{ctx.h},setsar=1[{out}]",
        ]
    a, b, c, d = ctx.uid("m"), ctx.uid("m"), ctx.uid("m"), ctx.uid("m")
    return [
        f"[{inp}]split=2[{a}][{b}]",
        f"[{b}]crop=iw/2:ih:0:0,hflip[{c}]",
        f"[{a}][{c}]hstack=inputs=2:shortest=1[{d}]",
        f"[{d}]crop={ctx.w}:ih:iw-{ctx.w}:0,setsar=1[{out}]",
    ]


@effect(
    "kaleidoscope",
    stage=STAGE_GEOMETRY,
    tags=("spatial", "psychedelic"),
    help="Four-way kaleidoscope of the top-left quadrant.",
)
def kaleidoscope(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    q, a, b, c, d, o = (ctx.uid("k") for _ in range(6))
    top, bot = ctx.uid("kt"), ctx.uid("kb")
    return [
        f"[{inp}]crop=iw/2:ih/2:0:0[{q}]",
        f"[{q}]split=4[{a}][{b}][{c}][{d}]",
        f"[{b}]hflip[{b}h]",
        f"[{c}]vflip[{c}v]",
        f"[{d}]hflip,vflip[{d}hv]",
        f"[{a}][{b}h]hstack=inputs=2[{top}]",
        f"[{c}v][{d}hv]hstack=inputs=2[{bot}]",
        f"[{top}][{bot}]vstack=inputs=2[{o}]",
        f"[{o}]scale={ctx.w}:{ctx.h}:flags=bicubic,setsar=1[{out}]",
    ]


@effect(
    "spin",
    stage=STAGE_GEOMETRY,
    tags=("motion", "psychedelic"),
    help="Continuous slow rotation (deg/sec).",
    speed=6.0,
)
def spin(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    deg = p.f("speed", 6.0)
    rad = deg * math.pi / 180.0
    return [
        f"[{inp}]rotate=a='{rad:.6f}*t':ow={ctx.w}:oh={ctx.h}:c=black,setsar=1[{out}]"
    ]


# =========================================================================== #
# COLOUR GRADING
# =========================================================================== #
@effect(
    "teal_orange",
    stage=STAGE_GRADE,
    tags=("grade", "classic", "look"),
    aliases=("blockbuster_grade",),
    help="Teal shadows / orange skin tones - the Hollywood default.",
    intensity=0.8,
)
def teal_orange(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    k = max(0.0, p.f("intensity", 0.8))
    rs, bs = -0.05 * k, 0.10 * k
    rh, bh = 0.11 * k, -0.09 * k
    rm, bm = -0.01 * k, 0.02 * k
    return [
        f"[{inp}]colorbalance=rs={rs:.3f}:bs={bs:.3f}:rm={rm:.3f}:bm={bm:.3f}"
        f":rh={rh:.3f}:bh={bh:.3f},"
        f"eq=contrast={1 + 0.06 * k:.3f}:saturation={1 + 0.14 * k:.3f}[{out}]"
    ]


@effect(
    "cinematic_grade",
    stage=STAGE_GRADE,
    tags=("grade", "look"),
    help="Filmic curve: lifted blacks, rolled-off highlights, gentle contrast.",
    intensity=0.8,
)
def cinematic_grade(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    lift = 0.02 + 0.05 * max(0.0, p.f("intensity", 0.8))
    hi = 1.0 - 0.03 * max(0.0, p.f("intensity", 0.8))
    return [
        f"[{inp}]curves=r='0/{lift:.3f} 0.5/0.5 1/{hi:.3f}'"
        f":g='0/{lift * 0.85:.3f} 0.5/0.5 1/{hi:.3f}'"
        f":b='0/{lift * 1.15:.3f} 0.5/0.5 1/{hi - 0.02:.3f}',"
        f"eq=contrast=1.05:saturation=1.06[{out}]"
    ]


@effect(
    "neon_night",
    stage=STAGE_GRADE,
    tags=("grade", "look", "night"),
    help="Magenta/cyan neon night-club grade with crushed blacks.",
    intensity=0.9,
)
def neon_night(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    k = max(0.0, p.f("intensity", 0.9))
    return [
        f"[{inp}]colorbalance=rs={0.04 * k:.3f}:bs={0.13 * k:.3f}"
        f":rm={0.0:.3f}:bm={0.05 * k:.3f}:rh={0.10 * k:.3f}:bh={0.06 * k:.3f},"
        f"curves=b='0/0.04 0.5/0.46 1/1',"
        f"eq=contrast={1 + 0.18 * k:.3f}:saturation={1 + 0.35 * k:.3f}[{out}]"
    ]


@effect(
    "sunset_warm",
    stage=STAGE_GRADE,
    tags=("grade", "look", "warm"),
    help="Golden-hour warmth: amber highlights, cooler shadows.",
    intensity=0.9,
)
def sunset_warm(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    k = max(0.0, p.f("intensity", 0.9))
    return [
        f"[{inp}]colorbalance=rs={0.02 * k:.3f}:gs={0.01 * k:.3f}:bs={-0.06 * k:.3f}"
        f":rh={0.13 * k:.3f}:gh={0.03 * k:.3f}:bh={-0.10 * k:.3f},"
        f"eq=saturation={1 + 0.12 * k:.3f}:gamma={1 + 0.04 * k:.3f}[{out}]"
    ]


@effect(
    "mono",
    stage=STAGE_GRADE,
    tags=("grade", "look", "classic"),
    help="Black & white (desaturate) with an optional contrast bump.",
    contrast=1.12,
    warm=False,
)
def mono(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    contrast = p.f("contrast", 1.12)
    chain = f"[{inp}]hue=s=0"
    if p.b("warm", False):
        chain += ",colorbalance=rs=0.03:bh=-0.03"
    chain += f",eq=contrast={contrast:.3f}[{out}]"
    return [chain]


@effect(
    "bleach_bypass",
    stage=STAGE_GRADE,
    tags=("grade", "look", "classic"),
    help="Silver-retention look: desaturated copy screened back at high contrast.",
    amount=0.6,
)
def bleach_bypass(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    amount = max(0.0, min(1.0, p.f("amount", 0.6)))
    a, b, c = ctx.uid("bb"), ctx.uid("bb"), ctx.uid("bb")
    return [
        f"[{inp}]split=2[{a}][{b}]",
        f"[{b}]hue=s=0,eq=contrast=2.0[{c}]",
        f"[{a}][{c}]blend=all_mode=overlay:all_opacity={amount:.3f}[{out}]",
    ]


@effect(
    "contrast_punch",
    stage=STAGE_GRADE,
    tags=("grade",),
    help="Contrast + saturation punch.",
    contrast=1.15,
    saturation=1.1,
)
def contrast_punch(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    return [
        f"[{inp}]eq=contrast={p.f('contrast', 1.15):.3f}"
        f":saturation={p.f('saturation', 1.1):.3f}[{out}]"
    ]


@effect(
    "crush_blacks",
    stage=STAGE_GRADE,
    tags=("grade",),
    help="Crush the shadows and cap the highlights for a punchy, digital look.",
    amount=0.06,
)
def crush_blacks(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    lo = max(0.0, min(0.2, p.f("amount", 0.06)))
    hi = 1.0 - lo * 0.4
    return [
        f"[{inp}]colorlevels=rimin={lo:.3f}:gimin={lo:.3f}:bimin={lo:.3f}"
        f":rimax={hi:.3f}:gimax={hi:.3f}:bimax={hi:.3f}[{out}]"
    ]


@effect(
    "vibrance",
    stage=STAGE_GRADE,
    tags=("grade",),
    help="Smart saturation that protects skin tones.",
    amount=0.6,
)
def vibrance(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    return [f"[{inp}]vibrance=intensity={p.f('amount', 0.6):.3f}[{out}]"]


@effect(
    "curves_preset",
    stage=STAGE_GRADE,
    tags=("grade", "look"),
    help="Built-in ffmpeg curve preset: vintage, cross_process, color_negative, "
    "darker, increase_contrast, lighter, linear_contrast, medium_contrast, negative, "
    "strong_contrast, none.",
    preset="vintage",
    strength=1.0,
)
def curves_preset(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    preset = p.s("preset", "vintage")
    strength = max(0.0, min(1.0, p.f("strength", 1.0)))
    chain = f"[{inp}]curves=preset={preset}"
    if strength < 1.0:
        a, b, c = ctx.uid("cv"), ctx.uid("cv"), ctx.uid("cv")
        return [
            f"[{inp}]split=2[{a}][{b}]",
            f"[{b}]curves=preset={preset}[{c}]",
            f"[{a}][{c}]blend=all_mode=normal:all_opacity={strength:.3f}[{out}]",
        ]
    return [chain + f"[{out}]"]


@effect(
    "posterize",
    stage=STAGE_GRADE,
    tags=("grade", "stylised"),
    help="Reduce the image to N tonal steps per channel.",
    levels=6,
)
def posterize(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    levels = max(2, p.i("levels", 6))
    step = 255.0 / levels
    expr = f"floor(val/{step:.5f})*{step:.5f}"
    return [f"[{inp}]lut=r='{expr}':g='{expr}':b='{expr}'[{out}]"]


@effect(
    "invert",
    stage=STAGE_GRADE,
    tags=("grade", "stylised"),
    help="Negative image (great for one-frame flashes).",
)
def invert(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    return [f"[{inp}]negate[{out}]"]


@effect(
    "saturation_ramp",
    stage=STAGE_GRADE,
    tags=("grade", "motion"),
    help="Sweep saturation up (or down) over the shot.",
    start=0.0,
    end=1.4,
)
def saturation_ramp(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    a, b = p.f("start", 0.0), p.f("end", 1.4)
    d = max(ctx.dur, 0.001)
    # `hue=s=` is not an expression option, so crossfade between a flat copy and a
    # saturated copy with an all_expr blend driven by T.
    flat, sat, o = ctx.uid("sr"), ctx.uid("sr"), ctx.uid("sr")
    weight = f"min(T/{d:.4f}\\,1)"
    expr = f"A*(1-({weight}))+B*({weight})"
    return [
        f"[{inp}]split=2[{flat}][{sat}]",
        f"[{flat}]eq=saturation={a:.3f}[{flat}f]",
        f"[{sat}]eq=saturation={b:.3f}[{sat}s]",
        f"[{flat}f][{sat}s]blend=all_expr='{expr}'[{o}]",
        f"[{o}]null[{out}]",
    ]


# =========================================================================== #
# BLENDS / DOUBLE EXPOSURE
# =========================================================================== #
@effect(
    "double_exposure",
    stage=STAGE_BLEND,
    tags=("blend", "classic", "artistic"),
    help="Screen the mirrored frame over itself - the classic double exposure.",
    opacity=0.45,
    mode="screen",
    flip=True,
)
def double_exposure(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    opacity = max(0.0, min(1.0, p.f("opacity", 0.45)))
    mode = p.s("mode", "screen")
    a, b, c = ctx.uid("dx"), ctx.uid("dx"), ctx.uid("dx")
    flip = "hflip," if p.b("flip", True) else ""
    return [
        f"[{inp}]split=2[{a}][{b}]",
        f"[{b}]{flip}eq=saturation=1.2:contrast=1.05[{c}]",
        f"[{a}][{c}]blend=all_mode={mode}:all_opacity={opacity:.3f}[{out}]",
    ]


@effect(
    "ghost_trails",
    stage=STAGE_BLEND,
    tags=("temporal", "classic", "psychedelic"),
    help="Motion trails: each frame is a weighted echo of the previous ones.",
    frames=6,
    decay=0.65,
)
def ghost_trails(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    n = max(2, min(64, p.i("frames", 6)))
    decay = max(0.05, min(0.98, p.f("decay", 0.65)))
    weights = " ".join(f"{decay ** i:.4f}" for i in range(n))
    return [f"[{inp}]tmix=frames={n}:weights='{weights}'[{out}]"]


@effect(
    "motion_blur",
    stage=STAGE_BLEND,
    tags=("temporal",),
    help="Even average of N frames - cheap, convincing motion blur.",
    frames=5,
)
def motion_blur(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    n = max(2, min(1024, p.i("frames", 5)))
    return [f"[{inp}]tmix=frames={n}[{out}]"]


@effect(
    "light_trails",
    stage=STAGE_BLEND,
    tags=("temporal", "light", "night"),
    help="Screen-blend consecutive frames so highlights smear into streaks.",
    decay=0.02,
)
def light_trails(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    decay = p.f("decay", 0.02)
    return [
        f"[{inp}]tblend=all_mode=screen,eq=brightness={-decay:.3f}:contrast=1.03[{out}]"
    ]


# =========================================================================== #
# ARTIFACTS / GLITCH
# =========================================================================== #
@effect(
    "grain",
    stage=STAGE_ARTIFACT,
    tags=("artifact", "film", "classic"),
    help="Film grain (temporal + spatial noise).",
    level=8,
    temporal=True,
)
def grain(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    level = max(0, p.i("level", 8))
    flags = "t+u" if p.b("temporal", True) else "u"
    return [f"[{inp}]noise=alls={level}:allf={flags}[{out}]"]


@effect(
    "chromatic",
    stage=STAGE_ARTIFACT,
    tags=("artifact", "glitch", "lens"),
    aliases=("rgb_split",),
    help="Chromatic aberration / RGB split. Set pulse>0 to throb on the beat.",
    r=3,
    b=-3,
    pulse=0.0,
)
def chromatic(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    r, b = p.i("r", 3), p.i("b", -3)
    pulse = p.f("pulse", 0.0)
    enable = ""
    if pulse > 0:
        enable = f":enable='lt(mod(t\\,{pulse:.4f})\\,{max(0.02, pulse * 0.22):.4f})'"
    return [f"[{inp}]rgbashift=rh={r}:bh={b}{enable}[{out}]"]


@effect(
    "chroma_bleed",
    stage=STAGE_ARTIFACT,
    tags=("artifact", "retro", "vhs"),
    help="VHS chroma bleed: shift the colour channels horizontally.",
    shift=3,
)
def chroma_bleed(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    s = p.i("shift", 3)
    return [f"[{inp}]chromashift=cbh={-s}:crh={s}[{out}]"]


@effect(
    "scanlines",
    stage=STAGE_ARTIFACT,
    tags=("artifact", "retro", "crt"),
    help="CRT/VHS scanlines.",
    gap=2,
    strength=0.35,
)
def scanlines(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    gap = max(2, p.i("gap", 2))
    strength = max(0.0, min(1.0, p.f("strength", 0.35)))
    src, pat, o1, o2 = (ctx.uid("sl") for _ in range(4))
    return [
        f"color=c=black:s=2x{gap}:d=1:r={ctx.fps:g}[{src}]",
        f"[{src}]format=yuv420p,"
        f"geq=lum='if(lt(Y\\,1)\\,255\\,0)':cb=128:cr=128,"
        f"scale={ctx.w}:{ctx.h}:flags=neighbor,loop=loop=-1:size=1[{pat}]",
        f"[{inp}]format=yuv420p[{o1}]",
        f"[{o1}][{pat}]blend=all_mode=multiply:all_opacity={strength:.3f}:shortest=1[{o2}]",
        f"[{o2}]null[{out}]",
    ]


@effect(
    "glitch_blocks",
    stage=STAGE_ARTIFACT,
    tags=("artifact", "glitch", "digital"),
    help="Torn horizontal blocks that jump sideways for a few frames.",
    bands=5,
    amount=0.25,
    seed=1,
)
def glitch_blocks(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    n = max(1, min(24, p.i("bands", 5)))
    amount = max(0.0, min(1.0, p.f("amount", 0.25)))
    rng = random.Random(p.i("seed", 1) + ctx.shot)
    labels = [ctx.uid("gb") for _ in range(n + 1)]
    lines: List[str] = [f"[{inp}]split={n + 1}" + "".join(f"[{l}]" for l in labels)]
    cur = labels[0]
    band_h = max(6, ctx.h // (n * 2))
    for i in range(n):
        y = int((i + 0.5) * ctx.h / n) - band_h // 2
        y = max(0, min(ctx.h - band_h, y))
        dx = int(rng.uniform(0.15, 1.0) * amount * ctx.w)
        if dx == 0:
            dx = int(0.05 * ctx.w)
        start = round(rng.uniform(0.0, max(0.05, ctx.dur - 0.1)), 3)
        length = round(rng.uniform(0.05, 0.16), 3)
        band, nxt = labels[i + 1], f"gl{ctx.shot}_{i}"
        lines.append(f"[{band}]crop=iw:{band_h}:0:{y},setsar=1[{band}c]")
        lines.append(
            f"[{cur}][{band}c]overlay=x={dx}:y={y}"
            f":enable='between(t\\,{start:.3f}\\,{start + length:.3f})'[{nxt}]"
        )
        cur = nxt
    lines.append(f"[{cur}]null[{out}]")
    return lines


@effect(
    "strobe",
    stage=STAGE_ARTIFACT,
    tags=("artifact", "beat", "light", "flash"),
    help="Strobe: flash the frame colour `rate` times per second.",
    rate=8.0,
    duty=0.45,
    color="white",
    strength=0.75,
)
def strobe(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    rate = max(0.5, p.f("rate", 8.0))
    duty = max(0.02, min(0.98, p.f("duty", 0.45)))
    period = 1.0 / rate
    on = period * duty
    color = p.s("color", "white")
    strength = max(0.0, min(1.0, p.f("strength", 0.75)))
    return [
        f"[{inp}]drawbox=x=0:y=0:w=iw:h=ih:color={color}@{strength:.3f}:t=fill"
        f":enable='lt(mod(t\\,{period:.4f})\\,{on:.4f})'[{out}]"
    ]


@effect(
    "flash",
    stage=STAGE_FRAME,
    tags=("beat", "light", "flash", "classic"),
    help="Single frame flash to white (or any colour) at the start of the shot.",
    duration=0.09,
    color="white",
    strength=0.9,
    at=0.0,
)
def flash(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    d = max(0.01, p.f("duration", 0.09))
    at = max(0.0, p.f("at", 0.0))
    color = p.s("color", "white")
    strength = max(0.0, min(1.0, p.f("strength", 0.9)))
    return [
        f"[{inp}]drawbox=x=0:y=0:w=iw:h=ih:color={color}@{strength:.3f}:t=fill"
        f":enable='between(t\\,{at:.3f}\\,{at + d:.3f})'[{out}]"
    ]


@effect(
    "tracking_band",
    stage=STAGE_ARTIFACT,
    tags=("artifact", "retro", "vhs"),
    help="Rolling VHS tracking band with a torn, displaced strip.",
    speed=3.0,
    height=0.08,
    strength=0.5,
)
def tracking_band(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    speed = p.f("speed", 3.0)
    height = max(0.01, min(0.5, p.f("height", 0.08)))
    strength = max(0.0, min(1.0, p.f("strength", 0.5)))
    band_h = max(4, int(ctx.h * height)) // 2 * 2
    a, b, c, d = (ctx.uid("tb") for _ in range(4))
    # crop and overlay expose different variable names: crop uses ih, overlay
    # uses main_h - both are the height of the frame the filter is reading.
    # clamp so the crop window never falls off the bottom of the frame
    ypos_crop = f"min(mod(t*{speed:.4f}*ih\\,ih)\\,ih-{band_h})"
    ypos_over = f"min(mod(t*{speed:.4f}*main_h\\,main_h)\\,main_h-{band_h})"
    cycle = max(0.4, 4.0 / max(0.5, speed))
    duty = cycle * max(0.05, min(0.9, strength))
    return [
        f"[{inp}]split=2[{a}][{b}]",
        f"[{b}]crop=iw:{band_h}:0:'{ypos_crop}',setsar=1,"
        f"lutyuv='y=val*1.3:u=val*0.65:v=val*1.45'[{c}]",
        f"[{a}][{c}]overlay=x=0:y='{ypos_over}'"
        f":enable='lt(mod(t\\,{cycle:.3f})\\,{duty:.3f})'[{d}]",
        f"[{d}]null[{out}]",
    ]


@effect(
    "dust_scratches",
    stage=STAGE_ARTIFACT,
    tags=("artifact", "retro", "film"),
    help="Random specks of dust and the odd hairline scratch.",
    density=0.02,
)
def dust_scratches(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    density = max(0.0, min(0.5, p.f("density", 0.02)))
    return [
        f"[{inp}]noise=alls={max(1, int(60 * density))}:allf=u"
        f":enable='lt(random(1)\\,{min(0.9, density * 8):.3f})'[{out}]"
    ]


# =========================================================================== #
# LIGHT
# =========================================================================== #
@effect(
    "bloom",
    stage=STAGE_LIGHT,
    tags=("light", "classic", "dream"),
    help="Glow: blur the highlights and screen them back over the image.",
    sigma=16.0,
    threshold=0.5,
    opacity=0.55,
)
def bloom(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    sigma = max(0.1, p.f("sigma", 16.0))
    threshold = max(0.0, min(1.0, p.f("threshold", 0.5)))
    opacity = max(0.0, min(1.0, p.f("opacity", 0.55)))
    a, b, c = ctx.uid("bl"), ctx.uid("bl"), ctx.uid("bl")
    return [
        f"[{inp}]split=2[{a}][{b}]",
        f"[{b}]colorlevels=rimin={threshold:.3f}:gimin={threshold:.3f}:bimin={threshold:.3f},"
        f"gblur=sigma={sigma:.2f}[{c}]",
        f"[{a}][{c}]blend=all_mode=screen:all_opacity={opacity:.3f}:shortest=1[{out}]",
    ]


@effect(
    "halation",
    stage=STAGE_LIGHT,
    tags=("light", "film", "classic"),
    help="Warm film halation: amber glow bleeding off the highlights.",
    sigma=24.0,
    opacity=0.4,
    threshold=0.6,
)
def halation(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    sigma = max(0.1, p.f("sigma", 24.0))
    threshold = max(0.0, min(1.0, p.f("threshold", 0.6)))
    opacity = max(0.0, min(1.0, p.f("opacity", 0.4)))
    a, b, c = ctx.uid("hl"), ctx.uid("hl"), ctx.uid("hl")
    return [
        f"[{inp}]split=2[{a}][{b}]",
        f"[{b}]colorlevels=rimin={threshold:.3f}:gimin={threshold:.3f}:bimin={threshold:.3f},"
        f"gblur=sigma={sigma:.2f},colorbalance=rs=0.12:bs=-0.12[{c}]",
        f"[{a}][{c}]blend=all_mode=screen:all_opacity={opacity:.3f}:shortest=1[{out}]",
    ]


@effect(
    "soft_focus",
    stage=STAGE_LIGHT,
    tags=("light", "dream", "lens"),
    help="Pro-mist / diffusion: softens the image while keeping the highlights.",
    sigma=2.5,
    glow=0.3,
)
def soft_focus(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    sigma = max(0.1, p.f("sigma", 2.5))
    glow = max(0.0, min(1.0, p.f("glow", 0.3)))
    a, b, c, d = (ctx.uid("sf") for _ in range(4))
    return [
        f"[{inp}]split=2[{a}][{b}]",
        f"[{a}]gblur=sigma={sigma:.2f}[{c}]",
        f"[{b}]colorlevels=rimin=0.5:gimin=0.5:bimin=0.5,gblur=sigma={sigma * 4:.2f}[{d}]",
        f"[{c}][{d}]blend=all_mode=screen:all_opacity={glow:.3f}[{out}]",
    ]


@effect(
    "light_leak",
    stage=STAGE_LIGHT,
    tags=("light", "classic", "dream"),
    help="Animated warm light leak washing across the frame.",
    opacity=0.5,
    seed=7,
    speed=0.03,
    tint="0xff9a3c",
)
def light_leak(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    opacity = max(0.0, min(1.0, p.f("opacity", 0.5)))
    seed = p.i("seed", 7)
    speed = p.f("speed", 0.03)
    tint = p.s("tint", "0xff9a3c")
    src, soft, o1 = (ctx.uid("ll") for _ in range(3))
    return [
        f"gradients=s={ctx.w}x{ctx.h}:r={ctx.fps:g}:nb_colors=3:type=radial"
        f":c0={tint}:c1=0x8b1a2b:c2=0x000000:seed={seed}:speed={speed:.4f}[{src}]",
        f"[{src}]gblur=sigma={max(4.0, ctx.w / 14):.2f},format=yuv420p[{soft}]",
        f"[{inp}]format=yuv420p[{o1}]",
        f"[{o1}][{soft}]blend=all_mode=screen:all_opacity={opacity:.3f}:shortest=1[{out}]",
    ]


@effect(
    "lens_flare",
    stage=STAGE_LIGHT,
    tags=("light", "lens", "classic"),
    help="Procedural anamorphic lens flare with a horizontal streak.",
    opacity=0.55,
    x=0.28,
    y=0.3,
    seed=3,
)
def lens_flare(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    opacity = max(0.0, min(1.0, p.f("opacity", 0.55)))
    seed = p.i("seed", 3)
    fx, fy = p.f("x", 0.28), p.f("y", 0.3)
    src, core, c1, c2, streak, half, o1, o2 = (ctx.uid("lf") for _ in range(8))
    w, h = ctx.w, ctx.h
    return [
        f"gradients=s={w}x{h}:r={ctx.fps:g}:nb_colors=3:type=radial"
        f":c0=0xffffff:c1=0xffc46b:c2=0x000000:seed={seed}:speed=0.01[{src}]",
        f"[{src}]gblur=sigma={max(2.0, w / 90):.2f},"
        f"pad={int(w * 2)}:{int(h * 2)}:{int(-w * (0.5 - fx))}:{int(-h * (0.5 - fy))}:color=black,"
        f"crop={w}:{h}:{int(w * (0.5 + fx))}:{int(h * (0.5 + fy))}[{core}]",
        f"[{core}]split=2[{c1}][{c2}]",
        f"[{c2}]crop=2:ih:iw/2:0,scale={w}:{h}:flags=neighbor,"
        f"gblur=sigma={max(3.0, w / 45):.2f}[{streak}]",
        f"[{inp}]format=yuv420p[{o1}]",
        f"[{o1}][{c1}]blend=all_mode=screen:all_opacity={opacity:.3f}:shortest=1[{half}]",
        f"[{half}][{streak}]blend=all_mode=screen:all_opacity={opacity * 0.55:.3f}:shortest=1[{o2}]",
        f"[{o2}]null[{out}]",
    ]


@effect(
    "film_burn",
    stage=STAGE_LIGHT,
    tags=("light", "retro", "film"),
    help="Orange film burn blooming in and out over the shot.",
    opacity=0.5,
    start=0.0,
    duration=1.0,
)
def film_burn(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    opacity = max(0.0, min(1.0, p.f("opacity", 0.5)))
    start = max(0.0, p.f("start", 0.0))
    duration = max(0.1, p.f("duration", 1.0))
    src, soft, o1, o2 = (ctx.uid("fb") for _ in range(4))
    w, h = ctx.w, ctx.h
    return [
        f"gradients=s={w}x{h}:r={ctx.fps:g}:nb_colors=3:type=spiral"
        f":c0=0xffd08a:c1=0xd8431f:c2=0x000000:seed=11:speed=0.05[{src}]",
        f"[{src}]gblur=sigma={max(4.0, w / 10):.2f},format=yuv420p[{soft}]",
        f"[{inp}]format=yuv420p[{o1}]",
        f"[{o1}][{soft}]blend=all_mode=screen:all_opacity={opacity:.3f}:shortest=1"
        f":enable='between(t\\,{start:.3f}\\,{start + duration:.3f})'[{o2}]",
        f"[{o2}]null[{out}]",
    ]


@effect(
    "vignette",
    stage=STAGE_LIGHT,
    tags=("light", "classic", "lens"),
    help="Lens vignette (darkened corners).",
    strength=0.5,
)
def vignette(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    strength = max(0.0, min(1.0, p.f("strength", 0.5)))
    angle = 0.35 + 0.95 * strength
    return [f"[{inp}]vignette=angle={angle:.3f}[{out}]"]


# =========================================================================== #
# FRAMING
# =========================================================================== #
@effect(
    "letterbox",
    stage=STAGE_FRAME,
    tags=("frame", "classic"),
    help="Cinemascope bars (2.39:1 by default).",
    ratio=2.39,
    color="black",
)
def letterbox(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    ratio = max(1.0, p.f("ratio", 2.39))
    color = p.s("color", "black")
    target_h = ctx.w / ratio
    if target_h >= ctx.h - 2:
        return [f"[{inp}]null[{out}]"]
    new_h = int(target_h) // 2 * 2
    bar = (ctx.h - new_h) // 2
    return [
        f"[{inp}]scale={ctx.w}:{new_h}:flags=bicubic,"
        f"pad={ctx.w}:{ctx.h}:0:{bar}:color={color},setsar=1[{out}]"
    ]


@effect(
    "fade_in",
    stage=STAGE_FRAME,
    tags=("frame", "transition"),
    help="Fade up from black at the start of the shot.",
    duration=0.4,
    color="black",
)
def fade_in(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    d = max(0.01, p.f("duration", 0.4))
    return [f"[{inp}]fade=t=in:st=0:d={d:.3f}:c={p.s('color', 'black')}[{out}]"]


@effect(
    "fade_out",
    stage=STAGE_FRAME,
    tags=("frame", "transition"),
    help="Fade to black at the end of the shot.",
    duration=0.4,
    color="black",
)
def fade_out(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    d = max(0.01, p.f("duration", 0.4))
    start = max(0.0, ctx.dur - d)
    return [
        f"[{inp}]fade=t=out:st={start:.3f}:d={d:.3f}:c={p.s('color', 'black')}[{out}]"
    ]


@effect(
    "edge_glow",
    stage=STAGE_FRAME,
    tags=("frame", "light", "stylised"),
    help="Coloured glow bleeding in from the frame edges.",
    color="0x66ccff",
    opacity=0.35,
    size=0.12,
)
def edge_glow(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    opacity = max(0.0, min(1.0, p.f("opacity", 0.35)))
    size = max(0.02, min(0.45, p.f("size", 0.12)))
    color = p.s("color", "0x66ccff")
    t = max(2, int(ctx.h * size))
    b = max(2, int(ctx.w * size))
    chain = f"[{inp}]"
    chain += f"drawbox=x=0:y=0:w=iw:h={t}:color={color}@{opacity:.3f}:t=fill,"
    chain += f"drawbox=x=0:y=ih-{t}:w=iw:h={t}:color={color}@{opacity:.3f}:t=fill,"
    chain += f"drawbox=x=0:y=0:w={b}:h=ih:color={color}@{opacity * 0.8:.3f}:t=fill,"
    chain += f"drawbox=x=iw-{b}:y=0:w={b}:h=ih:color={color}@{opacity * 0.8:.3f}:t=fill"
    # soften the hard boxes into a glow
    a, glow, o1 = ctx.uid("eg"), ctx.uid("eg"), ctx.uid("eg")
    return [
        f"[{inp}]split=2[{a}][{glow}]",
        chain.replace(f"[{inp}]", f"[{glow}]") + f",gblur=sigma={max(4.0, ctx.w / 40):.2f}[{o1}]",
        f"[{a}][{o1}]blend=all_mode=screen:all_opacity={min(1.0, opacity * 2):.3f}[{out}]",
    ]



@effect(
    "scrim",
    stage=65,
    tags=("text", "legibility", "frame"),
    help="Soft dark gradient behind captions so lyrics stay readable over busy footage.",
    height=0.35,
    strength=0.6,
    position="bottom",
)
def scrim(ctx: Ctx, p: P, inp: str, out: str) -> List[str]:
    height = max(0.05, min(1.0, p.f("height", 0.35)))
    strength = max(0.0, min(1.0, p.f("strength", 0.6)))
    band = max(8, int(ctx.h * height)) // 2 * 2
    src, soft, o1 = (ctx.uid("sc") for _ in range(3))
    # a 2px-wide gradient costs nothing to generate and scales up smoothly
    gradient = (
        f"color=c=black:s=2x64:d=1,format=yuva420p,"
        f"geq=lum='0':cb=128:cr=128:a='{strength:.3f}*255*Y/64',"
        f"scale={ctx.w}:{band}:flags=bilinear,loop=loop=-1:size=1[{src}]"
    )
    if p.s("position", "bottom").lower().startswith("top"):
        y = 0
    else:
        y = max(0, ctx.h - band)
    return [
        gradient,
        f"[{inp}]format=yuv420p[{soft}]",
        f"[{soft}][{src}]overlay=x=0:y={y}:shortest=1[{o1}]",
        f"[{o1}]null[{out}]",
    ]


# =========================================================================== #
# Composite looks (presets)
# =========================================================================== #
PRESETS: Dict[str, List[Applied]] = {}
PRESET_HELP: Dict[str, str] = {}


def preset(name: str, help: str = "") -> Callable[[Callable[[], List[Applied]]], None]:
    """Register a named look made of several effects."""

    def deco(fn: Callable[[], List[Applied]]) -> None:
        PRESETS[name] = fn()
        PRESET_HELP[name] = help or (fn.__doc__ or "").strip().splitlines()[0] if (
            help or fn.__doc__
        ) else ""

    return deco


def _e(name: str, **params) -> Applied:
    return get_effect(name).with_params(**params)


@preset("teal_orange", "Blockbuster teal & orange grade with a punchy contrast curve.")
def _preset_teal_orange() -> List[Applied]:
    return [
        _e("teal_orange", intensity=0.9),
        _e("vibrance", amount=0.4),
        _e("contrast_punch", contrast=1.08, saturation=1.05),
    ]


@preset("cinematic", "Film look: cinematic curve + halation + grain + vignette.")
def _preset_cinematic() -> List[Applied]:
    return [
        _e("cinematic_grade", intensity=0.85),
        _e("halation", sigma=22.0, opacity=0.35),
        _e("grain", level=7),
        _e("vignette", strength=0.45),
    ]


@preset("blockbuster", "Teal & orange + halation + grain + vignette + 2.39:1 bars.")
def _preset_blockbuster() -> List[Applied]:
    return [
        _e("teal_orange", intensity=0.95),
        _e("halation", sigma=20.0, opacity=0.38),
        _e("grain", level=6),
        _e("vignette", strength=0.5),
        _e("letterbox", ratio=2.39),
    ]


@preset("neon_night", "Club/neon look: magenta-cyan grade, bloom, RGB split.")
def _preset_neon_night() -> List[Applied]:
    return [
        _e("neon_night", intensity=0.95),
        _e("bloom", sigma=18.0, threshold=0.45, opacity=0.6),
        _e("chromatic", r=2, b=-2, pulse=0.5),
        _e("grain", level=6),
        _e("vignette", strength=0.55),
    ]


@preset("vhs_retro", "VHS tape: chroma bleed, scanlines, tracking band, dust, softness.")
def _preset_vhs_retro() -> List[Applied]:
    return [
        _e("curves_preset", preset="vintage", strength=0.8),
        _e("chroma_bleed", shift=4),
        _e("soft_focus", sigma=1.2, glow=0.25),
        _e("scanlines", gap=3, strength=0.3),
        _e("tracking_band", speed=2.4, height=0.07, strength=0.5),
        _e("dust_scratches", density=0.015),
        _e("grain", level=10),
        _e("vignette", strength=0.5),
    ]


@preset("glitch_hop", "Digital glitch: block tearing, RGB split, strobe, frame flash.")
def _preset_glitch_hop() -> List[Applied]:
    return [
        _e("chromatic", r=5, b=-5, pulse=0.4),
        _e("glitch_blocks", bands=6, amount=0.3, seed=3),
        _e("contrast_punch", contrast=1.2, saturation=1.15),
        _e("strobe", rate=6.0, duty=0.25, strength=0.55),
        _e("grain", level=5),
    ]


@preset("mono_noir", "High-contrast black & white with heavy grain.")
def _preset_mono_noir() -> List[Applied]:
    return [
        _e("mono", contrast=1.25),
        _e("halation", sigma=18.0, opacity=0.25),
        _e("grain", level=12),
        _e("vignette", strength=0.6),
    ]


@preset("sunset_dream", "Golden hour: warm grade, light leak, bloom, soft focus.")
def _preset_sunset_dream() -> List[Applied]:
    return [
        _e("sunset_warm", intensity=1.0),
        _e("soft_focus", sigma=1.8, glow=0.35),
        _e("bloom", sigma=22.0, threshold=0.5, opacity=0.5),
        _e("light_leak", opacity=0.45, seed=5),
        _e("grain", level=5),
        _e("vignette", strength=0.35),
    ]


@preset("dream_trails", "Ethereal: soft focus, bloom and long motion trails.")
def _preset_dream_trails() -> List[Applied]:
    return [
        _e("soft_focus", sigma=2.2, glow=0.4),
        _e("ghost_trails", frames=8, decay=0.7),
        _e("bloom", sigma=20.0, threshold=0.55, opacity=0.45),
        _e("vibrance", amount=0.5),
        _e("grain", level=4),
    ]


@preset("festival_strobe", "EDM/festival: strobe cuts, RGB split, flashes, punchy grade.")
def _preset_festival_strobe() -> List[Applied]:
    return [
        _e("contrast_punch", contrast=1.25, saturation=1.3),
        _e("strobe", rate=10.0, duty=0.3, strength=0.65),
        _e("chromatic", r=4, b=-4, pulse=0.25),
        _e("bloom", sigma=14.0, threshold=0.55, opacity=0.5),
    ]


@preset("psychedelic", "Kaleidoscope + neon grade + trails - the visualiser look.")
def _preset_psychedelic() -> List[Applied]:
    return [
        _e("kaleidoscope"),
        _e("neon_night", intensity=1.0),
        _e("ghost_trails", frames=5, decay=0.6),
        _e("bloom", sigma=16.0, threshold=0.5, opacity=0.5),
    ]


@preset("lyrics_stage", "Caption-ready: dimmed, softened background with a scrim for text.")
def _preset_lyrics_stage() -> List[Applied]:
    return [
        _e("soft_focus", sigma=1.4, glow=0.28),
        _e("vibrance", amount=0.35),
        _e("contrast_punch", contrast=1.05, saturation=0.92),
        _e("scrim", height=0.42, strength=0.62),
        _e("vignette", strength=0.5),
    ]


@preset("clean", "Just conform the footage (no look). Useful as a control strip.")
def _preset_clean() -> List[Applied]:
    return []


# --------------------------------------------------------------------------- #
# Spec parsing
# --------------------------------------------------------------------------- #
def _split_params(spec: str) -> tuple[str, Dict[str, Any]]:
    """``bloom(sigma=20:opacity=0.6)`` -> ``('bloom', {...})``."""
    spec = spec.strip()
    if not spec:
        raise ValueError("empty effect spec")
    name, _, rest = spec.partition("(")
    name = name.strip()
    params: Dict[str, Any] = {}
    if rest:
        rest = rest.rstrip(")").rstrip()
        for chunk in rest.split(":"):
            if not chunk.strip():
                continue
            key, _, value = chunk.partition("=")
            key = key.strip()
            if not key:
                raise ValueError(f"bad parameter '{chunk}' in '{spec}'")
            params[key] = _coerce(value.strip())
    return name, params


def _coerce(value: str) -> Any:
    low = value.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def resolve_preset(name: str) -> List[Applied]:
    """Return the :class:`Applied` effects that make up preset ``name``."""
    if name not in PRESETS:
        close = [n for n in PRESETS if n.startswith(name[:3])]
        hint = f" (did you mean: {', '.join(sorted(close))}?)" if close else ""
        raise KeyError(f"unknown preset '{name}'{hint}. Run `mvfx ls` to list them.")
    import copy

    return copy.deepcopy(PRESETS[name])


def parse_effects(specs: Sequence[str] | str | None) -> List[Applied]:
    """Parse ``["preset:cinematic", "bloom(sigma=20)"]`` into applied effects.

    Supported spec forms::

        cinematic                # a preset name
        preset:cinematic         # explicit preset
        bloom                    # a single effect with defaults
        bloom(sigma=20:opacity=.6)
    """
    if specs is None:
        return []
    if isinstance(specs, str):
        specs = [specs]
    out: List[Applied] = []
    for raw in specs:
        for spec in raw.split("+"):
            spec = spec.strip()
            if not spec:
                continue
            if spec.startswith("preset:"):
                out.extend(resolve_preset(spec.split(":", 1)[1].strip()))
                continue
            name, params = _split_params(spec)
            if name in PRESETS and name not in EFFECTS:
                out.extend(resolve_preset(name))
                if params:
                    raise ValueError(
                        f"preset '{name}' takes no parameters; list its effects instead"
                    )
                continue
            out.append(get_effect(name).with_params(**params))
    return out


def list_effects() -> List[Effect]:
    """Unique effects, sorted by stage then name."""
    seen: Dict[str, Effect] = {}
    for eff in EFFECTS.values():
        seen.setdefault(eff.name, eff)
    return sorted(seen.values(), key=lambda e: (e.stage, e.name))


def list_presets() -> List[str]:
    return sorted(PRESETS)
