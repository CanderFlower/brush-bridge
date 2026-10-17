"""GIMP: detection, user brush listing and installation.

GIMP reads brushes from every folder on its brush path; the per-user folder is
``%APPDATA%\\GIMP\\<major.minor>\\brushes`` (2.10, 3.0, ...). New files appear after pressing the
"Refresh brushes" button in the Brushes dockable or after restarting GIMP. GIMP imports ``.gbr``,
``.gih``, ``.vbr`` and Photoshop ``.abr`` natively.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from ..core.model import Brush, BrushRef
from ..core.plugin import AppAdapter, AppInstall, InstallResult, codec, register_app
from ..core.system import IS_WINDOWS, appdata, export_dir, file_version, find_programs, safe_filename, unique_path

BRUSH_EXTS = (".gbr", ".gih", ".vbr", ".abr")


def _exe_in(location: Path) -> Optional[Path]:
    bindir = location / "bin"
    if not bindir.is_dir():
        return None
    exes = [p for p in bindir.glob("gimp*.exe")
            if re.fullmatch(r"gimp(-\d+(\.\d+)*)?\.exe", p.name, re.I)]
    if not exes:
        return None
    # Prefer the versioned GUI binary (gimp-2.10.exe / gimp-3.0.exe), never gimp-console.
    exes.sort(key=lambda p: (p.name.lower() == "gimp.exe", p.name.lower()))
    return exes[0]


def _version_from(exe: Path, reg_version: str) -> str:
    for v in (file_version(exe), reg_version):
        m = re.search(r"\d+\.\d+(?:\.\d+)?", v or "")
        if m:
            return m.group(0)
    m = re.search(r"gimp-(\d+\.\d+)", exe.name, re.I)
    return m.group(1) if m else ""


def _series(version: str, exe: Optional[Path] = None) -> str:
    """GIMP_APP_VERSION (major.minor) used for the user config folder name."""
    m = re.match(r"(\d+)\.(\d+)", version or "")
    if m:
        return f"{int(m.group(1))}.{int(m.group(2))}"
    if exe is not None:
        m = re.search(r"gimp-(\d+\.\d+)", exe.name, re.I)
        if m:
            return m.group(1)
    return "2.10"


def user_dir(series: str) -> Path:
    override = os.environ.get("GIMP3_DIRECTORY" if series.startswith("3") else "GIMP2_DIRECTORY")
    if override:
        return Path(override)
    return appdata() / "GIMP" / series


def _candidates() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for p in find_programs(r"^GIMP\b|^GNU Image Manipulation Program"):
        if p.location:
            exe = _exe_in(Path(p.location))
            if exe:
                found.append((exe, p.version))
    for env in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if not root:
            continue
        for name in ("GIMP 3", "GIMP 2", "GIMP"):
            exe = _exe_in(Path(root) / name)
            if exe:
                found.append((exe, ""))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for d in (Path(local) / "Programs").glob("GIMP*"):
            exe = _exe_in(d)
            if exe:
                found.append((exe, ""))
    out, seen = [], set()
    for exe, ver in found:
        key = str(exe).lower()
        if key not in seen:
            seen.add(key)
            out.append((exe, ver))
    return out


@register_app
class GimpAdapter(AppAdapter):
    id = "gimp"
    name = "GIMP"
    native_format = "gimp"
    import_formats = ("gimp", "abr")

    def detect(self) -> list[AppInstall]:
        if not IS_WINDOWS:
            return []
        try:
            cands = _candidates()
        except Exception:
            return []
        res = []
        for exe, reg_version in cands:
            version = _version_from(exe, reg_version)
            series = _series(version, exe)
            udir = user_dir(series)
            probe = AppInstall(self.id, "GIMP", version, exe)
            res.append(AppInstall(
                app_id=self.id, name="GIMP", version=version, exe=exe, data_dir=udir / "brushes",
                running=self.is_running(probe),
                extra={"series": series, "user_dir": str(udir), "patterns_dir": str(udir / "patterns")},
            ))
        return res

    def list_brushes(self, inst: AppInstall) -> list[BrushRef]:
        bdir = Path(inst.data_dir) if inst.data_dir else None
        if not bdir or not bdir.is_dir():
            return []
        refs: list[BrushRef] = []
        for f in sorted(p for p in bdir.rglob("*") if p.is_file() and p.suffix.lower() in BRUSH_EXTS):
            group = "我的笔刷" if f.parent == bdir else f.parent.name
            if f.suffix.lower() == ".abr":
                try:
                    for r in codec("abr").list_refs(f):
                        r.group = f.stem + (" / " + r.group if r.group else "")
                        r.key = f"{f.name}#{r.key}"
                        refs.append(r)
                except Exception:
                    continue
                continue
            refs.append(self._ref(f, group))
        return refs

    @staticmethod
    def _ref(path: Path, group: str) -> BrushRef:
        name = path.stem
        try:
            info = codec("gimp").sniff(path)
            if info and info.details.get("名称"):
                name = info.details["名称"]
        except Exception:
            pass

        def load(p=path) -> Brush:
            return codec("gimp").read(p)[0]

        def thumb(p=path) -> Optional[Image.Image]:
            b = codec("gimp").read(p)[0]
            tip = b.main_tip()
            if tip is None:
                from ..core.imaging import round_tip

                tip = round_tip(64, b.hardness, b.roundness, b.angle)
            rgba = Image.new("RGBA", tip.size, (0, 0, 0, 0))
            rgba.putalpha(tip)
            return rgba

        return BrushRef(name=name, key=str(path), group=group, loader=load, thumbnail=thumb,
                        detail=f"GIMP {path.suffix.lower()[1:]}")

    def install(self, inst: AppInstall, brushes: list[Brush], **options) -> InstallResult:
        if not brushes:
            return InstallResult(False, "没有选择任何笔刷。")
        stage = export_dir("GIMP") / time.strftime("%m%d-%H%M%S")
        stage.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        gc = codec("gimp")
        for b in brushes:
            ext = ".gih" if len(b.tips) > 1 else ".gbr"
            files += gc.write([b], unique_path(stage / f"{safe_filename(b.name)}{ext}"), **options)
        res = self.install_files(inst, files)
        notes = sorted({n for b in brushes for n in b.notes})
        if notes:
            res.message += "\n\n转换提示：\n· " + "\n· ".join(notes)
        return res

    def install_files(self, inst: AppInstall, files: list[Path]) -> InstallResult:
        files = [Path(f) for f in files]
        ok_files = [f for f in files if f.suffix.lower() in BRUSH_EXTS]
        if not ok_files:
            return InstallResult(False, "GIMP 只能导入 .gbr、.gih、.vbr 或 .abr 笔刷文件。", files=files)
        if not inst.data_dir:
            return InstallResult(False, "找不到 GIMP 的笔刷文件夹，请手动把下面的文件复制到 GIMP 的 brushes 文件夹。",
                                 files=ok_files)
        bdir = Path(inst.data_dir)
        installed: list[Path] = []
        try:
            bdir.mkdir(parents=True, exist_ok=True)
            for f in ok_files:
                dest = unique_path(bdir / f.name)
                shutil.copy2(f, dest)
                installed.append(dest)
        except OSError as e:
            return InstallResult(False, f"无法写入 GIMP 笔刷文件夹：{e}\n请手动把下面的文件复制到 GIMP 的 brushes 文件夹。",
                                 files=ok_files)
        if self.is_running(inst):
            tail = "GIMP 正在运行：请在「笔刷」面板底部点击「刷新笔刷」按钮，新笔刷就会出现。"
        else:
            tail = "下次启动 GIMP 时新笔刷会出现在「笔刷」面板中。"
        return InstallResult(True, f"已安装到 GIMP 笔刷文件夹：{bdir}\n{tail}", files=installed)
