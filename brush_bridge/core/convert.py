"""High level operations shared by the GUI and the CLI."""
from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .model import Brush, BrushRef
from .plugin import AppAdapter, AppInstall, FileInfo, InstallResult, apps, codec, codecs, sniff

log = logging.getLogger(__name__)


@dataclass
class DetectedApp:
    adapter: AppAdapter
    install: Optional[AppInstall]  # None => not found on this computer
    note: str = ""

    @property
    def label(self) -> str:
        return self.install.label if self.install else self.adapter.name


def detect_all() -> list[DetectedApp]:
    """Every known app; installed ones first. Apps without a PC version are listed with a note."""
    found: list[DetectedApp] = []
    missing: list[DetectedApp] = []
    for a in apps():
        try:
            insts = a.detect()
        except Exception:
            log.exception("detect %s", a.id)
            insts = []
        if insts:
            found += [DetectedApp(a, i) for i in insts]
        elif not a.desktop:
            found.append(DetectedApp(a, None, "仅 iPad 版，电脑上无法检测版本；可直接导入或导出 .brush/.brushset 文件"))
        else:
            missing.append(DetectedApp(a, None, "未在此电脑上找到"))
    return found + missing


@dataclass
class OpenedFile:
    path: Path
    info: Optional[FileInfo]
    refs: list[BrushRef] = field(default_factory=list)
    error: str = ""


def open_file(path: Path) -> OpenedFile:
    path = Path(path)
    info = sniff(path)
    if info is None:
        hint = ""
        if zipfile.is_zipfile(path):
            hint = "（这是一个压缩包，请先解压再拖入里面的笔刷文件）"
        return OpenedFile(path, None, error=f"无法识别的文件格式{hint}")
    try:
        refs = codec(info.format_id).list_refs(path)
    except Exception as e:
        log.exception("list %s", path)
        return OpenedFile(path, info, error=f"读取失败：{e}")
    if info.brush_count < 0:
        info.brush_count = len(refs)
    return OpenedFile(path, info, refs)


def expand_paths(paths: list[Path]) -> list[Path]:
    """Folders → their brush files (recursively); known extensions only."""
    exts = {e for c in codecs() for e in c.extensions}
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in exts)
        else:
            out.append(p)
    return out


def diagnose(path: Path, detected: Optional[list[DetectedApp]] = None) -> Optional[FileInfo]:
    """Sniff a file and add compatibility problems for every installed app."""
    info = sniff(Path(path))
    if info is None:
        return None
    c = codec(info.format_id)
    for d in detected if detected is not None else detect_all():
        if not d.install:
            continue
        try:
            msg = c.compatibility(info, d.adapter.id, d.install.version)
        except Exception:
            msg = None
        if msg:
            info.problems.append(f"{d.install.label}：{msg}")
    return info


def load_brushes(refs: list[BrushRef]) -> tuple[list[Brush], list[str]]:
    brushes, errors = [], []
    for r in refs:
        try:
            brushes.append(r.load())
        except Exception as e:
            log.exception("load %s", r.name)
            errors.append(f"「{r.name}」读取失败：{e}")
    return brushes, errors


def writable_codecs() -> list:
    return [c for c in codecs() if c.can_write]


def export(brushes: list[Brush], codec_id: str, out_path: Path, **options) -> list[Path]:
    c = codec(codec_id)
    out_path = Path(out_path)
    if not out_path.suffix and c.extensions:
        out_path = out_path.with_suffix(c.extensions[0])
    return c.write(brushes, out_path, **options)


def install(target: DetectedApp, brushes: list[Brush], **options) -> InstallResult:
    if target.install is None:
        return InstallResult(False, f"未在此电脑上找到 {target.adapter.name}。")
    try:
        return target.adapter.install(target.install, brushes, **options)
    except NotImplementedError:
        return InstallResult(False, f"暂不支持自动导入到 {target.adapter.name}，请使用「导出为文件」。")
    except Exception as e:
        log.exception("install into %s", target.adapter.id)
        return InstallResult(False, f"导入 {target.label} 时出错：{e}")
