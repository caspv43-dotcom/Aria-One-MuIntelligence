"""Build and run the ffmpeg filter graph for a :class:`~mvfx.edl.Timeline`.

The graph mvfx emits looks roughly like this::

    [0:v]fps,scale,crop[src0]                 # conform each source
    [src0]split=3[b0][b1][b2]                 # one branch per shot
    [b0]trim=start=…:end=…,setpts=PTS-STARTPTS,<per-shot fx>[v0]
    [v0][v1][v2]concat=n=3[cut]               # (or a chain of xfades)
    [cut]<global look>[outv]
    [3:a]atrim=…,afade=…,loudnorm[outa]

Two details matter a lot and are handled here:

* generated filter sources (``gradients``, ``color``) are *infinite*, so every
  ``blend`` that eats one gets ``shortest=1`` or the encode would never end
* one-frame patterns are made endless with ``loop=loop=-1:size=1``
"""

from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional, Sequence

from . import effects as fx_mod
from .beats import Track
from .edl import Shot, Timeline, single_shot
from .effects import Applied, Ctx, P, parse_effects
from .ffmpeg import MediaInfo, ensure_ffmpeg, probe, run

__all__ = ["RenderConfig", "build_graph", "render", "render_sampler"]


@dataclass
class RenderConfig:
    """Encoding + conform settings for a render."""

    w: int = 1280
    h: int = 720
    fps: float = 30.0
    fit: str = "cover"  # cover | contain | stretch
    codec: str = "libx264"
    crf: int = 20
    preset: str = "veryfast"  # encoder speed preset
    pix_fmt: str = "yuv420p"
    audio_bitrate: str = "192k"
    loudnorm: bool = True
    fade_in: float = 0.0
    fade_out: float = 0.0
    threads: int = 0
    faststart: bool = True
    verbose: bool = False
    dry_run: bool = False
    extra_output: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def conform_chain(info: MediaInfo, cfg: RenderConfig) -> str:
    """Filters that bring any source to the target size / frame rate."""
    w, h = cfg.w, cfg.h
    parts = [f"fps={cfg.fps:g}"]
    if cfg.fit == "stretch":
        parts.append(f"scale={w}:{h}")
    elif cfg.fit == "contain":
        parts.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease")
        parts.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    else:  # cover
        parts.append(f"scale={w}:{h}:force_original_aspect_ratio=increase")
        parts.append(f"crop={w}:{h}")
    parts.append("setsar=1")
    return ",".join(parts)


def chain_effects(
    ctx: Ctx,
    applied: Sequence[Applied],
    inp: str,
    out: str,
    lines: List[str],
) -> None:
    """Append filter-graph nodes applying ``applied`` from ``inp`` to ``out``."""
    ordered = sorted(enumerate(applied), key=lambda pair: (pair[1].stage, pair[0]))
    if not ordered:
        lines.append(f"[{inp}]null[{out}]")
        return
    cur = inp
    for position, (_, item) in enumerate(ordered):
        last = position == len(ordered) - 1
        nxt = out if last else ctx.uid("c")
        lines.extend(item.build(ctx, cur, nxt))
        cur = nxt


def _shot_end(shot: Shot) -> float:
    return shot.src_start + shot.src_dur


