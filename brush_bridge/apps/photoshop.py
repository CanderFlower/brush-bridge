"""Adobe Photoshop: detection, brush library listing and installation."""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..core.model import Brush, BrushRef
from ..core.plugin import AppAdapter, AppInstall, InstallResult, register_app
from ..core.system import IS_WINDOWS, appdata, export_dir, file_version, find_programs, safe_filename

_PS_MAJOR_TO_NAME = {
    13: "CS6", 14: "CC", 15: "CC 2014", 16: "CC 2015", 17: "CC 2017", 18: "CC 2017", 19: "CC 2018",
    20: "CC 2019", 21: "2020", 22: "2021", 23: "2022", 24: "2023", 25: "2024", 26: "2025", 27: "2026",
}


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    for p in find_programs(r"^Adobe Photoshop(?! Elements)|^Photoshop"):
        if p.location:
            dirs.append(p.location)
    for root in {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", ""),
                 os.environ.get("ProgramFiles(x86)", "")}:
        if root:
            adobe = Path(root) / "Adobe"
            if adobe.is_dir():
                dirs += [d for d in adobe.iterdir() if d.is_dir() and d.name.lower().startswith("adobe photoshop")]
    out, seen = [], set()
    for d in dirs:
        key = str(d).lower().rstrip("\\")
        if key not in seen and (d / "Photoshop.exe").exists():
            seen.add(key)
            out.append(d)
    return out


def settings_dir(folder_name: str) -> Optional[Path]:
    base = appdata() / "Adobe" / folder_name
    for cand in (base / f"{folder_name} Settings", *(base.glob("* Settings") if base.is_dir() else [])):
        if cand.is_dir():
            return cand
    return None


def _library_file(folder_name: str) -> Optional[Path]:
    s = settings_dir(folder_name)
    if s and (s / "Brushes.psp").exists():
        return s / "Brushes.psp"
    return None


@register_app
class PhotoshopAdapter(AppAdapter):
    id = "photoshop"
    name = "Adobe Photoshop"
    native_format = "abr"
    import_formats = ("abr",)

    def detect(self) -> list[AppInstall]:
        if not IS_WINDOWS:
            return []
        res = []
        for d in _candidate_dirs():
            exe = d / "Photoshop.exe"
            version = file_version(exe)
            try:
                major = int(version.split(".")[0])
            except ValueError:
                major = 0
            folder = d.name
            label = folder if re.search(r"\d{4}|CS\d|CC", folder) else f"Adobe Photoshop {_PS_MAJOR_TO_NAME.get(major, '')}"
            lib = _library_file(folder)
            presets = appdata() / "Adobe" / folder / "Presets" / "Brushes"
            res.append(AppInstall(
                app_id=self.id, name=label.strip(), version=version, exe=exe,
                data_dir=lib.parent if lib else (presets if presets.parent.exists() else None),
                running=self.is_running(AppInstall(self.id, label, version, exe)),
                extra={"library": str(lib) if lib else "", "presets_dir": str(presets), "major": major},
            ))
        return res

    def list_brushes(self, inst: AppInstall) -> list[BrushRef]:
        from ..core.plugin import codec

        abr = codec("abr")
        refs: list[BrushRef] = []
        lib = inst.extra.get("library")
        if lib and Path(lib).exists():
            refs += abr.list_refs(Path(lib))
        pdir = Path(inst.extra.get("presets_dir", ""))
        if not refs and pdir.is_dir():  # CS6-era: loose .abr files in Presets\Brushes
            for f in sorted(pdir.glob("*.abr")):
                try:
                    for r in abr.list_refs(f):
                        r.group = f.stem + (" / " + r.group if r.group else "")
                        r.key = f"{f.name}#{r.key}"
                        refs.append(r)
                except Exception:
                    continue
        return refs

    def install(self, inst: AppInstall, brushes: list[Brush], **options) -> InstallResult:
        from ..core.plugin import codec

        if not brushes:
            return InstallResult(False, "没有选择任何笔刷。")
        name = options.get("file_name") or (brushes[0].name if len(brushes) == 1 else f"BrushBridge_{len(brushes)}笔刷")
        out = export_dir("Photoshop") / f"{safe_filename(name)}_{time.strftime('%m%d-%H%M%S')}.abr"
        files = codec("abr").write(brushes, out)
        res = self.install_files(inst, files)
        notes = sorted({n for b in brushes for n in b.notes})
        if notes:
            res.message += "\n\n转换提示：\n· " + "\n· ".join(notes)
        return res

    def install_files(self, inst: AppInstall, files: list[Path]) -> InstallResult:
        abrs = [Path(f) for f in files if Path(f).suffix.lower() == ".abr"]
        if not abrs:
            return InstallResult(False, "Photoshop 只能导入 .abr 笔刷文件。", files=list(files))
        if not inst.exe or not Path(inst.exe).exists():
            return InstallResult(False, "找不到 Photoshop 程序，请手动双击下面的文件导入。", files=abrs)
        running = self.is_running(inst)
        try:
            # Same as double-clicking: a running Photoshop receives the file and appends it to the
            # Brushes panel (saved into Brushes.psp); otherwise Photoshop starts and loads it.
            for f in abrs:
                subprocess.Popen([str(inst.exe), str(f)], close_fds=True)
                time.sleep(0.5)
        except OSError as e:
            return InstallResult(False, f"无法启动 Photoshop：{e}\n请手动双击下面的文件导入。", files=abrs)
        where = "已发送给正在运行的 Photoshop" if running else "正在启动 Photoshop 并载入"
        return InstallResult(True, f"{where}。笔刷会出现在「画笔」面板的最下方。\n文件也保存在：{abrs[0].parent}", files=abrs)
