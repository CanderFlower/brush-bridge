"""Build the single-file Windows executable: python scripts/build_exe.py

Output: dist/BrushBridge.exe
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if not (ROOT / "packaging" / "icon.ico").exists():
        subprocess.check_call([sys.executable, str(ROOT / "scripts" / "make_icon.py")])
    launcher = ROOT / "packaging" / "launcher.py"
    launcher.write_text("import sys\nfrom brush_bridge.gui.app import main\n\nsys.exit(main())\n", encoding="utf-8")
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "BrushBridge",
        "--icon", str(ROOT / "packaging" / "icon.ico"),
        "--collect-submodules", "brush_bridge",
        "--add-data", f"{ROOT / 'brush_bridge' / 'gui' / 'icon.png'};brush_bridge/gui",
        "--exclude-module", "tkinter",
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.Qt3DCore",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtMultimedia",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        str(launcher),
    ]
    return subprocess.call(args, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