# --------------------------------------------------------------------------- #
# Graph building
# --------------------------------------------------------------------------- #
def build_graph(
    timeline: Timeline,
    *,
    cfg: RenderConfig,
    look: Sequence[Applied] = (),
    post_filter: Optional[str] = None,
    music_index: Optional[int] = None,
    music: Optional[Track] = None,
    audio_start: float = 0.0,
    audio_duration: Optional[float] = None,
    ffmpeg: str | None = None,
) -> tuple[str, str, float, bool]:
    """Return ``(filter_complex, out_video_label, out_duration, has_audio)``.

    ``music_index`` is the ffmpeg input index of the music file; when given, its
    audio is trimmed to the timeline and mapped to ``[outa]``.
    """
    lines: List[str] = []
    ctx = Ctx(
        w=cfg.w,
        h=cfg.h,
        fps=cfg.fps,
        ffmpeg_filters=frozenset(fx_mod._FILTER_CACHE) if fx_mod._FILTER_CACHE else frozenset(),
        prefix="g_",
    )

    # 1. conform the sources (only the ones the edit actually uses) -----------
    by_source: dict[int, List[int]] = {}
    for i, shot in enumerate(timeline.shots):
        by_source.setdefault(shot.src, []).append(i)
    conformed: List[Optional[str]] = [None] * len(timeline.sources)
    for src_index in by_source:
        info = timeline.sources[src_index]
        label = f"src{src_index}"
        lines.append(f"[{src_index}:v]{conform_chain(info, cfg)}[{label}]")
        conformed[src_index] = label

    # 2. split each used source into one branch per shot ----------------------
    branch: dict[tuple[int, int], str] = {}
    for src_index, shot_indices in by_source.items():
        base = conformed[src_index]
        if len(shot_indices) == 1:  # no need to copy a single-use source
            branch[shot_indices[0]] = base
            continue
        labels = [f"b{src_index}_{i}" for i in shot_indices]
        lines.append(f"[{base}]split={len(labels)}" + "".join(f"[{l}]" for l in labels))
        for shot_index, label in zip(shot_indices, labels):
            branch[shot_index] = label

    # 3. per-shot trims + effects --------------------------------------------
    shot_labels: List[str] = []
    for i, shot in enumerate(timeline.shots):
        inp = branch[i]
        out = f"v{i}"
        shot_ctx = Ctx(
            w=cfg.w,
            h=cfg.h,
            fps=cfg.fps,
            dur=shot.out_dur,
            shot=i,
            seed=1,
            prefix=f"s{i}_",
            ffmpeg_filters=ctx.ffmpeg_filters,
        )
        end = _shot_end(shot)
        lines.append(f"[{inp}]trim=start={shot.src_start:.4f}:end={end:.4f},setpts=PTS-STARTPTS[t{i}]")
        local: List[str] = []
        chain_effects(shot_ctx, shot.effects, f"t{i}", out, local)
        # hard-trim to the planned length (effects can shift timing slightly)
        lines.extend(local)
        lines.append(f"[{out}]trim=duration={shot.out_dur:.4f},setpts=PTS-STARTPTS,fps={cfg.fps:g}[{out}f]")
        shot_labels.append(f"{out}f")

    # 4. join the shots -------------------------------------------------------
    if timeline.transition != "cut" and timeline.transition_dur > 0 and len(shot_labels) > 1:
        transition = timeline.transition
        d = timeline.transition_dur
        running = timeline.shots[0].out_dur
        current = shot_labels[0]
        for i in range(1, len(shot_labels)):
            nxt = f"xf{i}"
            offset = max(0.0, running - d)
            lines.append(
                f"[{current}][{shot_labels[i]}]xfade=transition={transition}"
                f":duration={d:.4f}:offset={offset:.4f}[{nxt}]"
            )
            current = nxt
            running = running + timeline.shots[i].out_dur - d
        joined = current
        total = running
    else:
        joined = "cut"
        lines.append(
            "".join(f"[{l}]" for l in shot_labels)
            + f"concat=n={len(shot_labels)}:v=1:a=0[{joined}]"
        )
        total = sum(s.out_dur for s in timeline.shots)

    # 5. the global look ------------------------------------------------------
    global_ctx = Ctx(
        w=cfg.w,
        h=cfg.h,
        fps=cfg.fps,
        dur=total,
        shot=-1,
        seed=1,
        prefix="look_",
        ffmpeg_filters=ctx.ffmpeg_filters,
    )
    if look:
        chain_effects(global_ctx, look, joined, "outv", lines)
    else:
        lines.append(f"[{joined}]null[outv]")
    video_out = "outv"
    if post_filter:
        lines.append(f"[outv]{post_filter}[vout]")
        video_out = "vout"

    # 6. audio ----------------------------------------------------------------
    has_audio = False
    if music_index is not None:
        has_audio = True
        start = max(0.0, audio_start)
        # NOTE: atrim takes colon-separated options, unlike most filters
        atrim = f"atrim=start={start:.4f}"
        if audio_duration:
            atrim += f":duration={audio_duration:.4f}"
        parts = [atrim, "asetpts=N/SR/TB"]
        if cfg.fade_in > 0:
            parts.append(f"afade=t=in:st=0:d={cfg.fade_in:.3f}")
        if cfg.fade_out > 0:
            st = max(0.0, (audio_duration or total) - cfg.fade_out)
            parts.append(f"afade=t=out:st={st:.3f}:d={cfg.fade_out:.3f}")
        if cfg.loudnorm:
            parts.append("loudnorm=I=-14:TP=-1.5:LRA=11")
        lines.append(f"[{music_index}:a]" + ",".join(parts) + "[outa]")
    else:
        # fall back to the first source's audio, trimmed like the timeline
        info = timeline.sources[0] if timeline.sources else None
        if info is not None and info.has_audio:
            has_audio = True
            if len(timeline.shots) == 1:
                shot = timeline.shots[0]
                lines.append(
                    f"[0:a]atrim=start={shot.src_start:.4f}:duration={shot.src_dur:.4f},"
                    f"asetpts=N/SR/TB[outa]"
                )
            else:
                lines.append(f"[0:a]atrim=duration={total:.4f},asetpts=N/SR/TB[outa]")

    return ";".join(lines), video_out, total, has_audio


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _encoder_args(cfg: RenderConfig) -> List[str]:
    args: List[str] = []
    if cfg.codec in {"libx264", "libx265"}:
        args += ["-c:v", cfg.codec, "-preset", cfg.preset]
        if cfg.codec == "libx264":
            args += ["-crf", str(cfg.crf)]
        else:
            args += ["-crf", str(max(0, cfg.crf + 4))]
    else:
        args += ["-c:v", cfg.codec]
    args += ["-pix_fmt", cfg.pix_fmt, "-r", f"{cfg.fps:g}"]
    if cfg.threads:
        args += ["-threads", str(cfg.threads)]
    args += ["-fps_mode", "cfr"]
    return args


