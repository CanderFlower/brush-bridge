"""Procreate (iPad). Nothing can be installed directly from a PC: we export a ``.brushset`` to a
folder the user can sync to the iPad (iCloud Drive when available) and explain how to import it."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Iterable, Optional

from ..core.model import Brush, BrushRef
from ..core.plugin import AppAdapter, AppInstall, InstallResult, codec, register_app
from ..core.system import IS_WINDOWS, documents_dir, safe_filename, unique_path

EXPORT_DIR_NAME = "brush-bridge 导出"
SCAN_BUDGET_S = 2.0
_EXTS = (".brushset", ".brush")
# Files that are cloud placeholders (iCloud/OneDrive "online only") must not be opened while
# listing: reading them would trigger a download.
_CLOUD_ATTRS = 0x00400000 | 0x00040000 | 0x00001000  # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE

INSTALL_HELP = (
    "Procreate 只能在 iPad 上使用，无法从电脑直接安装。请把这个 .brushset 文件传到 iPad：\n"
    "  • 隔空投送（AirDrop，Mac 电脑）；\n"
    "  • iCloud 云盘：放进 iCloud Drive 文件夹，等待同步后在 iPad 的“文件”App 里找到它；\n"
    "  • 微信/QQ“文件传输助手”、网盘或数据线拷贝到 iPad 的“文件”App。\n"
    "然后在 iPad 上轻点该文件（或选择“共享 → Procreate”），Procreate 会自动导入，"
    "新笔刷集出现在笔刷库最上方。"
)


def _known_folder(guid: str) -> Optional[Path]:
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                        ("d4", ctypes.c_ubyte * 8)]

        import uuid

        u = uuid.UUID(guid)
        g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001
        return None


def desktop_dir() -> Path:
    p = _known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")
    return p if p and p.exists() else Path.home() / "Desktop"


def downloads_dir() -> Path:
    p = _known_folder("374DE290-123F-4565-9164-39C4925E467B")
    return p if p and p.exists() else Path.home() / "Downloads"


def icloud_drive_dir() -> Optional[Path]:
    home = Path.home()
    candidates = [os.environ.get("ICLOUD_DRIVE", ""), home / "iCloudDrive", home / "iCloud Drive",
                  home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"]
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    return None


def default_export_dir() -> Path:
    icloud = icloud_drive_dir()
    return (icloud / EXPORT_DIR_NAME) if icloud else (desktop_dir() / EXPORT_DIR_NAME)


def _is_cloud_placeholder(entry: os.DirEntry) -> bool:
    try:
        return bool(getattr(entry.stat(), "st_file_attributes", 0) & _CLOUD_ATTRS)
    except OSError:
        return True


def scan_brush_files(roots: Iterable[Path], budget_s: float = SCAN_BUDGET_S) -> list[tuple[Path, bool]]:
    """Find .brush/.brushset files: each root plus 1 level deep, and folders whose name mentions
    brushes ("brush", "笔刷", "procreate") up to depth 3. Returns ``(path, is_cloud_placeholder)``."""
    deadline = time.monotonic() + budget_s
    found: list[tuple[Path, bool]] = []
    seen: set[str] = set()

    def brushy(name: str) -> bool:
        low = name.lower()
        return "brush" in low or "笔刷" in low or "procreate" in low

    stack: list[tuple[Path, int, bool]] = [(Path(r), 0, False) for r in roots if r and Path(r).is_dir()]
    while stack:
        if time.monotonic() > deadline:
            break
        folder, depth, in_brushy = stack.pop(0)
        key = str(folder).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            it = list(os.scandir(folder))
        except OSError:
            continue
        for e in it:
            if time.monotonic() > deadline:
                break
            try:
                if e.is_file(follow_symlinks=False):
                    if e.name.lower().endswith(_EXTS):
                        found.append((Path(e.path), _is_cloud_placeholder(e)))
                elif e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$")):
                    b = in_brushy or brushy(e.name)
                    if depth < 1 or (b and depth < 3):
                        stack.append((Path(e.path), depth + 1, b))
            except OSError:
                continue
    return found


@register_app
class ProcreateAdapter(AppAdapter):
    id = "procreate"
    name = "Procreate (iPad)"
    native_format = "procreate"
    import_formats = ("procreate", "abr")  # Procreate 5+ imports Photoshop .abr itself
    desktop = False

    def detect(self) -> list[AppInstall]:
        icloud = icloud_drive_dir()
        return [AppInstall(app_id=self.id, name=self.name, version="无法在电脑上检测",
                           data_dir=default_export_dir(),
                           extra={"icloud": str(icloud) if icloud else "", "ipad_only": True})]

    def scan_roots(self, inst: Optional[AppInstall] = None) -> list[Path]:
        roots = [downloads_dir(), desktop_dir(), documents_dir()]
        icloud = icloud_drive_dir()
        if icloud:
            roots.append(icloud)
        if inst and inst.data_dir:
            roots.insert(0, Path(inst.data_dir))
        out, seen = [], set()
        for r in roots:
            k = str(r).lower()
            if k not in seen:
                seen.add(k)
                out.append(r)
        return out

    def list_brushes(self, inst: AppInstall) -> list[BrushRef]:
        c = codec("procreate")
        refs: list[BrushRef] = []
        start = time.monotonic()
        for path, cloud in scan_brush_files(self.scan_roots(inst)):
            group = path.name
            expanded = False
            if not cloud and time.monotonic() - start < SCAN_BUDGET_S:
                try:
                    for r in c.list_refs(path):
                        r.key = f"{path}::{r.key}"
                        r.group = group
                        refs.append(r)
                    expanded = True
                except Exception:  # noqa: BLE001 - a broken file must not break the list
                    expanded = False
            if not expanded:
                refs.append(BrushRef(
                    name=path.stem, key=f"{path}::0", group=group,
                    loader=(lambda p=path: c.read(p)[0]),
                    detail="云端文件，未下载" if cloud else "未展开"))
        return refs

    def install(self, inst: AppInstall, brushes: list[Brush], **options) -> InstallResult:
        if not brushes:
            return InstallResult(ok=False, message="没有要导出的笔刷。")
        out_dir = Path(options.get("out_dir") or inst.data_dir or default_export_dir())
        title = (options.get("set_name") or options.get("name")
                 or next((b.group for b in brushes if b.group), "")
                 or (brushes[0].name if len(brushes) == 1 else "brush-bridge 笔刷"))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = unique_path(out_dir / f"{safe_filename(title, 'brush-bridge')}.brushset")
            files = codec("procreate").write(brushes, path, set_name=title,
                                             prefer_native=options.get("prefer_native", True))
        except Exception as e:  # noqa: BLE001
            return InstallResult(ok=False, message=f"导出 Procreate 笔刷失败：{e}")
        where = "iCloud 云盘" if icloud_drive_dir() and _is_under(out_dir, icloud_drive_dir()) else "文件夹"
        msg = (f"已导出 {len(brushes)} 个笔刷到{where}：\n{files[0]}\n\n" + INSTALL_HELP)
        notes = sorted({n for b in brushes for n in b.notes})
        if notes:
            msg += "\n\n转换提示：\n" + "\n".join(f"  • {n}" for n in notes[:10])
            if len(notes) > 10:
                msg += f"\n  • ……另有 {len(notes) - 10} 条"
        return InstallResult(ok=True, message=msg, files=list(files))

    def install_files(self, inst: AppInstall, files: list[Path]) -> InstallResult:
        out_dir = Path(inst.data_dir or default_export_dir())
        copied: list[Path] = []
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                f = Path(f)
                if f.suffix.lower() not in (".brushset", ".brush", ".abr"):
                    continue
                dst = unique_path(out_dir / f.name)
                shutil.copy2(f, dst)
                copied.append(dst)
        except OSError as e:
            return InstallResult(ok=False, message=f"复制文件失败：{e}", files=copied)
        if not copied:
            return InstallResult(ok=False, message="没有 Procreate 能导入的文件（.brushset / .brush / .abr）。")
        msg = f"已复制 {len(copied)} 个文件到：\n{out_dir}\n\n" + INSTALL_HELP
        if any(p.suffix.lower() == ".abr" for p in copied):
            msg += "\n\nProcreate 5 及以上版本可以直接打开 Photoshop 的 .abr 笔刷文件。"
        return InstallResult(ok=True, message=msg, files=copied)

    def is_running(self, inst: AppInstall) -> bool:
        return False


def _is_under(path: Path, parent: Optional[Path]) -> bool:
    if parent is None:
        return False
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False
