"""mvfx - a small, dependency-free music-video effects toolkit for ffmpeg.

Quick start::

    python3 -m mvfx ls                      # list every effect and look
    python3 -m mvfx beats song.mp3          # find the BPM and beat grid
    python3 -m mvfx apply clip.mp4 -e preset:cinematic -o graded.mp4
    python3 -m mvfx auto clips/*.mp4 --music song.mp3 -o video.mp4

The library pieces are importable too::

    from mvfx.beats import analyze
    from mvfx.effects import parse_effects
    from mvfx.render import RenderConfig, render
"""

from __future__ import annotations

try:  # pragma: no cover - metadata is optional at runtime
    from importlib.metadata import PackageNotFoundError, version as _version

    __version__ = _version("mvfx")
except Exception:  # pragma: no cover
    __version__ = "0.1.0"

__all__ = ["__version__"]
