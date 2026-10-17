"""Windows/system helpers: installed-program lookup, process checks, backups."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

IS_WINDOWS = sys.platform == "win32"


@dataclass
class InstalledProgram:
    name: str
    version: str
    location: Optional[Path]


def installed_programs() -> list[InstalledProgram]:
    """Read the Windows "Programs and Features" uninstall registry keys."""
    if not IS_WINDOWS:
        return []
    import winreg

    out: list[InstalledProgram] = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, key in roots:
        try:
            k = winreg.OpenKey(hive, key)
        except OSError:
            continue
        with k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    sub = winreg.OpenKey(k, winreg.EnumKey(k, i))
                except OSError:
                    continue
                with sub:
                    def val(n):
                        try:
                            return winreg.QueryValueEx(sub, n)[0]
                        except OSError:
                            return ""

                    name = val("DisplayName")
                    if not name:
                        continue
                    loc = val("InstallLocation") or ""
                    if not loc:
                        icon = str(val("DisplayIcon") or "").split(",")[0].strip('"')
                        loc = str(Path(icon).parent) if icon else ""
                    out.append(InstalledProgram(str(name), str(val("DisplayVersion") or ""),
                                                Path(loc) if loc else None))
    return out


def find_programs(pattern: str) -> list[InstalledProgram]:
    rx = re.compile(pattern, re.I)
    seen, res = set(), []
    for p in installed_programs():
        if rx.search(p.name) and (p.name, p.version) not in seen:
            seen.add((p.name, p.version))
            res.append(p)
    return res


def file_version(exe: Path) -> str:
    """Product version of a Windows executable ('' if unavailable)."""
    if not IS_WINDOWS or not exe or not Path(exe).exists():
        return ""
    import ctypes
    from ctypes import wintypes

    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(str(exe), None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(str(exe), 0, size, buf):
        return ""
    p = ctypes.c_void_p()
    n = wintypes.UINT()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(n)):
        return ""

    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [(f, wintypes.DWORD) for f in (
            "sig", "struc", "fvMS", "fvLS", "pvMS", "pvLS", "mask", "flags", "os", "type", "sub", "dMS", "dLS")]

    fi = ctypes.cast(p, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    return f"{fi.pvMS >> 16}.{fi.pvMS & 0xFFFF}.{fi.pvLS >> 16}"


def _process_paths() -> list[str]:
    if not IS_WINDOWS:
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Process | ForEach-Object { $_.Path }"],
            capture_output=True, text=True, timeout=20, creationflags=0x08000000)
        return [line.strip().lower() for line in out.stdout.splitlines() if line.strip()]
    except Exception:
        return []


_proc_cache: tuple[float, list[str]] = (0.0, [])


def process_running(exe: Path | str) -> bool:
    global _proc_cache
    if time.time() - _proc_cache[0] > 3:
        _proc_cache = (time.time(), _process_paths())
    target = str(exe).lower()
    name = Path(target).name
    return any(p == target or Path(p).name == name for p in _proc_cache[1])


def backup(path: Path, keep: int = 10) -> Path:
    """Copy ``path`` into a sibling ``brush-bridge-backups`` folder; return the backup path."""
    path = Path(path)
    bdir = path.parent / "brush-bridge-backups"
    bdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = bdir / f"{path.stem}.{stamp}{path.suffix}"
    if path.is_dir():
        shutil.copytree(path, dst)
    else:
        shutil.copy2(path, dst)
    olds = sorted(bdir.glob(f"{path.stem}.*{path.suffix}"))
    for old in olds[:-keep]:
        try:
            shutil.rmtree(old) if old.is_dir() else old.unlink()
        except OSError:
            pass
    return dst


def safe_filename(name: str, fallback: str = "brush") -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return s[:120] or fallback


def unique_path(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        return path
    i = 2
    while True:
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not p.exists():
            return p
        i += 1


def documents_dir() -> Path:
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
            return Path(buf.value)
    return Path.home() / "Documents"


def appdata() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))


def export_dir(sub: str = "") -> Path:
    """User-visible folder for generated files (manual-import fallback): Documents/BrushBridge/<sub>."""
    d = Path(os.environ["BRUSH_BRIDGE_EXPORT_DIR"]) if os.environ.get("BRUSH_BRIDGE_EXPORT_DIR") \
        else documents_dir() / "BrushBridge"
    if sub:
        d = d / safe_filename(sub)
    d.mkdir(parents=True, exist_ok=True)
    return d


def first_existing(paths: Iterable[Path]) -> Optional[Path]:
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None


def open_in_explorer(path: Path) -> None:
    if IS_WINDOWS:
        subprocess.Popen(["explorer", "/select,", str(path)])
