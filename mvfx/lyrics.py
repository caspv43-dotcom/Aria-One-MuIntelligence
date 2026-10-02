"""Lyrics: parsing, timing and ASS (karaoke) subtitle generation.

A lyrics video needs three things mvfx can now do for you:

* **timing** - if you only have the words, they get laid onto the beat grid of
  the track (:func:`from_text`), so lines land musically instead of drifting
* **word-level karaoke** - each line is split into per-word (or per-character)
  timings and written as ASS ``\\kf`` tags, which is what produces the
  left-to-right fill you see in real lyrics videos
* **legibility** - :func:`build_ass` emits properly outlined/shadowed text at
  the output resolution, and :func:`mvfx.effects.scrim` adds a gradient
  scrim under it so text stays readable over busy footage

Supported lyric inputs::

    plain text          one line per lyric line, blank lines = beat rest
    LRC                 [00:12.50]line            (enhanced <00:12.80>word ok)
    SRT-ish             1\\n00:00:12,500 --> 00:00:15,000\\nline
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .beats import Track

__all__ = [
    "LyricLine",
    "parse_lrc",
    "parse_plain",
    "parse_srt",
    "load_lyrics",
    "from_text",
    "split_words",
    "build_ass",
    "write_ass",
    "STYLES",
]

STYLES = ("karaoke", "pop", "fade", "typewriter")

_LRC_TAG = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_LRC_WORD = re.compile(r"<(\d+):(\d{1,2})(?:[.:](\d{1,3}))?>")
_SRT_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)


@dataclass
class LyricLine:
    """One caption, with optional per-word timings for karaoke."""

    text: str
    start: float
    end: float
    words: List[Tuple[str, float, float]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _stamp(minutes: str, seconds: str, frac: str | None) -> float:
    value = int(minutes) * 60 + int(seconds)
    if frac:
        # LRC uses hundredths, enhanced LRC sometimes thousandths
        value += int(frac) / (100.0 if len(frac) == 2 else 1000.0)
    return value


def parse_lrc(text: str) -> List[LyricLine]:
    """Parse LRC (and enhanced word-timestamped LRC) into timed lines."""
    raw: List[Tuple[float, str, List[Tuple[float, str]]]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        stamps = _LRC_TAG.findall(line)
        if not stamps:
            continue
        body = _LRC_TAG.sub("", line).strip()
        # enhanced LRC: <mm:ss.xx> before each word
        pieces = _LRC_WORD.split(body)
        words: List[Tuple[float, str]] = []
        plain_parts: List[str] = []
        if len(pieces) > 1:
            i = 0
            while i < len(pieces):
                chunk = pieces[i]
                if chunk and not _LRC_WORD.match(chunk or ""):
                    plain_parts.append(chunk)
                i += 1
            # rebuild (timestamp, word) pairs
            matches = list(_LRC_WORD.finditer(body))
            for index, match in enumerate(matches):
                start = _stamp(*match.groups())
                tail = body[match.end() :]
                nxt = matches[index + 1] if index + 1 < len(matches) else None
                word = tail[: nxt.start() - match.end()] if nxt else tail
                words.append((start, word.rstrip()))
                if not nxt:
                    break
            body = _LRC_WORD.sub("", body).replace("  ", " ").strip()
        for stamp in stamps:
            raw.append((_stamp(*stamp), body, words))

    raw.sort(key=lambda item: item[0])
    lines: List[LyricLine] = []
    for index, (start, body, words) in enumerate(raw):
        end = raw[index + 1][0] if index + 1 < len(raw) else start + 4.0
        timed_words: List[Tuple[str, float, float]] = []
        if words:
            for w_index, (w_start, word) in enumerate(words):
                w_end = (
                    words[w_index + 1][0] if w_index + 1 < len(words) else min(end, w_start + 0.6)
                )
                if word.strip():
                    timed_words.append((word.strip(), w_start, max(w_start + 0.05, w_end)))
        lines.append(LyricLine(text=body, start=start, end=end, words=timed_words))
    return lines


def parse_srt(text: str) -> List[LyricLine]:
    """Parse a minimal SRT file (indices + ``-->`` timestamps)."""
    lines: List[LyricLine] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        match = _SRT_TIME.search(block)
        if not match:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
        start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000.0
        end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000.0
        body = block[match.end() :].strip()
        body = "\n".join(l for l in body.splitlines() if l.strip())
        if body:
            lines.append(LyricLine(text=body.replace("\n", " "), start=start, end=end))
    return lines


_SECTION = re.compile(r"^\[[^\]]+\]$")


def parse_plain(text: str) -> List[str]:
    """Parse plain lyrics into a list of lines.

    Comments and ``[Section]`` headers are dropped.  Blank lines are kept as
    empty strings, which :func:`from_text` turns into a one-bar rest.
    """
    out: List[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            continue
        if stripped.startswith("#") or stripped.startswith("//") or _SECTION.match(stripped):
            continue
        out.append(stripped)
    while out and out[-1] == "":
        out.pop()
    return out


def load_lyrics(path: str | Path) -> List[LyricLine]:
    """Load a lyric file, detecting LRC / SRT / plain text automatically."""
    text = Path(path).read_text(encoding="utf-8")
    if _LRC_TAG.search(text):
        lines = parse_lrc(text)
        if lines:
            return lines
    if "-->" in text:
        lines = parse_srt(text)
        if lines:
            return lines
    # plain text: timing is decided later, against the track
    return [LyricLine(text=t, start=0.0, end=0.0) for t in parse_plain(text)]


# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #
def split_words(line: LyricLine, *, snap: Optional[float] = None) -> LyricLine:
    """Give each word in ``line`` a slice of the line's duration.

    Words are weighted by character count (longer words take longer) and the
    boundaries can be snapped to a grid - pass the beat period in ``snap`` to
    make the karaoke fill land on the beat.
    """
    words = line.text.split()
    if not words or line.duration <= 0:
        return line
    weights = [max(1.0, len(w) ** 0.85) for w in words]
    total = sum(weights)
    timed: List[Tuple[str, float, float]] = []
    cursor = line.start
    for word, weight in zip(words, weights):
        span = line.duration * weight / total
        word_end = cursor + span
        if snap and snap > 0:
            quantised = line.start + round((cursor - line.start) / snap) * snap
            quantised = max(line.start, min(quantised, line.end - 0.05))
            cursor = quantised
            word_end = max(cursor + 0.06, word_end)
        timed.append((word, cursor, min(word_end, line.end)))
        cursor = word_end
    if timed:
        timed[-1] = (timed[-1][0], timed[-1][1], line.end)
    line.words = timed
    return line


def from_text(
    lines: Sequence[str],
    track: Track,
    *,
    start: Optional[float] = None,
    end: Optional[float] = None,
    beats_per_line: int = 8,
    min_beats: int = 4,
    gap_beats: float = 0.0,
    words_per_second: float = 2.4,
    snap_words: bool = True,
) -> List[LyricLine]:
    """Lay untimed lyric lines onto the beat grid of ``track``.

    Each line is given a whole number of beats - estimated from its word count
    at ``words_per_second`` - then rounded to the nearest bar-friendly length.
    """
    if not track.beat_period:
        raise ValueError("track has no beat grid; run beats.analyze() first")
    period = track.beat_period
    end = track.duration if end is None else min(end, track.duration)

    if start is None:
        start = _guess_vocal_entry(track)
    cursor = track.quantize(max(0.0, start))
    if cursor < start:
        cursor += period

    out: List[LyricLine] = []
    for text in lines:
        if cursor >= end - 0.2:
            break
        if not text.strip():
            cursor += period * 4  # a blank line is a one-bar rest
            continue
        words = text.split()
        estimated = max(1.2, len(words) / max(0.5, words_per_second))
        beats = max(min_beats, min(beats_per_line, int(round(estimated / period))))
        duration = beats * period
        line = LyricLine(text=text, start=cursor, end=cursor + duration)
        if snap_words:
            split_words(line, snap=period / 2.0)
        out.append(line)
        cursor = line.end + gap_beats * period
    return out


def _guess_vocal_entry(track: Track) -> float:
    """Best guess for where the first line should start.

    Uses the loudness curve: the first beat whose energy is above the track's
    average - and never before the second bar.
    """
    if not track.energy:
        return track.beat_period * 4
    rate = track.energy_rate
    mean = sum(track.energy) / len(track.energy)
    floor_at = track.beat_period * 4
    for beat in track.beats:
        if beat < floor_at:
            continue
        index = int(beat * rate)
        if index < len(track.energy) and track.energy[index] > mean:
            return beat
    return floor_at


# --------------------------------------------------------------------------- #
# ASS generation
# --------------------------------------------------------------------------- #
def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours):d}:{int(minutes):02d}:{secs:05.2f}"


def _ass_colour(rgb: str, alpha: float = 0.0) -> str:
    """``#RRGGBB`` + alpha(0=opaque) -> ``&HAABBGGRR``."""
    rgb = rgb.lstrip("#")
    if len(rgb) == 3:
        rgb = "".join(c * 2 for c in rgb)
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    a = int(round(max(0.0, min(1.0, alpha)) * 255))
    return f"&H{a:02X}{b.upper()}{g.upper()}{r.upper()}"


