"""FFmpeg discovery, media probing and process execution for mvfx.

Everything mvfx does ends up as an ffmpeg command line.  This module owns the
three boring-but-critical pieces:

* :func:`find_ffmpeg`   - locating a usable ffmpeg binary
* :func:`probe`         - reading media metadata (size, fps, duration, codecs)
* :func:`run`           - running ffmpeg with sane defaults and useful errors

No third party dependencies: mvfx runs on a bare Python 3.8+ install.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "FFmpegNotFound",
    "MediaInfo",
    "find_ffmpeg",
    "ensure_ffmpeg",
    "probe",
    "run",
    "default_tools_dir",
]

#: Where mvfx looks for / installs its bundled ffmpeg.  Override with $MVFX_TOOLS.
default_tools_dir = Path(os.environ.get("MVFX_TOOLS", str(Path.home() / "tools")))

_BOOTSTRAP_HINT = """\
No ffmpeg binary found.

Install one automatically (downloads a static build from PyPI):

    python3 scripts/get_ffmpeg.py

...or point mvfx at an existing binary:

    export MVFX_FFMPEG=/usr/bin/ffmpeg
"""


class FFmpegNotFound(RuntimeError):
    """Raised when no ffmpeg binary can be located."""


@dataclass
class MediaInfo:
    """Summary of a media file, as reported by ffprobe/ffmpeg."""

    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    nb_frames: int = 0
    has_video: bool = False
    has_audio: bool = False
    vcodec: str = ""
    acodec: str = ""
    sample_rate: int = 0
    size_bytes: int = 0

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        bits = [self.path]
        if self.has_video:
            bits.append(f"{self.width}x{self.height}@{self.fps:.2f}fps")
        if self.has_audio:
            bits.append(f"{self.acodec or 'audio'}@{self.sample_rate}Hz")
        bits.append(f"{self.duration:.2f}s")
        return "  ".join(bits)


# --------------------------------------------------------------------------- #
# Binary discovery
# --------------------------------------------------------------------------- #
def find_ffmpeg(explicit: str | None = None) -> str | None:
    """Return a path to an ffmpeg binary, or ``None`` if none can be found.

    Resolution order:

    1. ``explicit`` argument (CLI ``--ffmpeg``)
    2. ``$MVFX_FFMPEG`` environment variable
    3. a binary bundled by :mod:`scripts.get_ffmpeg` under ``$MVFX_TOOLS``
    4. ``imageio_ffmpeg`` if it happens to be installed
    5. ``ffmpeg`` on ``$PATH``
    """
    candidates: list[str] = []
    for value in (explicit, os.environ.get("MVFX_FFMPEG")):
        if value:
            candidates.append(value)

    bundled = sorted(default_tools_dir.glob("imageio_ffmpeg/binaries/ffmpeg-*"))
    candidates.extend(str(p) for p in bundled)

    try:  # pragma: no cover - depends on environment
        import imageio_ffmpeg  # type: ignore

        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass

    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)

    for cand in candidates:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return os.path.abspath(cand)
    return None


def ensure_ffmpeg(explicit: str | None = None) -> str:
    """Like :func:`find_ffmpeg` but exits with a helpful message on failure."""
    path = find_ffmpeg(explicit)
    if path is None:
        sys.stderr.write(_BOOTSTRAP_HINT)
        raise FFmpegNotFound("ffmpeg not found")
    return path


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def _parse_rate(value: str | None) -> float:
    """Convert ``"30000/1001"`` / ``"30"`` / ``"0/0"`` into a float."""
    if not value:
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            num_f, den_f = float(num), float(den)
            return num_f / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def probe(path: str | Path, ffmpeg: str | None = None) -> MediaInfo:
    """Return :class:`MediaInfo` for ``path``.

    Uses ``ffprobe`` when available and falls back to parsing the banner that
    ffmpeg prints for the input (which works even for lavfi/virtual inputs).
    """
    path = str(path)
    info = MediaInfo(path=path)
    try:
        info.size_bytes = os.path.getsize(path)
    except OSError:
        info.size_bytes = 0

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        binary = ensure_ffmpeg(ffmpeg)
        maybe = Path(binary).with_name("ffprobe")
        ffprobe = str(maybe) if maybe.is_file() else None

    raw: dict | None = None
    if ffprobe:
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if out.returncode == 0 and out.stdout.strip():
                raw = json.loads(out.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError):
            raw = None

    if raw is None:  # fall back to ffmpeg's stderr banner
        binary = ensure_ffmpeg(ffmpeg)
        out = subprocess.run(
            [binary, "-hide_banner", "-i", path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return _probe_from_banner(path, out.stderr, info)

    fmt = raw.get("format", {}) or {}
    try:
        info.duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        info.duration = 0.0

    for stream in raw.get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            info.has_video = True
            info.vcodec = stream.get("codec_name", "")
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.fps = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(
                stream.get("r_frame_rate")
            )
            info.nb_frames = int(stream.get("nb_frames") or 0)
            if not info.nb_frames and info.fps and info.duration:
                info.nb_frames = int(round(info.fps * info.duration))
        elif codec_type == "audio":
            info.has_audio = True
            info.acodec = stream.get("codec_name", "")
            info.sample_rate = int(stream.get("sample_rate") or 0)
            try:
                info.duration = info.duration or float(stream.get("duration") or 0.0)
            except (TypeError, ValueError):
                pass
    return info


def _probe_from_banner(path: str, stderr: str, info: MediaInfo) -> MediaInfo:
    """Parse the ``Stream #...`` / ``Duration:`` lines ffmpeg prints."""
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", stderr)
    if dur:
        h, m, s = dur.groups()
        info.duration = int(h) * 3600 + int(m) * 60 + float(s)
    for line in stderr.splitlines():
        if "Stream #" not in line:
            continue
        if "Video:" in line:
            info.has_video = True
            res = re.search(r",\s*(\d{2,5})x(\d{2,5})", line)
            if res:
                info.width, info.height = int(res.group(1)), int(res.group(2))
            fps = re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
            if fps:
                info.fps = float(fps.group(1))
        elif "Audio:" in line:
            info.has_audio = True
            sr = re.search(r"(\d{3,6})\s*Hz", line)
            if sr:
                info.sample_rate = int(sr.group(1))
    if info.fps and info.duration:
        info.nb_frames = int(round(info.fps * info.duration))
    return info


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    """Outcome of an :func:`run` call."""

    command: list[str]
    returncode: int
    elapsed: float
    stderr_tail: str = ""
    log_lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


