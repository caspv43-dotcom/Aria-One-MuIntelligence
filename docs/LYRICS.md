# Lyrics videos

`mvfx lyrics` turns an audio track + a lyric sheet into a captioned video.
This page explains how the timing, the karaoke sweep and the typography work,
and how to get exactly the look you want.

## The one command

```bash
python3 -m mvfx lyrics --music song.mp3 --lyrics lyrics/song.txt \
    --title "SONG TITLE" --subtitle "artist name" \
    -o out/lyrics_video.mp4
```

Under the hood:

1. **analyse** the track (`mvfx/beats.py`) → BPM, beat grid, loudness curve
2. **parse** the lyric file
3. **time** the lines (yours, or laid onto the beat grid)
4. **split** each line into per-word slices → ASS `\kf` karaoke tags
5. **plan** a beat-synced cut of the background footage (`mvfx/edl.py`)
6. **render** one ffmpeg graph: footage → look → scrim → captions → music

## Input formats

### Plain text — timing is generated

```
# comments and [Section] headers are ignored
# a blank line = a one-bar rest

[Verse 1]
Morning light on the boulevard
Concrete drums inside my head

[Chorus]
Bounce with me, don't need a reason
```

Each line is given a whole number of beats, estimated from its word count at
`--words-per-second` (default 2.4 ≈ a comfortable rap/sing rate), then clamped
to `--min-beats … --beats-per-line` (4 and 8 by default). At 115 BPM a beat is
0.52 s, so a 4-beat line lasts ~2.1 s.

The first line starts where the track first gets loud
(`lyrics._guess_vocal_entry`) — usually just after the intro — or at
`--lyrics-start` if you'd rather pin it.

If the words run out before the music does, `mvfx lyrics` tells you the last
line's end time; add `--gap-beats 2` to spread the lines out with musical
rests, or add more lyrics.

### LRC — line timing is yours

```
[ti:Bounce]
[00:12.21]Morning light on the boulevard
[00:14.29]Concrete drums inside my head
```

### Enhanced LRC — per-word timing is yours

```
[00:12.21]<00:12.21>Morning <00:12.60>light <00:13.00>on the <00:13.30>boulevard
```

When per-word timestamps exist, the karaoke sweep follows them exactly — this
is the best-quality option if your player exports it.

### SRT — also accepted

```
1
00:00:12,210 --> 00:00:14,290
Morning light on the boulevard
```

## Timing knobs

| flag | default | meaning |
| --- | --- | --- |
| `--lyrics-start` | auto | when the first line appears |
| `--beats-per-line` | 8 | longest a line may be, in beats |
| `--min-beats` | 4 | shortest a line may be, in beats |
| `--gap-beats` | 0 | rest inserted after each line |
| `--words-per-second` | 2.4 | reading speed used to size a line |
| `--duration` | whole track | how much of the track to cut |

Check the result before rendering:

```bash
python3 -m mvfx lyrics --music song.mp3 --lyrics lyrics/song.txt \
    --print-sheet --dry-run -o /tmp/x.mp4
```

```
  1.   12.21 ->   14.29  Morning light on the boulevard
  2.   15.33 ->   17.41  Concrete drums inside my head
```

## Caption styles

`--style` picks how text is revealed:

| style | behaviour |
| --- | --- |
| `karaoke` | left-to-right colour sweep across the line, word by word |
| `typewriter` | the same sweep, character by character |
| `pop` | the line scales down from 118% when it lands |
| `fade` | the line fades in and out |

All four are built from ASS `\kf` / `\t` / `\fad` tags in
`mvfx/lyrics.build_ass()` — no image overlays, so captions stay crisp at any
resolution and can be re-rendered from the `.ass` file by any player.

## Typography

```bash
--font "DejaVu Sans"     # any family libass can find
--fonts-dir assets/fonts # extra directory to search (bundled DejaVu here)
--font-size 84           # absolute, or auto = 5.8% of the height
--highlight "#FFD400"    # colour of the sung part of a word
--text-colour "#FFFFFF"  # colour of the not-yet-sung part
--outline 3 --shadow 2   # stroke and drop shadow, in pixels
--margin 0.09            # distance from the bottom edge (fraction of height)
--max-chars 30           # wrap long lines (max 3 rows)
```

Long lines wrap automatically at word boundaries with `\N`; if a line still
needs more than three rows, shorten it or lower `--max-chars`.

## Backgrounds

With no `--background`, mvfx generates its own abstract footage
(`mvfx/scenes.py`): fractal zooms, cellular automata, smoke, light beams,
spiral washes. Seven 12-second clips, cut on the beat like real footage.

To use your own:

```bash
python3 -m mvfx lyrics --music song.mp3 --lyrics lyrics/song.txt \
    --background footage/*.mp4 -o out/lyrics_video.mp4
```

Anything `mvfx auto` can cut, `mvfx lyrics` can caption.

### Legibility

The `lyrics_stage` look (applied by default) softens and slightly desaturates
the footage, then a `scrim` gradient darkens the bottom of the frame so white
text keeps its contrast. If your footage is very bright or busy:

```bash
-e 'preset:lyrics_stage+scrim(height=0.55:strength=0.75)'
```

## Formats

```bash
--size fhd        # 1920x1080  (default for YouTube)
--size vertical   # 1080x1920  (Shorts / Reels / TikTok)
--size square     # 1080x1080
--fps 30 --crf 20 # encoder knobs
```

For vertical, the caption font size scales with the resolution — but the frame
is narrower, so drop `--max-chars` to ~20.

## Outputs

* `out/lyrics_video.mp4` — the video
* `out/lyrics_video.ass` — the generated subtitle file. Open it in any video
  player, or hand-edit it and re-render with `--ass-out` pointing elsewhere.

## Troubleshooting

**No text appears.** The `subtitles` filter needs a font. mvfx ships DejaVu in
`assets/fonts/` (or install fonts system-wide and pass `--fonts-dir`).
Run with `-v` and look for `Glyph not found` / `fontselect` messages.

**Words land slightly off the beat.** Auto-timing snaps word boundaries to
half-beats, so it can only be as good as the detected grid. Check the tempo
with `mvfx beats song.mp3 --print-beats`; if it reports a half or double tempo,
re-run with `--min-bpm` / `--max-bpm` narrowed around the real one. For exact
sync, use (enhanced) LRC.

**Rendering is slow.** Iterate at `--size hd --fps 24 --duration 30`, then
render the full thing once.