def render(
    timeline: Timeline,
    output: str | Path,
    *,
    cfg: RenderConfig,
    look: Sequence[Applied] = (),
    post_filter: Optional[str] = None,
    music_path: Optional[str] = None,
    music: Optional[Track] = None,
    audio_start: float = 0.0,
    ffmpeg: str | None = None,
) -> str:
    """Encode ``timeline`` to ``output``. Returns the output path."""
    binary = ensure_ffmpeg(ffmpeg)
    inputs: List[str] = []
    for info in timeline.sources:
        inputs += ["-i", info.path]
    music_index = None
    audio_duration = None
    if music_path:
        music_index = len(timeline.sources)
        inputs += ["-i", str(music_path)]
        audio_duration = timeline.duration or None

    graph, video_label, total, has_audio = build_graph(
        timeline,
        cfg=cfg,
        look=look,
        post_filter=post_filter,
        music_index=music_index,
        music=music,
        audio_start=audio_start,
        audio_duration=audio_duration,
        ffmpeg=binary,
    )

    args: List[str] = []
    args += inputs
    args += ["-filter_complex", graph, "-map", f"[{video_label}]"]
    if has_audio:
        args += ["-map", "[outa]"]
        args += ["-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000"]
    else:
        args += ["-an"]
    args += _encoder_args(cfg)
    args += ["-t", f"{total:.4f}"]
    args += list(cfg.extra_output)
    if cfg.faststart:
        args += ["-movflags", "+faststart"]
    args += [str(output)]

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    result = run(
        args,
        ffmpeg=binary,
        verbose=cfg.verbose,
        dry_run=cfg.dry_run,
        progress_total=total,
    )
    return str(output)


