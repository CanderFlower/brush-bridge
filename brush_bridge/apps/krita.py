"""Krita: detection, brush preset listing and bundle installation.

Krita 5 keeps user resources in ``%APPDATA%\\krita`` (overridable with the root-group
``ResourceDirectory`` key in ``%LOCALAPPDATA%\\kritarc``). At startup ``KisResourceLocator::findStorages``
recursively scans that directory for ``*.bundle`` / ``*.abr`` / ``*.asl`` files and registers every new
one as an *active* storage, so installing a bundle is just copying it into ``<resdir>\\bundles`` and
restarting Krita. Krita 4 loads bundles from the same ``bundles`` folder.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from ..core.model import Brush, BrushRef
from ..core.plugin import AppAdapter, AppInstall, InstallResult, codec, register_app
from ..core.system import IS_WINDOWS, appdata, export_dir, file_version, find_programs, safe_filename, unique_path
from ..formats.krita import preset_display_name

_PRESET_EXT = ".kpp"
_FOLDER_FOR_EXT = {
    ".kpp": "paintoppresets",
    ".gbr": "brushes", ".gih": "brushes", ".png": "brushes", ".svg": "brushes",
    ".pat": "patterns",
}


# ---------------------------------------------------------------------------------------------
# Locating Krita
# ---------------------------------------------------------------------------------------------
def _exe_in(location: Path) -> Optional[Path]:
    for cand in (location / "bin" / "krita.exe", location / "krita.exe", location / "krita" / "bin" / "krita.exe"):
        if cand.is_file():
            return cand
    return None


def _candidates() -> list[tuple[Path, str]]:
    """(krita.exe, registry version) pairs, de-duplicated."""
    found: list[tuple[Path, str]] = []
    for p in find_programs(r"^Krita\b"):
        if p.location:
            exe = _exe_in(Path(p.location))
            if exe:
                found.append((exe, p.version))
    for env in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if not root:
            continue
        for name in ("Krita (x64)", "Krita (x86)", "Krita"):
            exe = _exe_in(Path(root) / name)
            if exe:
                found.append((exe, ""))
    out, seen = [], set()
    for exe, ver in found:
        key = str(exe).lower()
        if key not in seen:
            seen.add(key)
            out.append((exe, ver))
    return out


def kritarc_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "kritarc" if local else appdata().parent / "Local" / "kritarc"


def resource_dir() -> Path:
    """Krita's user resource folder (``KoResourcePaths::getAppDataLocation``)."""
    rc = kritarc_path()
    try:
        text = rc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    # The key lives in KConfig's root group, i.e. before the first "[Group]" header.
    root = re.split(r"(?m)^\[", text, maxsplit=1)[0]
    m = re.search(r"(?m)^ResourceDirectory=(.+?)\s*$", root)
    if m:
        value = m.group(1).replace("\\\\", "\\").strip()
        if value:
            return Path(os.path.expandvars(value))
    return appdata() / "krita"


def _major(version: str) -> int:
    try:
        return int(re.match(r"\s*(\d+)", version or "").group(1))
    except (AttributeError, ValueError):
        return 0


# ---------------------------------------------------------------------------------------------
# Resource database (Krita 5)
# ---------------------------------------------------------------------------------------------
def _db_presets(resdir: Path) -> Optional[list[dict]]:
    """Active paint-op presets from ``resourcecache.sqlite`` (read from an in-memory copy)."""
    db = resdir / "resourcecache.sqlite"
    if not db.is_file():
        return None
    try:
        src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        mem = sqlite3.connect(":memory:")
        try:
            src.backup(mem)
        finally:
            src.close()
        try:
            cols = {r[1] for r in mem.execute("PRAGMA table_info(storages)")}
            stype = ("(SELECT name FROM storage_types st WHERE st.id = s.storage_type_id)"
                     if "storage_type_id" in cols else "''")
            rows = mem.execute(
                "SELECT r.name, r.filename, s.location, " + stype + " FROM resources r "
                "JOIN resource_types rt ON r.resource_type_id = rt.id "
                "JOIN storages s ON r.storage_id = s.id "
                "WHERE rt.name = 'paintoppresets' AND r.status = 1 AND s.active = 1 "
                "ORDER BY s.id, r.name"
            ).fetchall()
        finally:
            mem.close()
    except sqlite3.Error:
        return None
    return [{"name": n or "", "filename": f or "", "location": loc or "", "storage_type": st or ""}
            for n, f, loc, st in rows]


def _storage_path(resdir: Path, location: str) -> Path:
    p = Path(location) if location else resdir
    return p if p.is_absolute() else resdir / p


