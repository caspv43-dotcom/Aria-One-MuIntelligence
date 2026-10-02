"""Command line interface for mvfx.

Commands::

    mvfx ls                      list effects, looks and transitions
    mvfx beats MUSIC             BPM, beat grid and onset times
    mvfx apply VIDEO             grade / effect a single clip
    mvfx auto CLIPS... --music   beat-synced music video from a pile of clips
    mvfx sampler VIDEO           grid comparing looks side by side
    mvfx scenes                  generate procedural footage to practise on
    mvfx demo                    end-to-end demo (scenes + bundled music)
    mvfx check VIDEO             photosensitive-epilepsy flash warning
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import effects as fx_mod
from .beats import Track, analyze, load_cache, save_cache
from .edl import Timeline, plan, single_shot
from .effects import PRESET_HELP, list_effects, parse_effects, resolve_preset
from .ffmpeg import (
    FFmpegNotFound,
    MediaInfo,
    ensure_ffmpeg,
    filter_names,
    probe,
    run,
)
from .lyrics import (
    STYLES,
    build_ass,
    from_text,
    load_lyrics,
    split_words,
    summarise,
    write_ass,
)
from .render import RenderConfig, render, render_segmented, render_sampler
from .scenes import SCENES, render_scenes

#: bundled fonts (DejaVu) shipped with the repo so libass never needs fontconfig
BUNDLED_FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"

SIZES: Dict[str, tuple[int, int]] = {
    "vertical": (1080, 1920),
    "reels": (1080, 1920),
    "shorts": (1080, 1920),
    "square": (1080, 1080),
    "hd": (1280, 720),
    "720p": (1280, 720),
    "fhd": (1920, 1080),
    "1080p": (1920, 1080),
    "2k": (2048, 1080),
    "4k": (3840, 2160),
}

TRANSITIONS = [
    "cut",
    "fade",
    "dissolve",
    "fadeblack",
    "fadewhite",
    "wipeleft",
    "wiperight",
    "wipeup",
    "wipedown",
    "slideleft",
    "slideright",
    "slideup",
    "slidedown",
    "smoothleft",
    "smoothright",
    "circleopen",
    "circleclose",
    "radial",
    "pixelize",
    "hblur",
    "zoomin",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def parse_size(value: str) -> tuple[int, int]:
    value = value.strip().lower()
    if value in SIZES:
        return SIZES[value]
    for sep in ("x", "*", ":"):
        if sep in value:
            w, _, h = value.partition(sep)
            try:
                return int(w), int(h)
            except ValueError:
                pass
    raise argparse.ArgumentTypeError(
        f"cannot parse size '{value}' (try 1920x1080, vertical, square, hd)"
    )


def _wrap(text: str, indent: int = 4, width: int = 96) -> str:
    return textwrap.fill(text, width=width, initial_indent=" " * indent, subsequent_indent=" " * indent)


def _init_filters(ffmpeg: str | None) -> None:
    """Tell the effect library which filters this build actually has."""
    try:
        fx_mod.set_available_filters(filter_names(ffmpeg))
    except Exception:
        fx_mod.set_available_filters([])


def _cfg_from_args(args: argparse.Namespace) -> RenderConfig:
    return RenderConfig(
        w=args.size[0],
        h=args.size[1],
        fps=args.fps,
        fit=args.fit,
        codec=args.codec,
        crf=args.crf,
        preset=args.speed,
        pix_fmt="yuv420p",
        audio_bitrate=args.audio_bitrate,
        loudnorm=not args.no_loudnorm,
        fade_in=args.fade_in,
        fade_out=args.fade_out,
        threads=args.threads,
        verbose=args.verbose,
        dry_run=args.dry_run,
    )


def _gather_specs(args: argparse.Namespace) -> List[str]:
    specs: List[str] = []
    for preset_name in args.preset or []:
        specs.append(f"preset:{preset_name}")
    specs.extend(args.fx or [])
    return specs


def _load_track(music: str, args: argparse.Namespace) -> Track:
    binary = ensure_ffmpeg(args.ffmpeg)
    cache_dir = args.cache_dir or ".mvfx"
    track = None
    if not args.no_cache:
        track = load_cache(music, cache_dir)
        if track and args.verbose:
            print(f"[cache] loaded beat analysis from {cache_dir}", file=sys.stderr)
    if track is None:
        print(f"analysing {music} …", file=sys.stderr)
        track = analyze(
            music,
            ffmpeg=binary,
            sensitivity=args.sensitivity,
            min_bpm=args.min_bpm,
            max_bpm=args.max_bpm,
            progress=args.verbose,
        )
        if not args.no_cache:
            save_cache(track, cache_dir)
    if not track.beat_period:
        print(
            "warning: could not detect a tempo; falling back to 120 BPM",
            file=sys.stderr,
        )
        track.bpm, track.beat_period, track.offset = 120.0, 0.5, 0.0
    return track


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_ls(args: argparse.Namespace) -> int:
    show_effects = args.effects or not (args.presets or args.transitions or args.scenes)
    show_presets = args.presets or not (args.effects or args.transitions or args.scenes)

    if show_presets:
        print("LOOKS (presets)")
        print("=" * 78)
        for name in sorted(fx_mod.PRESETS):
            print(f"  {name}")
            print(_wrap(PRESET_HELP.get(name, ""), 6))
            print(_wrap("= " + ", ".join(a.name for a in fx_mod.PRESETS[name]) or "= (none)", 6))
        print()

    if show_effects:
        print("EFFECTS")
        print("=" * 78)
        current_stage = None
        for effect in list_effects():
            if effect.stage != current_stage:
                current_stage = effect.stage
                print(f"\n  [{fx_mod.STAGE_NAMES.get(effect.stage, effect.stage)}]")
            params = ""
            if effect.params:
                params = "(" + ":".join(f"{k}={v}" for k, v in effect.params.items()) + ")"
            print(f"    {effect.name}{params}")
            print(_wrap(effect.help, 8))
        print()

    if args.transitions:
        print("TRANSITIONS (--transition)")
        print("=" * 78)
        print(_wrap(", ".join(TRANSITIONS), 2))
        print()

    if args.scenes:
        print("PROCEDURAL SCENES")
        print("=" * 78)
        for name, help_text in SCENES.items():
            print(f"  {name}")
            print(_wrap(help_text, 6))
    return 0


def cmd_beats(args: argparse.Namespace) -> int:
    track = _load_track(args.music, args)
    print(f"\n{args.music}")
    print(f"  duration : {track.duration:.2f}s")
    print(f"  tempo    : {track.bpm:.2f} BPM  (beat every {track.beat_period * 1000:.0f} ms)")
    print(f"  offset   : {track.offset:.3f}s")
    print(f"  beats    : {len(track.beats)}")
    print(f"  onsets   : {len(track.onsets)}")
    if args.print_beats:
        print("\n  beat grid (s):")
        for i in range(0, len(track.beats), 8):
            print("   ", " ".join(f"{b:6.2f}" for b in track.beats[i : i + 8]))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(track.to_json())
        print(f"\n  wrote {args.json}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    binary = ensure_ffmpeg(args.ffmpeg)
    _init_filters(binary)
    info = probe(args.input, binary)
    if not info.has_video:
        raise SystemExit(f"{args.input} has no video stream")
    cfg = _cfg_from_args(args)
    if args.duration and cfg.fade_out:
        pass
    applied = parse_effects(_gather_specs(args))
    timeline = single_shot(
        info, w=cfg.w, h=cfg.h, fps=cfg.fps, effects=applied, start=args.start, duration=args.duration
    )
    if args.print_edl:
        print(timeline.shots[0].describe())
    render(
        timeline,
        args.output,
        cfg=cfg,
        look=args.global_fx and parse_effects(args.global_fx) or [],
        music_path=args.music,
        audio_start=args.music_start,
        ffmpeg=binary,
    )
    print(f"wrote {args.output}")
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    binary = ensure_ffmpeg(args.ffmpeg)
    _init_filters(binary)
    if not args.inputs:
        raise SystemExit("auto needs at least one input clip")
    sources = [probe(p, binary) for p in args.inputs]
    for s in sources:
        if not s.has_video:
            raise SystemExit(f"{s.path} has no video stream")

    cfg = _cfg_from_args(args)
    look = parse_effects(_gather_specs(args)) or parse_effects(["preset:cinematic"])

    if args.music:
        track = _load_track(args.music, args)
        duration = args.duration
    else:
        # no music: cut on a nominal tempo and keep the audio of the clips
        track = Track(duration=min(s.duration for s in sources))
        track.bpm = args.bpm or 120.0
        track.beat_period = 60.0 / track.bpm
        track.offset = 0.0
        period = track.beat_period
        track.beats = [i * period for i in range(int(track.duration / period) + 1)]
        duration = args.duration

    timeline = plan(
        track,
        sources,
        w=cfg.w,
        h=cfg.h,
        fps=cfg.fps,
        start=args.music_start,
        duration=duration,
        intensity=args.intensity,
        seed=args.seed,
        transition=args.transition,
        transition_dur=args.transition_dur,
        min_shot_beats=args.min_beats,
        max_shot_beats=args.max_beats,
        flash_on_downbeat=not args.no_flash,
    )
    print(
        f"planned {len(timeline.shots)} shots over {timeline.duration:.1f}s "
        f"({timeline.duration / max(1, len(timeline.shots)):.2f}s per shot)"
    )
    if args.print_edl:
        for shot in timeline.shots:
            print("  " + shot.describe())
    if args.edl_out:
        Path(args.edl_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.edl_out).write_text(
            json.dumps(
                [
                    {
                        "start": s.start,
                        "out_dur": s.out_dur,
                        "src": timeline.sources[s.src].path,
                        "src_start": s.src_start,
                        "src_dur": s.src_dur,
                        "effects": [a.name for a in s.effects],
                    }
                    for s in timeline.shots
                ],
                indent=1,
            )
        )
        print(f"wrote {args.edl_out}")

    render_segmented(
        timeline,
        args.output,
        cfg=cfg,
        look=look,
        music_path=args.music,
        music=track if args.music else None,
        audio_start=args.music_start,
        segment_size=args.segment_size,
        keep_segments=args.keep_segments,
        ffmpeg=binary,
    )
    print(f"wrote {args.output}")
    return 0


def cmd_sampler(args: argparse.Namespace) -> int:
    binary = ensure_ffmpeg(args.ffmpeg)
    _init_filters(binary)
    cfg = _cfg_from_args(args)
    presets = args.presets or [
        "clean",
        "teal_orange",
        "cinematic",
        "blockbuster",
        "neon_night",
        "vhs_retro",
        "glitch_hop",
        "mono_noir",
        "sunset_dream",
    ]
    if args.cols:
        cols = args.cols
    else:
        cols = 3
    render_sampler(
        args.input,
        args.output,
        cfg=cfg,
        presets=presets,
        cols=cols,
        start=args.start,
        duration=args.duration,
        music_path=args.music,
        audio_start=args.music_start,
        ffmpeg=binary,
    )
    print(f"wrote {args.output}")
    print("grid order (left to right, top to bottom):")
    for index, name in enumerate(presets):
        print(f"  {index + 1:2d}. {name}")
    return 0


def cmd_scenes(args: argparse.Namespace) -> int:
    binary = ensure_ffmpeg(args.ffmpeg)
    infos = render_scenes(
        args.out,
        args.names or None,
        w=args.size[0],
        h=args.size[1],
        fps=args.fps,
        duration=args.duration,
        verbose=args.verbose,
        dry_run=args.dry_run,
        ffmpeg=binary,
    )
    for info in infos:
        print(f"  {info.path}  {info.width}x{info.height}  {info.duration:.1f}s")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Generate scenes, then cut them to the bundled music track."""
    binary = ensure_ffmpeg(args.ffmpeg)
    _init_filters(binary)
    outdir = Path(args.out)
    scenes_dir = outdir / "scenes"
    cfg = _cfg_from_args(args)

    if not args.skip_scenes:
        print("rendering procedural footage …")
        sources = render_scenes(
            scenes_dir,
            args.names or None,
            w=cfg.w,
            h=cfg.h,
            fps=cfg.fps,
            duration=args.scene_duration,
            verbose=args.verbose,
            ffmpeg=binary,
        )
    else:
        sources = [probe(str(p), binary) for p in sorted(scenes_dir.glob("*.mp4"))]
        if not sources:
            raise SystemExit(f"no scenes in {scenes_dir}; run without --skip-scenes")

    music = args.music
    if not music:
        candidates = sorted(Path(".").glob("*.mp3")) + sorted(Path(".").glob("*.wav"))
        if not candidates:
            raise SystemExit("no music found: pass --music PATH")
        music = str(candidates[0])
    track = _load_track(music, args)

    timeline = plan(
        track,
        sources,
        w=cfg.w,
        h=cfg.h,
        fps=cfg.fps,
        start=args.music_start,
        duration=args.duration,
        intensity=args.intensity,
        seed=args.seed,
        transition=args.transition,
        transition_dur=args.transition_dur,
    )
    print(f"cut {len(timeline.shots)} shots to {Path(music).name} at {track.bpm:.1f} BPM")
    if args.print_edl:
        for shot in timeline.shots:
            print("  " + shot.describe())

    look = parse_effects(_gather_specs(args)) or parse_effects(["preset:neon_night"])
    render_segmented(
        timeline,
        outdir / "music_video.mp4",
        cfg=cfg,
        look=look,
        music_path=music,
        music=track,
        audio_start=args.music_start,
        segment_size=args.segment_size,
        keep_segments=args.keep_segments,
        ffmpeg=binary,
    )
    print(f"wrote {outdir / 'music_video.mp4'}")

    if not args.skip_sampler:
        render_sampler(
            sources[0].path,
            outdir / "effects_sampler.mp4",
            cfg=RenderConfig(
                w=cfg.w // 2 if cfg.w >= 640 else cfg.w,
                h=cfg.h // 2 if cfg.h >= 360 else cfg.h,
                fps=cfg.fps,
                codec=cfg.codec,
                crf=cfg.crf,
                preset=cfg.preset,
                verbose=cfg.verbose,
                dry_run=cfg.dry_run,
            ),
            presets=args.sampler_presets
            or [
                "clean",
                "teal_orange",
                "cinematic",
                "blockbuster",
                "neon_night",
                "vhs_retro",
                "glitch_hop",
                "mono_noir",
                "sunset_dream",
            ],
            cols=3,
            start=0.5,
            duration=args.sampler_duration,
            music_path=music,
            audio_start=args.music_start,
            ffmpeg=binary,
        )
        print(f"wrote {outdir / 'effects_sampler.mp4'}")
    return 0