# --------------------------------------------------------------------------- #
# Sampler (grid of looks)
# --------------------------------------------------------------------------- #
def render_sampler(
    source: str | Path,
    output: str | Path,
    *,
    cfg: RenderConfig,
    presets: Sequence[str],
    cols: int = 3,
    start: float = 0.0,
    duration: float = 3.0,
    music_path: Optional[str] = None,
    music: Optional[Track] = None,
    audio_start: float = 0.0,
    ffmpeg: str | None = None,
) -> str:
    """Render a labelled grid comparing several looks on the same footage."""
    binary = ensure_ffmpeg(ffmpeg)
    rows = math.ceil(len(presets) / cols)
    cell_w = cfg.w
    cell_h = cfg.h
    grid_w, grid_h = cell_w * cols, cell_h * rows

    info = probe(str(source), binary)
    lines: List[str] = []
    lines.append(
        f"[0:v]fps={cfg.fps:g},scale={cfg.w}:{cfg.h}:force_original_aspect_ratio=increase,"
        f"crop={cfg.w}:{cfg.h},setsar=1,"
        f"trim=start={start:.3f}:duration={duration:.3f},setpts=PTS-STARTPTS[base]"
    )
    labels = [f"p{i}" for i in range(len(presets))]
    lines.append(f"[base]split={len(presets)}" + "".join(f"[{l}]" for l in labels))

    cells: List[str] = []
    for index, (preset_name, label) in enumerate(zip(presets, labels)):
        try:
            applied = fx_mod.resolve_preset(preset_name)
        except KeyError:
            applied = parse_effects([preset_name])
        ctx = Ctx(
            w=cfg.w, h=cfg.h, fps=cfg.fps, dur=duration, shot=index, seed=1, prefix=f"p{index}_"
        )
        cell = f"c{index}"
        local: List[str] = []
        chain_effects(ctx, applied, label, cell, local)
        lines.extend(local)
        lines.append(f"[{cell}]scale={cell_w}:{cell_h}:flags=bicubic,setsar=1[{cell}s]")
        cells.append(f"{cell}s")

    layout = "|".join(
        f"{(i % cols) * cell_w}_{(i // cols) * cell_h}" for i in range(len(cells))
    )
    lines.append(
        "".join(f"[{c}]" for c in cells)
        + f"xstack=inputs={len(cells)}:layout={layout},format={cfg.pix_fmt}[grid]"
    )
    if grid_w > max_w:
        grid_h = int(grid_h * max_w / grid_w) // 2 * 2
        grid_w = max_w
        lines.append(f"[grid]scale={grid_w}:{grid_h}:flags=lanczos,setsar=1[outv]")
    else:
        lines.append("[grid]setsar=1[outv]")

    args: List[str] = ["-i", str(source)]
    music_index = None
    if music_path:
        music_index = 1
        args += ["-i", str(music_path)]
        lines.append(
            f"[{music_index}:a]atrim=start={max(0.0, audio_start):.3f}:duration={duration:.3f},"
            f"asetpts=N/SR/TB,loudnorm=I=-14:TP=-1.5:LRA=11[outa]"
        )
    args += ["-filter_complex", ";".join(lines), "-map", "[outv]"]
    if music_index is not None:
        args += ["-map", "[outa]", "-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000"]
    else:
        args += ["-an"]

    args += [
        "-c:v",
        cfg.codec,
        "-preset",
        cfg.preset,
        "-crf",
        str(cfg.crf),
        "-pix_fmt",
        cfg.pix_fmt,
        "-r",
        f"{cfg.fps:g}",
        "-t",
        f"{duration:.3f}",
    ]
    if cfg.faststart:
        args += ["-movflags", "+faststart"]
    args += [str(output)]

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    run(args, ffmpeg=binary, verbose=cfg.verbose, dry_run=cfg.dry_run, progress_total=duration)
    return str(output)


