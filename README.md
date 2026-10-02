# Aria-One-MuIntelligence

`mvfx` — a small, dependency-free music-video toolkit built on ffmpeg, plus a
finished **lyrics video** rendered from the track already in this repo
(`Bounce_174954917.mp3`).

Everything runs on a bare Python 3.8+ install. No numpy, no pip packages, no
GPU — just ffmpeg.

---

## The lyrics video

```bash
python3 -m mvfx lyrics \
    --music Bounce_174954917.mp3 \
    --lyrics lyrics/bounce.txt \
    --title "BOUNCE" \
    --subtitle "placeholder lyrics - swap in your own words" \
    --style karaoke --gap-beats 2 \
    --size fhd --fps 30 \
    -o out/lyrics_video.mp4
```

`out/lyrics_video.mp4` — 1920×1080, the full 2:57 track, cut on the beat
(115.4 BPM detected), with word-by-word karaoke captions.

### ⚠️ The words are placeholders

`Bounce_174954917.mp3` is an **instrumental** production track — there are no
vocal stems and no official lyrics to transcribe. The words in
`lyrics/bounce.txt` are original filler written to fit its 115 BPM groove, so
you have something to watch. Swap in the real words whenever you like:

```bash
# edit the lyric sheet, then re-render (takes one command)
$EDITOR lyrics/bounce.txt
python3 -m mvfx lyrics --music Bounce_174954917.mp3 --lyrics lyrics/bounce.txt \
    --title "BOUNCE" -o out/lyrics_video.mp4
```

Three input formats are understood, picked automatically:

| format | what it looks like | timing |
| --- | --- | --- |
| plain text | one lyric line per line, blank line = one-bar rest | **auto**, laid onto the beat grid |
| LRC | `[00:12.50]Morning light on the boulevard` | uses your line times |
| enhanced LRC | `<00:12.50>Morning <00:13.10>light …` | uses your **per-word** times |
| SRT | `00:00:12,500 --> 00:00:15,000` | uses your line times |

Caption styles: `--style karaoke` (word sweep, default) · `pop` · `fade` ·
`typewriter` (per-character sweep).

Useful switches:

```bash
--size vertical          # 1080x1920 for Shorts / Reels / TikTok
--background clips/*.mp4 # cut your own footage instead of the procedural stuff
--highlight "#FF3D81"    # colour of the sung word
--text-colour "#FFFFFF"  # colour of the un-sung word
--max-chars 24           # narrower wrapping
--print-sheet            # dump the timing sheet so you can check the sync
--dry-run                # print the ffmpeg command instead of rendering
```

---

## Install

mvfx needs an ffmpeg with `libx264` and `libass`. If yours is missing either,
grab a static build (no root required):

```bash
python3 scripts/get_ffmpeg.py     # downloads into ~/tools (or $MVFX_TOOLS)
export MVFX_FFMPEG=/usr/bin/ffmpeg   # ...or point at an existing binary
```

Nothing else to install: `python3 -m mvfx ls` should work immediately.

---

## Commands

| command | what it does |
| --- | --- |
| `mvfx ls` | list every effect, look, transition and procedural scene |
| `mvfx beats TRACK` | detect BPM, beat grid and onsets |
| `mvfx apply CLIP` | grade / effect a single clip |
| `mvfx auto CLIPS… --music` | cut a pile of clips into a beat-synced music video |
| `mvfx lyrics --music --lyrics` | **captioned lyrics video** (see above) |
| `mvfx sampler CLIP` | grid comparing several looks on the same footage |
| `mvfx scenes` | generate procedural footage to practise on |
| `mvfx demo` | end-to-end demo: procedural footage + bundled music |
| `mvfx check VIDEO` | flashing / photosensitive-epilepsy warning |
| `mvfx info` | which ffmpeg build and filters are available |

Common options: `--size` (`hd`, `fhd`, `vertical`, `square`, `1920x1080`),
`--fps`, `--crf`, `--speed` (encoder preset), `--codec`, `--dry-run`, `-v`.

### Grade a clip

```bash
python3 -m mvfx apply clip.mp4 \
    -e 'preset:teal_orange+bloom(sigma=22)+grain(level=10)' \
    -o graded.mp4
```

### Cut a music video to the beat

```bash
python3 -m mvfx auto takes/*.mp4 --music Bounce_174954917.mp3 \
    --look neon_night --transition dissolve --transition-dur 0.4 \
    --intensity 0.8 --size fhd -o video.mp4
```

`auto` reads the track's loudness curve: loud passages get 1–2 beat shots,
quiet ones get 4–8 beat shots, downbeats get a white flash, and accents
(slow motion, reverse, stutter, freeze, speed ramp) are rolled for ~10% of
shots so they stay accents. Seeded by `--seed`, so the same command always
produces the same edit.

