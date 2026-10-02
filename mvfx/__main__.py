"""Allow ``python3 -m mvfx …``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
