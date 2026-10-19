"""Build a self-contained wukong app for the current platform.

    python packaging/build.py

Produces:

* **macOS** -- ``dist/wukong.app`` and ``dist/wukong-<version>.dmg``
  (drag-to-Applications layout, with a symlink to ``/Applications``).
* **Windows** -- ``dist/wukong.exe``, a single double-clickable file.

PyInstaller cannot cross-compile: run this on each target OS, or let
``.github/workflows/release.yml`` run it on ``macos-latest`` / ``windows-latest``.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "wukong.spec"
DIST = ROOT / "dist"


def version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def ensure_build_deps() -> None:
    missing = []
    for module, package in (("PyInstaller", "pyinstaller"), ("webview", "pywebview")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        raise SystemExit(
            "missing build dependencies: " + ", ".join(missing) + "\n"
            "install them with:  pip install pyinstaller pywebview"
        )


def build_icon() -> None:
    """Generate ``packaging/AppIcon.icns`` on macOS. Best effort.

    The icon is drawn procedurally by the package itself (stdlib PNG encoder +
    ``iconutil``), so a failure here costs a generic icon, not the build.
    """
    if sys.platform != "darwin":
        return
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from wukong.desktop.bundle import build_icns
    except Exception as exc:  # pragma: no cover - defensive
        print(f"icon: skipped ({type(exc).__name__}: {exc})")
        return
    result = build_icns(ROOT / "packaging")
    if not result:
        print(f"icon: skipped ({result.reason})")
    else:
        print(f"icon: {result.path}")


def clear_previous() -> None:
    """Move the previous artifacts aside instead of letting PyInstaller delete them.

    PyInstaller's COLLECT removes the old output directory, and some
    environments refuse (or prompt for) a recursive delete of a directory with
    hundreds of files. A rename is one filesystem operation, and it leaves the
    previous build recoverable under ``build/previous/``.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside = ROOT / "build" / "previous" / stamp
    for name in ("wukong", "wukong.app"):
        path = DIST / name
        if path.exists():
            aside.mkdir(parents=True, exist_ok=True)
            path.rename(aside / name)


def run_pyinstaller() -> None:
    clear_previous()
    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(SPEC)]
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def sign_app() -> None:
    """Ad-hoc sign the .app so Gatekeeper does not kill it on first open.

    Ad-hoc (`-`) is enough for a locally-built artifact. A real release needs a
    Developer ID certificate and notarization; without it macOS still warns,
    but the app runs.
    """
    if sys.platform != "darwin":
        return
    app = DIST / "wukong.app"
    if not app.exists():
        return
    cmd = ["codesign", "--deep", "--force", "--sign", "-", str(app)]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def make_dmg() -> Path | None:
    if sys.platform != "darwin":
        return None
    app = DIST / "wukong.app"
    if not app.exists():
        raise SystemExit(f"expected {app}; PyInstaller did not produce it")
    dmg = DIST / f"wukong-{version()}.dmg"
    # A staging folder holding the .app and a symlink to /Applications is the
    # drag-to-install layout every macOS user recognises.
    with tempfile.TemporaryDirectory(prefix="wukong-dmg-") as tmp:
        stage = Path(tmp) / "wukong"
        stage.mkdir()
        shutil.copytree(app, stage / "wukong.app", symlinks=True)
        (stage / "Applications").symlink_to("/Applications")
        cmd = [
            "hdiutil",
            "create",
            "-volname",
            "wukong",
            "-srcfolder",
            str(stage),
            "-ov",
            "-format",
            "UDZO",
            str(dmg),
        ]
        print("+", " ".join(cmd))
        subprocess.run(cmd, check=True)
    return dmg


def report() -> None:
    print("\nartifacts:")
    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates = [DIST / "wukong.app", DIST / f"wukong-{version()}.dmg"]
    elif sys.platform == "win32":
        candidates = [DIST / "wukong.exe", DIST / "wukong" / "wukong.exe"]
    else:
        candidates = [DIST / "wukong" / "wukong"]
    found = [p for p in candidates if p.exists()]
    for path in found:
        print(f"  {path}")
    if not found:
        print("  (none -- check the PyInstaller output above)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-deps-check",
        action="store_true",
        help="Do not verify pyinstaller/pywebview are importable first.",
    )
    parser.add_argument(
        "--sign",
        action="store_true",
        help="Ad-hoc sign the macOS .app before sealing the dmg.",
    )
    args = parser.parse_args(argv)
    if not args.skip_deps_check:
        ensure_build_deps()
    build_icon()
    run_pyinstaller()
    if args.sign:
        sign_app()
    make_dmg()
    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