def cmd_lyrics(args: argparse.Namespace) -> int:
    """Build a captioned lyrics video from a track + lyric text."""
    binary = ensure_ffmpeg(args.ffmpeg)
    _init_filters(binary)
    cfg = _cfg_from_args(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    track = _load_track(args.music, args)

    # ---------------- lyrics ------------------------------------------------
    lyric_path = args.lyrics
    if not lyric_path:
        for suffix in (".lrc", ".txt", ".srt"):
            candidate = Path(args.music).with_suffix(suffix)
            if candidate.is_file():
                lyric_path = str(candidate)
                break
    if not lyric_path or not Path(lyric_path).is_file():
        raise SystemExit(
            "no lyrics file. Pass --lyrics FILE (plain text, LRC or SRT).\n"
            "Plain text is timed automatically against the beat grid."
        )

    parsed = load_lyrics(lyric_path)
    if not parsed:
        raise SystemExit(f"no lyric lines found in {lyric_path}")

    has_timings = any(line.end > line.start for line in parsed)
    if has_timings:
        lines = []
        for line in parsed:
            if not line.words:
                split_words(line, snap=track.beat_period / 2.0)
            lines.append(line)
        print(f"using timings from {lyric_path} ({len(lines)} lines)")
    else:
        end = args.music_start + args.duration if args.duration else None
        lines = from_text(
            [line.text for line in parsed],
            track,
            start=args.lyrics_start,
            end=end,
            beats_per_line=args.beats_per_line,
            min_beats=args.min_beats,
            gap_beats=args.gap_beats,
            words_per_second=args.words_per_second,
        )
        print(
            f"timed {len(lines)} lyric lines to the beat grid "
            f"({track.bpm:.1f} BPM, first line at {lines[0].start:.1f}s)"
        )

    if args.fonts_dir:
        fonts_dir = args.fonts_dir
    elif BUNDLED_FONTS.is_dir():
        fonts_dir = str(BUNDLED_FONTS)
    else:
        fonts_dir = None  # let libass fall back to whatever fontconfig knows
    ass_text = build_ass(
        lines,
        w=cfg.w,
        h=cfg.h,
        style=args.style,
        font=args.font,
        font_size=args.font_size,
        highlight=args.highlight,
        base=args.text_colour,
        outline=args.outline,
        shadow=args.shadow,
        margin_v=args.margin,
        max_chars=args.max_chars,
        title=args.title,
        subtitle=args.subtitle,
        title_duration=args.title_duration,
    )
    ass_path = write_ass(ass_text, args.ass_out or str(output.with_suffix(".ass")))
    if args.print_sheet:
        print(summarise(lines))
    print(f"wrote {ass_path}")

    # ---------------- backgrounds -------------------------------------------
    if args.background:
        sources = [probe(item, binary) for item in args.background]
    else:
        scene_dir = Path(args.scene_dir)
        if not args.skip_scenes or not any(scene_dir.glob("*.mp4")):
            print("rendering procedural backgrounds …")
            sources = render_scenes(
                scene_dir,
                args.scenes or None,
                w=cfg.w,
                h=cfg.h,
                fps=cfg.fps,
                duration=args.scene_duration,
                verbose=args.verbose,
                ffmpeg=binary,
            )
        else:
            sources = [probe(str(item), binary) for item in sorted(scene_dir.glob("*.mp4"))]

    look = parse_effects(_gather_specs(args)) or parse_effects(["preset:lyrics_stage"])
    if not any(applied.name == "scrim" for applied in look):
        look.append(parse_effects(["scrim(height=0.42:strength=0.6)"])[0])

    timeline = plan(
        track,
        sources,
        w=cfg.w,
        h=cfg.h,
        fps=cfg.fps,
        start=args.music_start,
        duration=args.duration,
        intensity=args.intensity,
        seed=args.seed,
        transition=args.transition,
        transition_dur=args.transition_dur,
        flash_on_downbeat=not args.no_flash,
    )
    print(
        f"cut {len(timeline.shots)} shots over {timeline.duration:.1f}s "
        f"({timeline.duration / max(1, len(timeline.shots)):.2f}s per shot)"
    )
    if args.print_edl:
        for shot in timeline.shots:
            print("  " + shot.describe())

    post_filter = f"subtitles=f='{ass_path.as_posix()}'"
    if fonts_dir:
        post_filter += f":fontsdir='{Path(fonts_dir).as_posix()}'"

    render_segmented(
        timeline,
        output,
        cfg=cfg,
        look=look,
        post_filter=post_filter,
        music_path=args.music,
        music=track,
        audio_start=args.music_start,
        segment_size=args.segment_size,
        keep_segments=args.keep_segments,
        ffmpeg=binary,
    )
    print(f"wrote {output}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Flash / photosensitivity warning based on frame luminance jumps."""
    binary = ensure_ffmpeg(args.ffmpeg)
    info = probe(args.input, binary)
    cmd = [
        "-v",
        "error",
        "-i",
        args.input,
        "-vf",
        "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
        "-f",
        "null",
        "-",
    ]
    out = run(cmd, ffmpeg=binary, check=True, quiet=True)
    values: List[float] = []
    for line in out.log_lines:
        if "YAVG" in line:
            try:
                values.append(float(line.split("=")[-1].strip()))
            except ValueError:
                continue
    if len(values) < 4:
        print("not enough frames to analyse")
        return 0

    fps = info.fps or 25.0
    deltas = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    threshold = 0.10 * 255  # ~10% of the luminance range
    flashes = 0
    i = 0
    while i < len(deltas):
        if deltas[i] > threshold:
            flashes += 1
            i += max(1, int(fps / 6))  # debounce: 6 flashes/s max counting
        else:
            i += 1
    seconds = len(values) / fps
    rate = flashes / max(0.001, seconds)
    verdict = "OK"
    note = "no problematic flashing detected"
    if rate >= 3.0:
        verdict = "WARNING"
        note = (
            "flashing above 3 Hz - consider adding the photosensitive-epilepsy "
            "warning or reducing --strobe/--flash effects"
        )
    print(f"{args.input}")
    print(f"  frames          : {len(values)} ({seconds:.1f}s @ {fps:.2f}fps)")
    print(f"  luminance jumps : {flashes} ({rate:.2f} per second)")
    print(f"  verdict         : {verdict}")
    print(f"  {note}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    binary = ensure_ffmpeg(args.ffmpeg)
    print(f"ffmpeg: {binary}")
    out = run(["-version"], ffmpeg=binary, check=True, quiet=True)
    for line in out.log_lines[:3]:
        print("  " + line)
    names = filter_names(binary)
    print(f"  filters: {len(names)}")
    for path in args.inputs or []:
        print(f"  {probe(path, binary)}")
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mvfx",
        description="Music-video effects and beat-synced cutting on top of ffmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              mvfx ls --presets
              mvfx apply clip.mp4 -e 'preset:teal_orange+bloom(sigma=22)+grain(level=10)' -o out.mp4
              mvfx auto takes/*.mp4 --music song.mp3 --look neon_night -o video.mp4
              mvfx sampler clip.mp4 --presets cinematic,vhs_retro,glitch_hop -o grid.mp4
              mvfx demo                       # procedural footage + bundled music
            """
        ),
    )
    parser.add_argument("--ffmpeg", default=os.environ.get("MVFX_FFMPEG"), help="path to ffmpeg")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the ffmpeg command")
    parser.add_argument("--dry-run", action="store_true", help="print the ffmpeg command only")
    sub = parser.add_subparsers(dest="command", required=True)

    # ls -------------------------------------------------------------------
    p = sub.add_parser("ls", help="list effects, looks, transitions and scenes")
    p.add_argument("--effects", action="store_true")
    p.add_argument("--presets", action="store_true")
    p.add_argument("--transitions", action="store_true")
    p.add_argument("--scenes", action="store_true")
    p.set_defaults(func=cmd_ls)

    # info -----------------------------------------------------------------
    p = sub.add_parser("info", help="show the ffmpeg build and probe files")
    p.add_argument("inputs", nargs="*")
    p.set_defaults(func=cmd_info)

    # beats ----------------------------------------------------------------
    p = sub.add_parser("beats", help="analyse a track's tempo and beat grid")
    p.add_argument("music")
    p.add_argument("--print-beats", action="store_true")
    p.add_argument("--json", help="write the analysis to this JSON file")
    p.add_argument("--sensitivity", type=float, default=1.5)
    p.add_argument("--min-bpm", type=float, default=60.0)
    p.add_argument("--max-bpm", type=float, default=200.0)
    p.add_argument("--cache-dir", default=".mvfx")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(func=cmd_beats)

    # shared output options -------------------------------------------------
    def add_common(p: argparse.ArgumentParser, *, with_audio: bool = True) -> None:
        p.add_argument("-o", "--output", required=True)
        p.add_argument("-e", "--fx", action="append", default=[], metavar="SPEC",
                       help="effect spec, e.g. bloom(sigma:22) - repeatable")
        p.add_argument("--preset", action="append", default=[], help="named look (repeatable)")
        p.add_argument("--global-fx", action="append", default=[],
                       help="effects applied to the finished cut, after the per-shot effects")
        p.add_argument("--size", type=parse_size, default="hd")
        p.add_argument("--fps", type=float, default=30.0)
        p.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
        p.add_argument("--codec", default="libx264")
        p.add_argument("--crf", type=int, default=20)
        p.add_argument("--speed", default="veryfast", help="encoder preset (x264)")
        p.add_argument("--threads", type=int, default=0)
        p.add_argument(
            "--segment-size",
            type=int,
            default=10,
            help="shots per render pass (lower = less memory on long edits)",
        )
        p.add_argument("--keep-segments", action="store_true")
        if with_audio:
            p.add_argument("--audio-bitrate", default="192k")
            p.add_argument("--no-loudnorm", action="store_true")
            p.add_argument("--fade-in", type=float, default=0.0)
            p.add_argument("--fade-out", type=float, default=0.0)

    # apply ----------------------------------------------------------------
    p = sub.add_parser("apply", help="grade / effect a single clip")
    p.add_argument("input")
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--music", help="replace the clip's audio with this track")
    p.add_argument("--music-start", type=float, default=0.0)
    p.add_argument("--print-edl", action="store_true")
    add_common(p)
    p.set_defaults(func=cmd_apply)

    # auto -----------------------------------------------------------------
    p = sub.add_parser("auto", help="cut clips to a music track automatically")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--music")
    p.add_argument("--music-start", type=float, default=0.0)
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--bpm", type=float, default=None, help="assume this tempo when --music is absent")
    p.add_argument("--intensity", type=float, default=0.7, help="0=calm, 1=frantic cutting")
    p.add_argument("--min-beats", type=int, default=1)
    p.add_argument("--max-beats", type=int, default=8)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--transition", default="cut", choices=TRANSITIONS)
    p.add_argument("--transition-dur", type=float, default=0.0, help="seconds (0 = hard cuts)")
    p.add_argument("--no-flash", action="store_true", help="no white flashes on downbeats")
    p.add_argument("--print-edl", action="store_true")
    p.add_argument("--edl-out", help="write the shot list to this JSON file")
    p.add_argument("--sensitivity", type=float, default=1.5)
    p.add_argument("--min-bpm", type=float, default=60.0)
    p.add_argument("--max-bpm", type=float, default=200.0)
    p.add_argument("--cache-dir", default=".mvfx")
    p.add_argument("--no-cache", action="store_true")
    add_common(p)
    p.set_defaults(func=cmd_auto)

    # sampler --------------------------------------------------------------
    p = sub.add_parser("sampler", help="render a grid comparing several looks")
    p.add_argument("input")
    p.add_argument("--presets", default=None, help="comma separated look names")
    p.add_argument("--cols", type=int, default=3)
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--duration", type=float, default=3.0)
    p.add_argument("--music")
    p.add_argument("--music-start", type=float, default=0.0)
    add_common(p)
    p.set_defaults(func=cmd_sampler)

    # scenes ---------------------------------------------------------------
    p = sub.add_parser("scenes", help="generate procedural footage to practise on")
    p.add_argument("--out", default="out/scenes")
    p.add_argument("--names", nargs="*", default=None)
    p.add_argument("--size", type=parse_size, default="hd")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--duration", type=float, default=8.0)
    p.set_defaults(func=cmd_scenes)

    # demo -----------------------------------------------------------------
    p = sub.add_parser("demo", help="end-to-end demo: procedural footage + bundled music")
    p.add_argument("--out", default="out")
    p.add_argument("--music", default=None)
    p.add_argument("--music-start", type=float, default=0.0)
    p.add_argument("--duration", type=float, default=40.0)
    p.add_argument("--scene-duration", type=float, default=8.0)
    p.add_argument("--sampler-duration", type=float, default=4.0)
    p.add_argument("--sampler-presets", default=None)
    p.add_argument("--names", nargs="*", default=None)
    p.add_argument("--intensity", type=float, default=0.75)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--transition", default="cut", choices=TRANSITIONS)
    p.add_argument("--transition-dur", type=float, default=0.0)
    p.add_argument("--skip-scenes", action="store_true")
    p.add_argument("--skip-sampler", action="store_true")
    p.add_argument("--print-edl", action="store_true")
    p.add_argument("--sensitivity", type=float, default=1.5)
    p.add_argument("--min-bpm", type=float, default=60.0)
    p.add_argument("--max-bpm", type=float, default=200.0)
    p.add_argument("--cache-dir", default=".mvfx")
    p.add_argument("--no-cache", action="store_true")
    add_common(p)
    p.set_defaults(func=cmd_demo, output="out/music_video.mp4")

    # lyrics ----------------------------------------------------------------
    p = sub.add_parser("lyrics", help="build a captioned lyrics video")
    p.add_argument("--music", required=True)
    p.add_argument("--lyrics", help="plain text, LRC or SRT file")
    p.add_argument("--style", default="karaoke", choices=list(STYLES))
    p.add_argument("--font", default="DejaVu Sans")
    p.add_argument("--font-size", type=int, default=None)
    p.add_argument("--fonts-dir", default=None)
    p.add_argument("--highlight", default="#FFD400", help="colour of the sung word")
    p.add_argument("--text-colour", default="#FFFFFF", help="colour of the un-sung word")
    p.add_argument("--outline", type=float, default=3.0)
    p.add_argument("--shadow", type=float, default=2.0)
    p.add_argument("--margin", type=float, default=0.09, help="bottom margin as a fraction of height")
    p.add_argument("--max-chars", type=int, default=30, help="wrap long lines at this width")
    p.add_argument("--title", default=None, help="title card shown over the first seconds")
    p.add_argument("--subtitle", default=None, help="second line of the title card")
    p.add_argument("--title-duration", type=float, default=4.0)
    p.add_argument("--lyrics-start", type=float, default=None, help="when the first line appears")
    p.add_argument("--beats-per-line", type=int, default=8)
    p.add_argument("--min-beats", type=int, default=4)
    p.add_argument("--gap-beats", type=float, default=0.0)
    p.add_argument("--words-per-second", type=float, default=2.4)
    p.add_argument("--print-sheet", action="store_true")
    p.add_argument("--ass-out", default=None, help="where to write the generated .ass file")
    p.add_argument("--background", nargs="*", default=None, help="your own clips to cut to")
    p.add_argument("--scene-dir", default="out/scenes")
    p.add_argument("--scenes", nargs="*", default=None)
    p.add_argument("--scene-duration", type=float, default=8.0)
    p.add_argument("--skip-scenes", action="store_true")
    p.add_argument("--music-start", type=float, default=0.0)
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--intensity", type=float, default=0.55)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--transition", default="cut", choices=TRANSITIONS)
    p.add_argument("--transition-dur", type=float, default=0.0)
    p.add_argument("--no-flash", action="store_true")
    p.add_argument("--print-edl", action="store_true")
    p.add_argument("--sensitivity", type=float, default=1.5)
    p.add_argument("--min-bpm", type=float, default=60.0)
    p.add_argument("--max-bpm", type=float, default=200.0)
    p.add_argument("--cache-dir", default=".mvfx")
    p.add_argument("--no-cache", action="store_true")
    add_common(p)
    p.set_defaults(func=cmd_lyrics)

    # check ----------------------------------------------------------------
    p = sub.add_parser("check", help="photosensitive-epilepsy flash check")
    p.add_argument("input")
    p.set_defaults(func=cmd_check)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "presets", None) and isinstance(args.presets, str):
        args.presets = [p.strip() for p in args.presets.split(",") if p.strip()]
    try:
        return args.func(args)
    except FFmpegNotFound:
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