### Compare looks side by side

```bash
python3 -m mvfx sampler clip.mp4 --presets clean,cinematic,vhs_retro,glitch_hop \
    --cols 2 -o grid.mp4
```

---

## Effects

49 effects in 7 stages, composed automatically in the right order
(geometry → grade → blend → artifact → light → frame → text) whatever order you
list them in. Full catalogue with parameters: **[docs/EFFECTS.md](docs/EFFECTS.md)**.

The music-video staples are all there:

| | |
| --- | --- |
| **cutting** | beat-synced cuts, flash frames, strobe, whip dissolves (`--transition`) |
| **camera** | `zoom_punch`, `zoom_pulse`, `drift`, `shake`, `slowmo`, `speed_ramp`, `reverse`, `freeze`, `stutter`, `spin` |
| **grade** | `teal_orange`, `cinematic_grade`, `neon_night`, `sunset_warm`, `mono`, `bleach_bypass`, `crush_blacks`, `posterize`, `curves_preset` |
| **light** | `bloom`, `halation`, `soft_focus`, `light_leak`, `lens_flare`, `film_burn`, `vignette` |
| **glitch / retro** | `chromatic`, `glitch_blocks`, `scanlines`, `chroma_bleed`, `tracking_band`, `dust_scratches`, `grain` |
| **space** | `mirror`, `kaleidoscope`, `letterbox`, `edge_glow`, `scrim` |

Ready-made looks (`--preset`): `teal_orange`, `cinematic`, `blockbuster`,
`neon_night`, `vhs_retro`, `glitch_hop`, `mono_noir`, `sunset_dream`,
`dream_trails`, `festival_strobe`, `psychedelic`, `lyrics_stage`, `clean`.

Effects take parameters inline — `bloom(sigma=22:opacity=0.6)` — and combine
with `+`:

```bash
-e 'teal_orange(intensity=0.9)+halation(sigma=24)+grain(level=8)+letterbox(ratio=2.39)'
```

---

## How it works

```
music ─► beats.py ─┐   band-split energy envelopes ─► onsets, BPM, beat grid
                   │                                        │
clips ─► edl.py ───┴────────────────────────────────────────┤
                     shot list (when/where/what effect)     │
                                                            ▼
lyrics ─► lyrics.py ──► ASS karaoke ──► render.py ──► one ffmpeg filter graph ──► mp4
```

* **`mvfx/beats.py`** — splits audio into kick / body / sparkle bands with
  ffmpeg, builds ~100 Hz energy envelopes, takes log-compressed differences as
  a novelty function, peak-picks onsets against a local threshold, then
  autocorrelates to find the tempo and fits a beat grid by phase search.
  Pure stdlib, ~2 s for a 3-minute track, cached in `.mvfx/`.
* **`mvfx/edl.py`** — turns beats + clips into a shot list.
* **`mvfx/effects.py`** — each effect is a function that emits filter-graph
  nodes; stages keep the chain in a sane order.
* **`mvfx/render.py`** — assembles the graph (conform → split → per-shot trim +
  effects → concat/xfade → global look → captions) and runs ffmpeg.
* **`mvfx/lyrics.py`** — parses / times lyrics and writes ASS with `\kf`
  karaoke tags.

Add your own effect in three lines:

```python
@effect("my_look", stage=STAGE_GRADE, help="…", amount=0.5)
def my_look(ctx, p, inp, out):
    return [f"[{inp}]hue=s={p.f('amount', 0.5):.2f}[{out}]"]
```

It is then available to every command as `-e 'my_look(amount=0.8)'`.

---

## Repo layout

```
mvfx/            the toolkit (beats, edl, effects, render, lyrics, scenes, cli)
lyrics/          lyric sheets (plain text / LRC / SRT)
assets/fonts/    DejaVu fonts so libass never needs fontconfig
scripts/         get_ffmpeg.py (bootstrap), gen_effects_doc.py (docs)
docs/            EFFECTS.md, LYRICS.md
out/             rendered videos + generated .ass (gitignored)
.mvfx/           beat-analysis cache (gitignored)
```

## Notes

* Rendering is CPU-bound; a 3-minute 1080p cut takes roughly 10–15 minutes on
  two cores. Use `--size hd --fps 24 --duration 30` while iterating, then
  render for real.
* `mvfx check video.mp4` measures luminance jumps and warns if the flashing
  rate passes 3 Hz — worth running before publishing anything with
  `strobe` / `flash` in it.
* Licensed fonts: DejaVu (Bitstream Vera / public-domain-style licence), see
  `assets/fonts/`.
