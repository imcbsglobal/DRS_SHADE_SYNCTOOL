"""
build.py  —  Builds DRSSync into a standalone Windows executable.

Usage:
    python build.py          <- build the exe
    python build.py --clean  <- clean build artifacts then build

Output:
    DRS_SYNC\DRSSync.exe     <- the final executable
    DRS_SYNC\config.json     <- edit database name, place next to exe
"""

import os
import sys
import shutil
import subprocess
import json
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
DIST_DIR  = BASE_DIR / "DRS_SYNC"
BUILD_DIR = BASE_DIR / "build"
APP_NAME  = "DRSSync"
ENTRY     = BASE_DIR / "app.py"

# ──────────────────────────────────────────────────────────────────────────

def banner():
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║          DRS Sync Tool  —  build.py              ║")
    print("╚══════════════════════════════════════════════════╝")
    print()


def step(msg):
    print(f"\n[Step] {msg}")


def ok(msg):
    print(f"  ✓  {msg}")


def warn(msg):
    print(f"  ⚠  {msg}")


def fail(msg):
    print(f"  ✗  {msg}")
    sys.exit(1)


# ── Dependency checks ──────────────────────────────────────────────────────

def install_deps():
    step("Installing / verifying dependencies …")
    pkgs = ["pyodbc", "Pillow>=10.0.0", "plyer", "pyinstaller>=6.0.0"]
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install"] + pkgs,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ok("All packages ready.")


def check_entry():
    if not ENTRY.exists():
        fail(f"app.py not found at {ENTRY}")
    ok(f"Entry point: {ENTRY.name}")


# ── Optional: build a proper multi-resolution ICO from PNG ────────────────

def build_ico_from_png(png_src: Path, ico_dst: Path) -> bool:
    """
    Build a proper multi-resolution ICO from a PNG using only Pillow.
    Generates sizes 16, 32, 48, 64, 256 so Windows shows a crisp icon
    at every zoom level. No numpy required.
    """
    import struct, io
    try:
        from PIL import Image
    except ImportError:
        warn("Pillow not installed — cannot rebuild ICO")
        return False

    try:
        src = Image.open(str(png_src)).convert("RGBA")

        sizes = [16, 32, 48, 64, 256]
        images_data = []
        for size in sizes:
            img = src.resize((size, size), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            images_data.append((size, buf.getvalue()))

        # Build ICO file: ICONDIR + ICONDIRENTRYs + raw PNG blobs
        count  = len(images_data)
        header = struct.pack("<HHH", 0, 1, count)
        offset = 6 + count * 16
        entries = b""
        data    = b""
        for size, img_bytes in images_data:
            w = h = 0 if size == 256 else size   # 0 means 256 in ICO spec
            entries += struct.pack(
                "<BBBBHHII",
                w, h, 0, 0,
                1, 32,
                len(img_bytes),
                offset,
            )
            offset += len(img_bytes)
            data   += img_bytes

        ico_dst.write_bytes(header + entries + data)
        ok(f"Built multi-resolution ICO: {ico_dst.name}")
        return True

    except Exception as e:
        warn(f"ICO build failed: {e}")
        return False


# ── Clean stale build artifacts ────────────────────────────────────────────

def clean_stale():
    for path in [BUILD_DIR, BASE_DIR / f"{APP_NAME}.spec"]:
        if Path(path).exists():
            if Path(path).is_dir():
                shutil.rmtree(path)
            else:
                Path(path).unlink()
            ok(f"Removed stale: {Path(path).name}")


# ── Main build ─────────────────────────────────────────────────────────────

def build():
    step("Building DRSSync.exe with PyInstaller …")

    # ── Locate icon files ──────────────────────────────────────────────────
    ico_path = None
    png_path = None

    for name in ["DRS_icon.ico", "DRS_icon (1).ico"]:
        p = BASE_DIR / name
        if p.exists():
            ico_path = p
            break

    for name in ["DRS_icon.png"]:
        p = BASE_DIR / name
        if p.exists():
            png_path = p
            break

    # Try to rebuild ICO from PNG for best quality
    rebuilt_ico = BASE_DIR / "DRS_icon.ico"
    if png_path and build_ico_from_png(png_path, rebuilt_ico):
        ico_path = rebuilt_ico
    elif ico_path:
        ok(f"Using existing ICO: {ico_path.name}")
    else:
        warn("No icon file found — using default PyInstaller icon")

    # ── Check for config.json ──────────────────────────────────────────────
    config_path = BASE_DIR / "config.json"
    if not config_path.exists():
        # Create a default one
        config_path.write_text(json.dumps({"database": "SHADEDB"}, indent=4))
        ok("Created default config.json")

    clean_stale()

    # ── PyInstaller command ────────────────────────────────────────────────
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name",     APP_NAME,
        "--distpath", str(DIST_DIR),
        # Hidden imports
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "PIL.ImageTk",
        "--hidden-import", "PIL.Image",
        "--collect-submodules", "PIL",
        "--hidden-import", "pyodbc",
        "--hidden-import", "plyer",
        "--hidden-import", "plyer.platforms.win.notification",
        # Bundle config.json
        f"--add-data={config_path}{os.pathsep}.",
    ]

    # Bundle icon files if present
    if ico_path and ico_path.exists():
        cmd += [f"--add-data={ico_path}{os.pathsep}."]
        cmd += ["--icon", str(ico_path)]
        ok(f"Embedding icon: {ico_path.name}")

    if png_path and png_path.exists():
        cmd += [f"--add-data={png_path}{os.pathsep}."]

    cmd.append(str(ENTRY))

    print(f"\n  Running PyInstaller …\n")
    result = subprocess.run(cmd, cwd=str(BASE_DIR))

    if result.returncode != 0:
        fail("PyInstaller build failed — check output above.")

    ok(f"Build complete → {DIST_DIR / (APP_NAME + '.exe')}")


# ── Generate client config ─────────────────────────────────────────────────

def generate_config():
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    dest = DIST_DIR / "config.json"
    if not dest.exists():
        dest.write_text(json.dumps({"database": "SHADEDB"}, indent=4) + "\n")
        ok(f"config.json created → {dest}")
    else:
        ok(f"config.json already exists → {dest}")


# ── Summary ────────────────────────────────────────────────────────────────

def print_summary():
    exe = DIST_DIR / f"{APP_NAME}.exe"
    print()
    print("─" * 54)
    print(f"  DRSSync BUILD COMPLETE")
    print("─" * 54)
    print(f"  Folder     : {DIST_DIR}")
    print(f"  Executable : {exe}")
    print()
    print("  FILES TO DEPLOY (place in same folder):")
    print(f"  ✓  {APP_NAME}.exe")
    print(f"  ✓  config.json  ← set 'database' to your DB name")
    print()
    print("  USAGE:")
    print("  * Double-click DRSSync.exe  → opens sync window")
    print("  * Edit config.json to change the database name")
    print("─" * 54)
    print()


# ── Entry ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Build DRSSync.exe")
    parser.add_argument("--clean", action="store_true",
                        help="Remove build/ and DRS_SYNC/ before building")
    args = parser.parse_args()

    banner()
    install_deps()
    check_entry()

    if args.clean:
        step("Cleaning previous build …")
        for d in [DIST_DIR, BUILD_DIR]:
            if d.exists():
                shutil.rmtree(d)
                ok(f"Removed: {d}")

    build()
    generate_config()
    print_summary()