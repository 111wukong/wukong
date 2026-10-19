"""Entry point for the frozen desktop app.

PyInstaller runs this as `__main__`. It is a thin wrapper rather than pointing
the build at `wukong/desktop/__main__.py` for one reason: a double-clicked app
starts with a working directory of `/` (macOS) or the install folder (Windows),
and `load_settings(workspace=None)` would then adopt that as the workspace --
a directory the app cannot write, and one whose permission fence would reject
every tool call. Fall back to the user's home when the cwd is not a place a
person would have chosen.
"""

from __future__ import annotations

import os
from pathlib import Path


def _usable_workspace() -> bool:
    cwd = Path.cwd()
    return cwd != Path("/") and os.access(cwd, os.W_OK)


def main() -> int:
    if not _usable_workspace():
        os.chdir(Path.home())
    from wukong.desktop.__main__ import main as desktop_main

    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