def _wrap_line(text: str, max_chars: int) -> str:
    words, current, out = text.split(), "", []
    for word in words:
        candidate = (current + " " + word).strip()
        if current and len(candidate) > max_chars:
            out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)
    return "\\N".join(out[:3])


def build_ass(
    lines: Sequence[LyricLine],
    *,
    w: int = 1920,
    h: int = 1080,
    style: str = "karaoke",
    font: str = "DejaVu Sans",
    font_size: Optional[int] = None,
    highlight: str = "#FFD400",
    base: str = "#FFFFFF",
    outline_colour: str = "#000000",
    outline: float = 3.0,
    shadow: float = 2.0,
    margin_v: float = 0.09,
    max_chars: int = 30,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    title_duration: float = 4.0,
) -> str:
    """Render lyric lines as an ASS subtitle file (returned as text)."""
    if style not in STYLES:
        raise ValueError(f"unknown style '{style}' (choose from {', '.join(STYLES)})")
    size = font_size or max(28, int(h * 0.058))
    max_chars = max(12, max_chars)

    header = [
        "[Script Info]",
        "; generated by mvfx - https://github.com/caspv43-dotcom/Aria-One-MuIntelligence",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
    ]

    # PrimaryColour = sung, SecondaryColour = not-yet-sung (karaoke sweep)
    primary = _ass_colour(highlight)
    secondary = _ass_colour(base)
    tertiary = _ass_colour(highlight)
    back = _ass_colour("#000000", 0.55)
    border = _ass_colour(outline_colour)

    def style_row(name: str, fs: int, align: int, mv: int, bold: int = -1) -> str:
        return (
            f"Style: {name},{font},{fs},{primary},{secondary},{tertiary},{back},"
            f"{bold},0,0,0,100,100,0,0,1,{outline:.1f},{shadow:.1f},{align},"
            f"{int(w * 0.06)},{int(w * 0.06)},{mv},1"
        )

    header.append(style_row("Lyrics", size, 2, int(h * margin_v)))
    header.append(style_row("Title", int(size * 1.5), 5, 0))
    header.append(style_row("Credit", int(size * 0.62), 5, 0, bold=0))
    header += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, "
               "MarginR, MarginV, Effect, Text"]

    events: List[str] = []

    def karaoke_text(line: LyricLine) -> str:
        if not line.words:
            return _wrap_line(line.text, max_chars)
        parts: List[str] = []
        for word, w_start, w_end in line.words:
            centis = max(1, int(round((w_end - w_start) * 100)))
            # \kf = smooth left-to-right sweep (needs the leading backslash!)
            parts.append(f"{{\\kf{centis}}}{word} ")
        return "".join(parts).strip()

    def line_text(line: LyricLine) -> str:
        body = _wrap_line(line.text, max_chars)
        if style == "pop":
            return (
                "{\\fscx118\\fscy118\\t(0,140,\\fscx100\\fscy100)\\fad(90,140)}" + body
            )
        if style == "fade":
            return "{\\fad(180,180)}" + body
        if style == "typewriter":
            # reveal character by character
            per_char = max(1, int(round(line.duration * 100 / max(1, len(line.text)))))
            body = body.replace(" ", " ")
            return "".join(f"{{\\kf{per_char}}}{ch}" for ch in line.text if ch != "\n")
        return karaoke_text(line)

    if title:
        events.append(
            f"Dialogue: 0,{_ass_time(0)},{_ass_time(title_duration)},Title,,0,0,0,,"
            "{\\fad(220,220)\\bord6\\shad3}" + title
        )
    if subtitle:
        events.append(
            f"Dialogue: 0,{_ass_time(0.25)},{_ass_time(title_duration)},Credit,,0,0,0,,"
            "{\\fad(260,220)}" + subtitle
        )

    for line in lines:
        events.append(
            f"Dialogue: 0,{_ass_time(line.start)},{_ass_time(line.end)},Lyrics,,0,0,0,,"
            + line_text(line)
        )

    return "\n".join(header + events) + "\n"


def write_ass(text: str, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def summarise(lines: Sequence[LyricLine]) -> str:
    """Human-readable timing sheet (handy for checking the sync)."""
    out = []
    for index, line in enumerate(lines, 1):
        out.append(f"{index:3d}. {line.start:7.2f} -> {line.end:7.2f}  {line.text}")
    return "\n".join(out)
