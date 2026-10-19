"""MyPaint: detection, user brush listing and installation.

MyPaint keeps user brushes in ``%LOCALAPPDATA%\\mypaint\\brushes`` (GLib's user data dir) as
``<name>.myb`` + ``<name>_prev.png``. Brushes in sub folders are listed by their folder; brushes not
mentioned in ``order.conf`` appear in MyPaint's "Lost & Found" group. GIMP 2.10+ also reads MyPaint
brushes from ``%APPDATA%\\GIMP\\<series>\\mypaint-brushes``.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from ..core.model import Brush, BrushRef
from ..core.plugin import AppAdapter, AppInstall, InstallResult, codec, register_app
from ..core.system import IS_WINDOWS, export_dir, file_version, find_programs, safe_filename, unique_path

SUB_DIR = "brush-bridge"


def user_brush_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "mypaint" / "brushes"


def _candidates() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for p in find_programs(r"^MyPaint\b"):
        if p.location:
            for exe in (Path(p.location) / "mingw64" / "bin" / "mypaint.exe", Path(p.location) / "mypaint.exe",
                        Path(p.location) / "bin" / "mypaint.exe"):
                if exe.is_file():
                    found.append((exe, p.version))
                    break
    for env in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if not root:
            continue
        for d in Path(root).glob("MyPaint*"):
            for exe in (d / "mingw64" / "bin" / "mypaint.exe", d / "mypaint.exe", d / "bin" / "mypaint.exe"):
                if exe.is_file():
                    found.append((exe, ""))
                    break
    out, seen = [], set()
    for exe, ver in found:
        if str(exe).lower() not in seen:
            seen.add(str(exe).lower())
            out.append((exe, ver))
    return out


@register_app
class MyPaintAdapter(AppAdapter):
    id = "mypaint"
    name = "MyPaint"
    native_format = "mypaint"
    import_formats = ("mypaint",)

    def detect(self) -> list[AppInstall]:
        if not IS_WINDOWS:
            return []
        try:
            cands = _candidates()
        except Exception:
            return []
        res = []
        for exe, reg_version in cands:
            version = ""
            for v in (reg_version, file_version(exe)):
                m = re.search(r"\d+\.\d+(?:\.\d+)?", v or "")
                if m:
                    version = m.group(0)
                    break
            probe = AppInstall(self.id, "MyPaint", version, exe)
            res.append(AppInstall(self.id, "MyPaint", version, exe, user_brush_dir(), running=self.is_running(probe)))
        return res

    def list_brushes(self, inst: AppInstall) -> list[BrushRef]:
        bdir = Path(inst.data_dir) if inst.data_dir else None
        if not bdir or not bdir.is_dir():
            return []
        mc = codec("mypaint")
        refs: list[BrushRef] = []
        for f in sorted(bdir.rglob("*.myb")):
            try:
                for r in mc.list_refs(f):
                    r.group = "我的笔刷" if f.parent == bdir else f.parent.name
                    refs.append(r)
            except Exception:
                continue
        return refs

    def install(self, inst: AppInstall, brushes: list[Brush], **options) -> InstallResult:
        if not brushes:
            return InstallResult(False, "没有选择任何笔刷。")
        stage = export_dir("MyPaint") / time.strftime("%m%d-%H%M%S")
        stage.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        mc = codec("mypaint")
        if inst.version and not options.get("mypaint_version"):
            m = re.match(r"(\d+)\.(\d+)", inst.version)
            if m and (int(m.group(1)), int(m.group(2))) < (1, 1):
                options["mypaint_version"] = 2
        for b in brushes:
            files += mc.write([b], unique_path(stage / f"{safe_filename(b.name)}.myb"), **options)
        res = self.install_files(inst, files)
        notes = sorted({n for b in brushes for n in b.notes})
        if notes:
            res.message += "\n\n转换提示：\n· " + "\n· ".join(notes)
        return res

    def install_files(self, inst: AppInstall, files: list[Path]) -> InstallResult:
        files = [Path(f) for f in files]
        ok = [f for f in files if f.suffix.lower() == ".myb"]
        if not ok:
            return InstallResult(False, "MyPaint 只能导入 .myb 笔刷文件。", files=files)
        if not inst.data_dir:
            return InstallResult(False, "找不到 MyPaint 的笔刷文件夹，请手动导入下面的文件。", files=ok)
        dest_dir = Path(inst.data_dir) / SUB_DIR
        installed: list[Path] = []
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            for f in ok:
                dest = unique_path(dest_dir / f.name)
                shutil.copy2(f, dest)
                installed.append(dest)
                prev = f.with_name(f.stem + "_prev.png")
                if prev.is_file():
                    shutil.copy2(prev, dest.with_name(dest.stem + "_prev.png"))
        except OSError as e:
            return InstallResult(False, f"无法写入 MyPaint 笔刷文件夹：{e}\n请手动导入下面的文件。", files=ok)
        tail = ("MyPaint 正在运行：请重启 MyPaint，新笔刷会出现在笔刷列表的「失物招领」(Lost & Found) 分组中。"
                if self.is_running(inst) else "下次启动 MyPaint 时，新笔刷会出现在「失物招领」(Lost & Found) 分组中。")
        return InstallResult(True, f"已安装到 MyPaint 笔刷文件夹：{dest_dir}\n{tail}", files=installed)
