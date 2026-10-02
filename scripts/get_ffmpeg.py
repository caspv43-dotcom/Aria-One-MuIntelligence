#!/usr/bin/env python3
"""Download a static ffmpeg build into $MVFX_TOOLS (default: ~/tools).

mvfx needs ffmpeg with libx264, libass and the usual filter set.  Rather than
depending on a distro package, this fetches the well-known static build that
ships inside the ``imageio-ffmpeg`` wheel on PyPI - no root, no compilation.

    python3 scripts/get_ffmpeg.py
"""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path

PYPI = "https://pypi.org/pypi/imageio-ffmpeg/json"
WHEEL = "imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", default=os.environ.get("MVFX_TOOLS", str(Path.home() / "tools")))
    parser.add_argument("--version-pin", default=WHEEL, help="wheel filename to fetch")
    args = parser.parse_args(argv)

    tools = Path(args.tools).expanduser()
    tools.mkdir(parents=True, exist_ok=True)

    print(f"looking up {args.version_pin} on PyPI …")
    with urllib.request.urlopen(PYPI, timeout=60) as response:
        data = json.load(response)
    urls = [u for u in data.get("urls", []) if u["filename"] == args.version_pin]
    if not urls:
        available = [u["filename"] for u in data.get("urls", [])]
        print("wheel not found. available:", *available, sep="\n  ", file=sys.stderr)
        return 1
    url = urls[0]["url"]

    print(f"downloading {url} …")
    with urllib.request.urlopen(url, timeout=300) as response:
        payload = response.read()

    print(f"extracting into {tools} …")
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.extractall(tools)

    binaries = sorted(tools.glob("imageio_ffmpeg/binaries/ffmpeg-*"))
    if not binaries:
        print("no ffmpeg binary found in the wheel", file=sys.stderr)
        return 1
    binary = binaries[0]
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"\nffmpeg ready: {binary}")
    print("mvfx will find it automatically, or set:  export MVFX_FFMPEG=" + str(binary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
