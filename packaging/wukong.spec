# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the wukong desktop app.

`python packaging/build.py` invokes this; `pyinstaller --clean packaging/wukong.spec`
works too.

Two facts shape it:

* The console is static files at ``wukong/api/console``; ``api/app.py`` resolves
  them with ``Path(__file__).parent / "console"``, so they must land at that
  same relative path inside the bundle.
* ``webview`` is imported lazily inside ``launcher._webview()``, so PyInstaller's
  static analysis cannot see it. It is collected explicitly, together with
  uvicorn's dynamically-imported protocol/loop modules.

The platform branch is deliberate: a macOS ``.app`` needs an onedir layout,
while a Windows user expects a single double-clickable ``.exe``.
"""

import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
ICON = ROOT / "packaging" / "AppIcon.icns"

try:
    VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
except Exception:  # pragma: no cover - a broken pyproject is a build-time error anyway
    VERSION = "0.0.0"


def _collect(package: str):
    """collect_all, but tolerant of an optional package not being installed."""
    try:
        return collect_all(package)
    except Exception:
        return ([], [], [])


webview_datas, webview_binaries, webview_hidden = _collect("webview")
vec_datas, vec_binaries, vec_hidden = _collect("sqlite_vec")

datas = [(str(ROOT / "src" / "wukong" / "api" / "console"), "wukong/api/console")]
datas += webview_datas + vec_datas
binaries = webview_binaries + vec_binaries

hiddenimports = (
    collect_submodules("uvicorn")
    + webview_hidden
    + vec_hidden
    + ["wukong.api.app", "wukong.desktop.launcher", "wukong.desktop.__main__"]
)

a = Analysis(
    [str(ROOT / "packaging" / "entry_desktop.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # tkinter is not used (pywebview uses the OS webview); pytest/tests are not
    # shipped. Dropping them keeps the bundle from carrying a test runner.
    excludes=["tkinter", "pytest", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "win32":
    # One file: a user double-clicks `wukong.exe`. Datas are extracted to
    # sys._MEIPASS at startup, which is where `Path(__file__).parent` points.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="wukong",
        console=False,
        upx=False,
        debug=False,
        strip=False,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="wukong",
        console=False,
        upx=False,
        debug=False,
        strip=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        name="wukong",
        upx=False,
        strip=False,
    )
    if sys.platform == "darwin":
        bundle_kwargs = {
            "name": "wukong.app",
            "bundle_identifier": "dev.wukong.agent",
            "info_plist": {
                "CFBundleDisplayName": "wukong",
                "CFBundleShortVersionString": VERSION,
                "CFBundleVersion": VERSION,
                "LSMinimumSystemVersion": "11.0",
                "NSHighResolutionCapable": True,
                "NSHumanReadableCopyright": "MIT licensed.",
            },
        }
        if ICON.exists():
            bundle_kwargs["icon"] = str(ICON)
        app = BUNDLE(coll, **bundle_kwargs)
