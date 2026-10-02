"""Procedural source footage.

mvfx needs *something* to cut when you have no clips yet.  These recipes are
pure ffmpeg ``lavfi`` graphs - no downloads, no assets - and they render fast
while giving the effects chain something structured (edges, motion, colour) to
bite on, which flat colour fields would not.

They are deliberately abstract (fractal zooms, cellular automata, smoke, light
beams) so the demo reads as a visualiser rather than as test patterns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .ffmpeg import MediaInfo, ensure_ffmpeg, probe, run

__all__ = ["SCENES", "SCENE_HELP", "render_scenes"]


def _scene_fractal(w: int, h: int, fps: int, duration: float) -> str:
    pts = int(duration * fps)
    return (
        f"mandelbrot=s={w}x{h}:rate={fps}:start_scale=3.0:end_scale=0.02:end_pts={pts},"
        f"curves=preset=strong_contrast,hue=s=0.9"
    )


def _scene_cells(w: int, h: int, fps: int, duration: float) -> str:
    return (
        f"life=s={w}x{h}:rate={fps}:mold=10:ratio=0.12:seed=7:stitch=1:"
        f"life_color=#5ce1e6:death_color=#0b1021:mold_color=#ff8a3d,"
        f"gblur=sigma=0.6"
    )


def _scene_automata(w: int, h: int, fps: int, duration: float) -> str:
    return (
        f"cellauto=s={w}x{h}:rate={fps}:rule=110:scroll=1:random_fill_ratio=0.5,"
        f"format=rgb24,lutrgb=r='val*0.6+40':g='val*0.9':b='255-val*0.7',"
        f"gblur=sigma=0.4"
    )


def _scene_smoke(w: int, h: int, fps: int, duration: float) -> str:
    """Drifting coloured smoke / stage haze."""
    return (
        f"color=c=black:s={w}x{h}:r={fps},"
        f"noise=alls=90:allf=t+u,"
        f"gblur=sigma=18,"
        f"format=rgb24,"
        f"lutrgb=r='val':g='sin(val/255*PI)*200':b='200-val*0.8',"
        f"tmix=frames=3,"
        f"eq=saturation=1.4:contrast=1.2"
    )


def _scene_beams(w: int, h: int, fps: int, duration: float) -> str:
    """Sweeping concert light beams."""
    g1 = f"gradients=s={w}x{h}:r={fps}:nb_colors=3:type=linear:c0=0xff3d81:c1=0x00e5ff:c2=0x000000:speed=0.04:seed=4"
    g2 = f"gradients=s={w}x{h}:r={fps}:nb_colors=3:type=radial:c0=0xffe066:c1=0x7a00ff:c2=0x000000:speed=0.02:seed=9"
    return (
        f"{g1}[a];{g2}[b];"
        f"[a]rotate=a='0.35*sin(2*PI*t/6)':ow={w}:oh={h}:c=black[ar];"
        f"[b]rotate=a='-0.5*sin(2*PI*t/8)':ow={w}:oh={h}:c=black[br];"
        f"[ar][br]blend=all_mode=screen[a1];"
        f"[a1]gblur=sigma=6,eq=saturation=1.5:contrast=1.2"
    )


def _scene_waves(w: int, h: int, fps: int, duration: float) -> str:
    """Slow psychedelic spiral wash."""
    return (
        f"gradients=s={w}x{h}:r={fps}:nb_colors=5:type=spiral:c0=0x1b00ff"
        f":c1=0xff0080:c2=0x00ffd5:c3=0xffd000:c4=0x000000:speed=0.05:seed=13,"
        f"zoompan=z='1+0.25*sin(2*PI*on/168)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s={w}x{h}:fps={fps},"
        f"rgbashift=rh=2:bh=-2"
    )


def _scene_grid(w: int, h: int, fps: int, duration: float) -> str:
    """Retro perspective grid - reads as motion even through heavy effects."""
    return (
        f"cellauto=s={w}x{h}:rate={fps}:rule=90:scroll=1,"
        f"perspective=x0=0:y0={int(h * 0.35)}:x1={w}:y1={int(h * 0.35)}"
        f":x2=0:y2={h}:x3={w}:y3={h}:interpolation=linear,"
        f"format=rgb24,lutrgb=r='val*0.5':g='val*0.8+30':b='val',"
        f"gblur=sigma=0.5"
    )


SCENES: Dict[str, str] = {
    "fractal": "Mandelbrot zoom - hard edges and fine detail for glitch effects",
    "cells": "Conway's Game of Life - organic motion, good for trails",
    "automata": "Scrolling cellular automaton, false-colour",
    "smoke": "Drifting coloured haze - great for bloom and leaks",
    "beams": "Sweeping concert light beams",
    "waves": "Psychedelic spiral wash",
    "grid": "Retro perspective grid",
}

SCENE_HELP = SCENES
_GENERATORS = {
    "fractal": _scene_fractal,
    "cells": _scene_cells,
    "automata": _scene_automata,
    "smoke": _scene_smoke,
    "beams": _scene_beams,
    "waves": _scene_waves,
    "grid": _scene_grid,
}


def render_scenes(
    outdir: str | Path,
    names: Optional[Sequence[str]] = None,
    *,
    w: int = 1280,
    h: int = 720,
    fps: float = 30.0,
    duration: float = 8.0,
    crf: int = 22,
    verbose: bool = False,
    dry_run: bool = False,
    ffmpeg: str | None = None,
) -> List[MediaInfo]:
    """Render the procedural scenes to ``outdir`` as mp4 files.

    Returns the probed :class:`MediaInfo` of each generated clip.
    """
    binary = ensure_ffmpeg(ffmpeg)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    selected = list(names) if names else list(SCENES)
    unknown = [n for n in selected if n not in _GENERATORS]
    if unknown:
        raise KeyError(f"unknown scene(s): {', '.join(unknown)}. Available: {', '.join(SCENES)}")

    fps_int = int(round(fps))
    results: List[MediaInfo] = []
    for name in selected:
        graph = _GENERATORS[name](w, h, fps_int, duration)
        target = outdir / f"scene_{name}.mp4"
        args = [
            "-f",
            "lavfi",
            "-i",
            graph,
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps_int),
            "-movflags",
            "+faststart",
            str(target),
        ]
        run(args, ffmpeg=binary, verbose=verbose, dry_run=dry_run, progress_total=duration)
        results.append(probe(str(target), binary))
    return results