# --------------------------------------------------------------------------- #
# Segmented rendering
# --------------------------------------------------------------------------- #
def render_segmented(
    timeline: Timeline,
    output: str | Path,
    *,
    cfg: RenderConfig,
    look: Sequence[Applied] = (),
    post_filter: Optional[str] = None,
    music_path: Optional[str] = None,
    music: Optional[Track] = None,
    audio_start: float = 0.0,
    segment_size: int = 10,
    keep_segments: bool = False,
    ffmpeg: str | None = None,
) -> str:
    """Render a long timeline in chunks, then join and finish it.

    A filter graph with N shots holds N decoded branches in flight at once -
    at 1080p that is what kills a long music video (74 shots OOMs on 4 GB).
    So the edit is rendered in ``segment_size``-shot chunks, those chunks are
    losslessly concatenated, and the global look, scrim and captions are
    applied once, in a single final pass.

    Returns the output path.
    """
    binary = ensure_ffmpeg(ffmpeg)
    output = Path(output)
    total = timeline.duration or sum(s.out_dur for s in timeline.shots)
    shots = timeline.shots
    segment_size = max(1, int(segment_size))
    chunks = [shots[i : i + segment_size] for i in range(0, len(shots), segment_size)]

    if len(chunks) == 1:
        return render(
            timeline,
            output,
            cfg=cfg,
            look=look,
            post_filter=post_filter,
            music_path=music_path,
            music=music,
            audio_start=audio_start,
            ffmpeg=binary,
        )

    if cfg.dry_run:
        # show the command for the first chunk plus a note about the rest
        sub0 = Timeline(
            shots=chunks[0],
            sources=timeline.sources,
            w=cfg.w,
            h=cfg.h,
            fps=cfg.fps,
            transition=timeline.transition,
            transition_dur=timeline.transition_dur,
            duration=sum(s.out_dur for s in chunks[0]),
        )
        render(sub0, output, cfg=seg_cfg if False else cfg, look=(), ffmpeg=binary)
        print(
            f"# ...then {max(0, len(chunks) - 1)} more segment(s), a lossless join, "
            f"and a finishing pass applying the look and captions."
        )
        return str(output)

    workdir = output.parent / f".{output.stem}_segments"
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. render each chunk (no look yet - it is applied once at the end)
    seg_cfg = replace(cfg, crf=min(16, cfg.crf), loudnorm=False, fade_in=0.0, fade_out=0.0)
    segments: List[Path] = []
    for index, chunk in enumerate(chunks):
        sub = Timeline(
            shots=chunk,
            sources=timeline.sources,
            w=cfg.w,
            h=cfg.h,
            fps=cfg.fps,
            transition=timeline.transition,
            transition_dur=timeline.transition_dur,
            duration=sum(s.out_dur for s in chunk),
        )
        target = workdir / f"seg_{index:03d}.mp4"
        render(sub, target, cfg=seg_cfg, look=(), post_filter=None, ffmpeg=binary)
        segments.append(target)
        if cfg.verbose:
            print(f"  segment {index + 1}/{len(chunks)} done")

    # 2. lossless join
    if len(segments) == 1:
        joined: Path = segments[0]
    else:
        listing = workdir / "concat.txt"
        listing.write_text("\n".join(f"file '{p.resolve()}'" for p in segments) + "\n")
        joined = workdir / "joined.mp4"
        run(
            ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(joined)],
            ffmpeg=binary,
            verbose=cfg.verbose,
        )

    # 3. finishing pass: look + captions + music
    info = probe(str(joined), binary)
    final = single_shot(info, w=cfg.w, h=cfg.h, fps=cfg.fps, duration=min(info.duration, total))
    render(
        final,
        output,
        cfg=replace(cfg, extra_output=list(cfg.extra_output) + (["-shortest"] if music_path else [])),
        look=look,
        post_filter=post_filter,
        music_path=music_path,
        music=music,
        audio_start=audio_start,
        ffmpeg=binary,
    )

    if not keep_segments:
        shutil.rmtree(workdir, ignore_errors=True)
    return str(output)