_PROGRESS_RE = re.compile(r"out_time_ms=(\d+)")


def run(
    args: Sequence[str],
    *,
    ffmpeg: str | None = None,
    verbose: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    progress_total: float | None = None,
    check: bool = True,
    env: dict | None = None,
) -> RunResult:
    """Run ffmpeg with ``args`` (the binary itself is prepended).

    Args:
        args: ffmpeg arguments, *without* the program name.
        ffmpeg: explicit binary; otherwise discovered automatically.
        verbose: stream ffmpeg's stderr live.
        dry_run: print the command and exit without running anything.
        progress_total: when given (seconds), print a progress line.
        check: raise :class:`RuntimeError` on non-zero exit.
    """
    binary = ensure_ffmpeg(ffmpeg)
    cmd = [binary, "-hide_banner", "-nostdin", "-y", *map(str, args)]

    printable = " ".join(_quote(part) for part in cmd)
    if dry_run:
        print(printable)
        return RunResult(command=cmd, returncode=0, elapsed=0.0)

    if verbose:
        sys.stderr.write(f"$ {printable}\n")

    child_env = dict(os.environ)
    if env:
        child_env.update(env)

    started = time.time()
    if verbose:
        proc = subprocess.Popen(cmd, env=child_env)
        returncode = proc.wait()
        log: list[str] = []
        stderr_tail = ""
    else:
        proc = subprocess.Popen(
            cmd,
            # stdout must be drained or dropped: filters such as
            # metadata=print write there, and a full pipe would block ffmpeg
            # forever while we sit here reading stderr.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=child_env,
            text=True,
            bufsize=1,
        )
        assert proc.stderr is not None
        log = []
        last_report = 0.0
        for line in proc.stderr:
            log.append(line.rstrip("\n"))
            if progress_total and not quiet:
                match = _PROGRESS_RE.search(line)
                if match:
                    micros = int(match.group(1))
                    seconds = micros / 1_000_000.0
                    now = time.time()
                    if seconds >= last_report + max(progress_total / 20.0, 0.5):
                        last_report = seconds
                        pct = min(100.0, 100.0 * seconds / progress_total) if progress_total else 0
                        sys.stderr.write(
                            f"\r  rendering … {pct:5.1f}%  ({seconds:.1f}s / {progress_total:.1f}s)"
                        )
                        sys.stderr.flush()
        proc.wait()
        if progress_total and not quiet:
            sys.stderr.write("\r  rendering … 100.0%\n")
        returncode = proc.returncode
        stderr_tail = "\n".join(log[-25:])

    elapsed = time.time() - started
    result = RunResult(
        command=cmd,
        returncode=returncode or 0,
        elapsed=elapsed,
        stderr_tail=stderr_tail,
        log_lines=log,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}) after {elapsed:.1f}s:\n"
            f"{printable}\n\n"
            + (stderr_tail or "\n".join(log[-25:]))
        )
    return result


def _quote(part: str) -> str:
    """Shell-quote a single argument for display purposes."""
    if part and not re.search(r"[\s'\"\\$`|&;<>()]", part):
        return part
    return "'" + part.replace("'", "'\\''") + "'"


def has_filter(name: str, ffmpeg: str | None = None) -> bool:
    """Return True if the ffmpeg build provides filter ``name``."""
    binary = ensure_ffmpeg(ffmpeg)
    out = subprocess.run(
        [binary, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=60
    )
    return any(parts[1] == name for parts in (line.split() for line in out.stdout.splitlines()[1:]) if parts)


def filter_names(ffmpeg: str | None = None) -> set[str]:
    """All filter names supported by this ffmpeg build."""
    binary = ensure_ffmpeg(ffmpeg)
    out = subprocess.run(
        [binary, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=60
    )
    names: set[str] = set()
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            names.add(parts[1])
    return names


def version(ffmpeg: str | None = None) -> str:
    binary = ensure_ffmpeg(ffmpeg)
    out = subprocess.run(
        [binary, "-hide_banner", "-version"], capture_output=True, text=True, timeout=60
    )
    first = out.stdout.splitline()[0] if hasattr(out.stdout, "splitline") else ""
    return first or out.stdout.splitlines()[0] if out.stdout else "unknown"
