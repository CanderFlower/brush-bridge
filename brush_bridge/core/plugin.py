"""Plugin interfaces: format codecs (files) and app adapters (installed software)."""
from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .model import Brush, BrushRef

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------------------
# File formats
# ---------------------------------------------------------------------------------------------
@dataclass
class Fix:
    """An automatic repair for a file that a target app refuses to import."""

    title: str  # short, shown on a button
    description: str
    apply: Callable[[Path, Path], list[Path]]  # (src, out_dir) -> produced files


@dataclass
class FileInfo:
    path: Path
    format_id: str
    format_name: str
    version: str = ""
    brush_count: int = -1
    details: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    fixes: list[Fix] = field(default_factory=list)


class FormatCodec(ABC):
    id: str = ""
    name: str = ""  # user facing, e.g. "Photoshop 笔刷 (.abr)"
    extensions: tuple[str, ...] = ()
    can_read: bool = True
    can_write: bool = False

    @abstractmethod
    def sniff(self, path: Path) -> Optional[FileInfo]:
        """Return FileInfo when ``path`` is this format (cheap: header only), else None."""

    def list_refs(self, path: Path) -> list[BrushRef]:
        """List brushes inside a file. Default implementation decodes everything."""
        brushes = self.read(path)
        return [BrushRef(name=b.name, key=str(i), group=b.group, loader=(lambda b=b: b),
                         thumbnail=(lambda b=b: b.preview or b.main_tip())) for i, b in enumerate(brushes)]

    @abstractmethod
    def read(self, path: Path) -> list[Brush]:
        ...

    def write(self, brushes: list[Brush], out_path: Path, **options) -> list[Path]:
        """Write brushes; ``out_path`` is a file path with this codec's first extension."""
        raise NotImplementedError(f"{self.id} cannot be written")

    def compatibility(self, info: FileInfo, app_id: str, app_version: str) -> Optional[str]:
        """Return a problem description if ``app_id``/``app_version`` cannot import this file."""
        return None


# ---------------------------------------------------------------------------------------------
# Installed applications
# ---------------------------------------------------------------------------------------------
@dataclass
class AppInstall:
    app_id: str
    name: str  # e.g. "Adobe Photoshop 2021"
    version: str
    exe: Optional[Path] = None
    data_dir: Optional[Path] = None  # where the app keeps user brushes
    running: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.name} {self.version}".strip()


@dataclass
class InstallResult:
    ok: bool
    message: str  # user facing, Chinese
    files: list[Path] = field(default_factory=list)  # produced files (for manual import fallback)
    needs_app_closed: bool = False
    backup: Optional[Path] = None


class AppAdapter(ABC):
    id: str = ""
    name: str = ""
    native_format: str = ""  # codec id the app imports best
    import_formats: tuple[str, ...] = ()  # codec ids the app can import itself
    desktop: bool = True  # False for Procreate (iPad only)

    @abstractmethod
    def detect(self) -> list[AppInstall]:
        ...

    def list_brushes(self, inst: AppInstall) -> list[BrushRef]:
        return []

    def install(self, inst: AppInstall, brushes: list[Brush], **options) -> InstallResult:
        raise NotImplementedError

    def install_files(self, inst: AppInstall, files: list[Path]) -> InstallResult:
        """Install already-encoded native files (lossless path)."""
        raise NotImplementedError

    def is_running(self, inst: AppInstall) -> bool:
        from .system import process_running

        return bool(inst.exe) and process_running(inst.exe)


# ---------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------
_CODECS: dict[str, FormatCodec] = {}
_APPS: dict[str, AppAdapter] = {}
_loaded = False

PLUGIN_MODULES = [
    "brush_bridge.formats.abr",
    "brush_bridge.formats.procreate",
    "brush_bridge.formats.csp",
    "brush_bridge.formats.sai2",
    "brush_bridge.formats.krita",
    "brush_bridge.formats.gimp",
    "brush_bridge.formats.mypaint",
    "brush_bridge.formats.images",
    "brush_bridge.apps.photoshop",
    "brush_bridge.apps.csp",
    "brush_bridge.apps.sai2",
    "brush_bridge.apps.krita",
    "brush_bridge.apps.gimp",
    "brush_bridge.apps.mypaint",
    "brush_bridge.apps.procreate",
]


def register_codec(cls):
    _CODECS[cls.id] = cls()
    return cls


def register_app(cls):
    _APPS[cls.id] = cls()
    return cls


def load_plugins() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in PLUGIN_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as e:
            if e.name != mod:
                log.exception("plugin %s failed", mod)
        except Exception:  # a broken plugin must never break the whole app
            log.exception("plugin %s failed", mod)


def codecs() -> list[FormatCodec]:
    load_plugins()
    return list(_CODECS.values())


def codec(cid: str) -> FormatCodec:
    load_plugins()
    return _CODECS[cid]


def apps() -> list[AppAdapter]:
    load_plugins()
    return list(_APPS.values())


def app(aid: str) -> AppAdapter:
    load_plugins()
    return _APPS[aid]


def sniff(path: Path) -> Optional[FileInfo]:
    path = Path(path)
    ordered = sorted(codecs(), key=lambda c: path.suffix.lower() not in c.extensions)
    for c in ordered:
        try:
            info = c.sniff(path)
        except Exception:
            log.exception("sniff %s with %s", path, c.id)
            continue
        if info:
            return info
    return None
