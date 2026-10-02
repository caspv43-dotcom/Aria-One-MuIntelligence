#!/usr/bin/env python3
"""Serve the rendered videos so you can watch them in a browser.

Why not ``python3 -m http.server``? Because it ignores HTTP ``Range`` headers,
so browsers cannot seek: a 90 MB mp4 has to download in full before it plays
and the scrubber is dead. This server implements Range properly.

    python3 scripts/watch.py                 # serves ./out on port 8000
    python3 scripts/watch.py --port 9000     # pick a port
    python3 scripts/watch.py --dir out

An index page listing every video (with the lyric timing sheet next to it) is
generated automatically.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".m4v", ".webm", ".mov"}


# --------------------------------------------------------------------------- #
# index page
# --------------------------------------------------------------------------- #
def _ass_to_rows(ass_path: Path) -> list[tuple[float, float, str]]:
    """Pull (start, end, text) out of an ASS file, dropping the override tags."""
    rows: list[tuple[float, float, str]] = []
    if not ass_path.is_file():
        return rows
    stamp = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,2})")
    for line in ass_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) < 10:
            continue

        def seconds(value: str) -> float:
            m = stamp.match(value.strip())
            if not m:
                return 0.0
            h, mnt, s, frac = m.groups()
            return int(h) * 3600 + int(mnt) * 60 + int(s) + int(frac) / (10 ** len(frac))

        text = re.sub(r"\{[^}]*\}", "", fields[9])
        text = text.replace("\\N", " / ").replace("\\n", " / ").strip()
        style = fields[3].strip()
        rows.append((seconds(fields[1]), seconds(fields[2]), f"[{style}] {text}" if style != "Lyrics" else text))
    return rows


def build_index(directory: Path) -> Path:
    """Write an index page listing every video in ``directory``."""
    videos = sorted(p for p in directory.glob("*") if p.suffix.lower() in VIDEO_SUFFIXES)
    videos = [v for v in videos if not v.name.startswith(".")]
    cards = []
    for video in videos:
        ass = video.with_suffix(".ass")
        rows = _ass_to_rows(ass)
        sheet = ""
        if rows:
            items = "".join(
                f'<li><span class="t">{int(start // 60):02d}:{start % 60:05.2f}</span>'
                f"{html.escape(text)}</li>"
                for start, _end, text in rows
            )
            sheet = f'<details open><summary>timing sheet ({len(rows)} lines)</summary><ol class="sheet">{items}</ol></details>'

        size_mb = video.stat().st_size / 1048576
        cards.append(
            f"""<section class="card">
  <h2>{html.escape(video.stem.replace('_', ' '))}</h2>
  <video controls preload="metadata" playsinline src="{html.escape(video.name)}"></video>
  <p class="meta">{html.escape(video.name)} &middot; {size_mb:.1f} MB
     &middot; <a href="{html.escape(video.name)}" download>download</a>
     {f'&middot; <a href="{html.escape(ass.name)}" download>captions (.ass)</a>' if ass.is_file() else ''}</p>
  {sheet}
</section>"""
        )

    body = "\n".join(cards) or "<p>No videos in this folder yet. Render one with <code>mvfx lyrics</code>.</p>"
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mvfx renders</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; padding:32px 24px 64px; background:#0c0c10; color:#e8e8ef;
         font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  h1 {{ font-size:20px; margin:0 0 4px; letter-spacing:.02em; }}
  .note {{ color:#9a9aae; margin:0 0 28px; max-width:70ch; }}
  .grid {{ display:grid; gap:28px; grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); }}
  .card {{ background:#14141b; border:1px solid #262633; border-radius:12px; padding:16px 18px 18px; }}
  h2 {{ font-size:15px; margin:0 0 12px; text-transform:capitalize; color:#cfcfe0; }}
  video {{ width:100%; border-radius:8px; background:#000; display:block; }}
  video.portrait {{ max-height:70vh; width:auto; margin:0 auto; }}
  .meta {{ color:#8b8b9e; font-size:12.5px; margin:10px 0 0; }}
  .meta a {{ color:#9ecbff; }}
  details {{ margin-top:14px; }}
  summary {{ cursor:pointer; color:#9ecbff; font-size:13px; }}
  ol.sheet {{ max-height:230px; overflow:auto; margin:10px 0 0; padding-left:0; list-style:none;
             font-size:12.5px; color:#c9c9d8; }}
  ol.sheet li {{ padding:2px 0; border-bottom:1px solid #1e1e28; }}
  .t {{ display:inline-block; width:62px; color:#6f7f9c; font-variant-numeric:tabular-nums; }}
  code {{ background:#1c1c26; padding:1px 5px; border-radius:4px; }}
</style>
</head>
<body>
<h1>mvfx renders</h1>
<p class="note">Every video in <code>{html.escape(str(directory))}</code>. Captions are burned in
&mdash; the timing sheet below each player shows when each line lands.</p>
<div class="grid">
{body}
</div>
<script>
  // nudge portrait clips into a narrower column so they stay watchable
  document.querySelectorAll('video').forEach(v => {{
    v.addEventListener('loadedmetadata', () => {{
      if (v.videoHeight > v.videoWidth) v.classList.add('portrait');
    }});
  }});
</script>
</body>
</html>
"""
    target = directory / "index.html"
    target.write_text(page, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# range-capable server
# --------------------------------------------------------------------------- #
class _Slice:
    """Read-only view of a file object limited to ``length`` bytes."""

    def __init__(self, handle, length: int) -> None:
        self._handle = handle
        self._left = length

    def read(self, amount: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        if amount is None or amount < 0 or amount > self._left:
            amount = self._left
        data = self._handle.read(amount)
        self._left -= len(data)
        return data

    def close(self) -> None:
        try:
            self._handle.close()
        except Exception:
            pass


class RangeHandler(SimpleHTTPRequestHandler):
    """Static handler that honours ``Range`` requests so video can be seeked."""

    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".ass": "text/plain"}

    def send_head(self) -> object:  # noqa: D102 - stdlib signature
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            handle = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = os.fstat(handle.fileno()).st_size
        content_type = self.guess_type(path)
        header = self.headers.get("Range")
        match = re.match(r"bytes=(\d*)-(\d*)$", header.strip()) if header else None

        if match:
            start_text, end_text = match.groups()
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
            else:
                # suffix range: bytes=-500 means "the last 500 bytes"
                length = int(end_text) if end_text else 0
                start = max(0, size - length)
                end = size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                handle.close()
                self.send_error(416, "Requested Range Not Satisfiable")
                return None
            handle.seek(start)
            self.send_response(206)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return _Slice(handle, end - start + 1)

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        return handle

    protocol_version = "HTTP/1.1"  # keep-alive: a player opens many ranges

    def log_message(self, fmt: str, *args) -> None:  # quieter default log
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))


def serve(directory: Path, port: int, host: str = "0.0.0.0") -> None:
    handler = partial(RangeHandler, directory=str(directory))
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    print(f"serving {directory} on http://{host}:{port}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="out", help="directory to serve (default: out)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--no-index", action="store_true", help="do not (re)generate index.html")
    args = parser.parse_args(argv)

    directory = Path(args.dir).resolve()
    if not directory.is_dir():
        print(f"no such directory: {directory}", file=sys.stderr)
        return 1
    if not args.no_index:
        index = build_index(directory)
        print(f"index: {index}")
    serve(directory, args.port, args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
