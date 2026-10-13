"""
Build FFmpeg Studio into a single Windows EXE.

    pip install -r requirements.txt
    python build.py            # one-file EXE  -> dist/FFmpeg Studio.exe
    python build.py --onedir   # folder build  -> dist/FFmpeg Studio/  (starts faster)
    python build.py --installer  # EXE + Inno Setup installer -> dist/FFmpegStudio-Setup-<version>.exe
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "FFmpeg Studio"


def make_icon() -> Path:
    """Render the app icon (same drawing the app uses) to assets/icon.ico."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(HERE))
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    from theme import _logo

    out = HERE / "assets"
    out.mkdir(exist_ok=True)
    ico = out / "icon.ico"
    if not _logo(256).save(str(ico), "ICO"):
        raise SystemExit("Couldn't write icon.ico")
    return ico


def build_installer():
    """Compile installer/FFmpegStudio.iss with Inno Setup (ISCC)."""
    sys.path.insert(0, str(HERE))
    import core

    candidates = [Path(os.environ.get(v, "")) / "Inno Setup 6" / "ISCC.exe"
                  for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe")
    iscc = next((c for c in candidates if c.is_file()), None)
    if not iscc:
        raise SystemExit("Inno Setup 6 not found. Install it from https://jrsoftware.org/isinfo.php "
                         "or open installer/FFmpegStudio.iss in the Inno Setup compiler.")
    if onedir_requested():
        raise SystemExit("The installer script expects the one-file EXE; run without --onedir.")
    subprocess.check_call([str(iscc), f"/DMyAppVersion={core.APP_VERSION}", str(HERE / "installer" / "FFmpegStudio.iss")])
    print("\nInstaller ->", HERE / "dist" / f"FFmpegStudio-Setup-{core.APP_VERSION}.exe")


def onedir_requested() -> bool:
    return "--onedir" in sys.argv


def main():
    onedir = onedir_requested()
    ico = make_icon()
    exe = HERE / "dist" / f"{NAME}.exe"
    if os.name == "nt":  # a running copy locks the EXE (WinError 5) - close it first
        subprocess.run(["taskkill", "/IM", f"{NAME}.exe", "/F"], capture_output=True)
    if exe.exists():
        try:
            exe.unlink()
        except PermissionError:
            raise SystemExit(f"Can't overwrite {exe}. Close it (or wait for antivirus to finish) and retry.")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--onedir" if onedir else "--onefile",
        "--name", NAME,
        "--icon", str(ico),
        "--exclude-module", "tkinter",
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.Qt3DCore",
        str(HERE / "main.py"),
    ]
    print(" ".join(cmd))
    subprocess.check_call(cmd, cwd=HERE)
    print("\nDone ->", HERE / "dist")
    if "--installer" in sys.argv:
        build_installer()


if __name__ == "__main__":
    main()