@register_app
class KritaAdapter(AppAdapter):
    id = "krita"
    name = "Krita"
    native_format = "krita"
    import_formats = ("krita", "abr", "gimp")

    def detect(self) -> list[AppInstall]:
        if not IS_WINDOWS:
            return []
        try:
            cands = _candidates()
        except Exception:
            return []
        if not cands:
            return []
        resdir = resource_dir()
        res = []
        for exe, reg_version in cands:
            version = file_version(exe) or reg_version
            m = re.search(r"\d+(?:\.\d+){0,2}", version or "")
            version = m.group(0) if m else (version or "")
            probe = AppInstall(self.id, "Krita", version, exe)
            res.append(AppInstall(
                app_id=self.id, name="Krita", version=version, exe=exe,
                data_dir=resdir, running=self.is_running(probe),
                extra={"resource_dir": str(resdir), "bundles_dir": str(resdir / "bundles"),
                       "major": _major(version), "kritarc": str(kritarc_path())},
            ))
        return res

    # -- listing ---------------------------------------------------------------------------------
    def list_brushes(self, inst: AppInstall) -> list[BrushRef]:
        resdir = Path(inst.data_dir or inst.extra.get("resource_dir") or resource_dir())
        if not resdir.is_dir():
            return []
        rows = _db_presets(resdir)
        if rows is not None:
            refs = self._refs_from_db(resdir, rows)
            if refs:
                return refs
        return self._refs_from_files(resdir)

    def _refs_from_db(self, resdir: Path, rows: list[dict]) -> list[BrushRef]:
        kc = codec("krita")
        refs: list[BrushRef] = []
        bundle_refs: dict[str, dict[str, BrushRef]] = {}
        for row in rows:
            loc = row["location"]
            st = row["storage_type"].lower()
            if st in ("memory", "fontstorage") or loc in ("memory", "fontregistry"):
                continue
            spath = _storage_path(resdir, loc)
            if spath.suffix.lower() == ".bundle" or st == "bundle":
                key = str(spath)
                if key not in bundle_refs:
                    try:
                        bundle_refs[key] = {Path(r.key).name.lower(): r for r in kc.list_refs(spath)}
                    except Exception:
                        bundle_refs[key] = {}
                ref = bundle_refs[key].get(row["filename"].lower())
                if ref is not None:
                    refs.append(BrushRef(name=row["name"] or ref.name, key=f"{spath.name}#{ref.key}",
                                         group=ref.group, loader=ref.loader, thumbnail=ref.thumbnail,
                                         detail=ref.detail))
            elif spath.is_dir():
                f = spath / "paintoppresets" / row["filename"]
                if f.is_file():
                    refs.append(self._kpp_ref(f, row["name"], group="我的预设" if spath == resdir else spath.name))
        return refs

    def _refs_from_files(self, resdir: Path) -> list[BrushRef]:
        kc = codec("krita")
        refs: list[BrushRef] = []
        for b in sorted(resdir.rglob("*.bundle")):
            try:
                for r in kc.list_refs(b):
                    r.key = f"{b.name}#{r.key}"
                    refs.append(r)
            except Exception:
                continue
        pdir = resdir / "paintoppresets"
        if pdir.is_dir():
            for f in sorted(pdir.glob("*" + _PRESET_EXT)):
                try:
                    name = preset_display_name(f.read_bytes(), f.name)
                except OSError:
                    continue
                refs.append(self._kpp_ref(f, name, group="我的预设"))
        return refs

    @staticmethod
    def _kpp_ref(path: Path, name: str, group: str) -> BrushRef:
        def load(p=path) -> Brush:
            return codec("krita").read(p)[0]

        def thumb(p=path) -> Optional[Image.Image]:
            with Image.open(p) as img:
                return img.convert("RGBA")

        return BrushRef(name=name or path.stem, key=str(path), group=group, loader=load, thumbnail=thumb,
                        detail="Krita 预设")

    # -- installing ------------------------------------------------------------------------------
    def install(self, inst: AppInstall, brushes: list[Brush], **options) -> InstallResult:
        if not brushes:
            return InstallResult(False, "没有选择任何笔刷。")
        title = options.get("title") or options.get("file_name") or (
            brushes[0].name if len(brushes) == 1 else f"BrushBridge {len(brushes)} 笔刷")
        out = export_dir("Krita") / f"{safe_filename(title)}_{time.strftime('%m%d-%H%M%S')}.bundle"
        files = codec("krita").write(brushes, out, title=title, tag=options.get("tag") or title)
        res = self.install_files(inst, files)
        notes = sorted({n for b in brushes for n in b.notes})
        if notes:
            res.message += "\n\n转换提示：\n· " + "\n· ".join(notes)
        return res

    def install_files(self, inst: AppInstall, files: list[Path]) -> InstallResult:
        files = [Path(f) for f in files]
        resdir = Path(inst.data_dir or inst.extra.get("resource_dir") or resource_dir())
        major = int(inst.extra.get("major") or _major(inst.version) or 5)
        supported = [f for f in files if f.suffix.lower() in (".bundle", ".abr", *_FOLDER_FOR_EXT)]
        if not supported:
            return InstallResult(False, "Krita 只能导入 .bundle、.kpp、.abr、.gbr、.gih、.png 或 .pat 文件。", files=files)
        installed: list[Path] = []
        try:
            for f in supported:
                ext = f.suffix.lower()
                if ext == ".bundle" or (ext == ".abr" and major >= 5):
                    # Krita 5 registers every *.bundle / *.abr under the resource folder as a storage.
                    dest_dir = resdir / "bundles"
                elif ext == ".abr":
                    dest_dir = resdir / "brushes"  # Krita 4 reads .abr from the brushes folder
                else:
                    dest_dir = resdir / _FOLDER_FOR_EXT[ext]
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = unique_path(dest_dir / f.name)
                shutil.copy2(f, dest)
                installed.append(dest)
        except OSError as e:
            return InstallResult(False, f"无法写入 Krita 资源文件夹：{e}\n可以在 Krita 中通过「设置 → 管理资源库 → 导入」手动导入下面的文件。",
                                 files=supported)
        running = self.is_running(inst)
        where = installed[0].parent
        if running:
            msg = (f"已安装到 Krita 资源文件夹：{where}\nKrita 正在运行，请关闭并重新启动 Krita 后才能看到新笔刷"
                   "（重启后在笔刷预设面板中选择对应的标签）。")
        else:
            msg = f"已安装到 Krita 资源文件夹：{where}\n下次启动 Krita 时会自动载入新笔刷（在笔刷预设面板中选择对应的标签）。"
        return InstallResult(True, msg, files=installed)
