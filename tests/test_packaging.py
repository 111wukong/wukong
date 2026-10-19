"""The packaging pipeline must not rot.

A broken spec or a moved console directory is a release-time failure. It is
cheap to catch at test time without running PyInstaller (which needs a GUI
toolchain and minutes), so these assert the wiring, not the build.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "wukong.spec"
BUILD = ROOT / "packaging" / "build.py"
ENTRY = ROOT / "packaging" / "entry_desktop.py"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def test_the_spec_is_valid_python() -> None:
    compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec")


def test_the_spec_bundles_the_console_static_files() -> None:
    """`api/app.py` resolves the console with `Path(__file__).parent / "console"`,
    so the spec must place that directory at the same relative path."""
    text = SPEC.read_text(encoding="utf-8")
    assert "api/console" in text
    assert (ROOT / "src" / "wukong" / "api" / "console" / "index.html").is_file()


def test_the_entry_and_build_script_are_valid_python() -> None:
    for path in (BUILD, ENTRY):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_the_release_workflow_builds_both_platforms() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert "macos-latest" in text, "macOS artifact would never be built"
    assert "windows-latest" in text, "the .exe would never be built"
    assert "packaging/build.py" in text
