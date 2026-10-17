"""PaintTool SAI Ver.2: detection, brush listing and installation.

Installing copies shapes/textures into the settings folder (safe while SAI2 runs, picked up on the
next start) and registers custom tools in ``custool\\nrm`` (only while SAI2 is closed, because SAI2
rewrites custool when it exits).
"""
from __future__ import annotations

import datetime
import os
import re
import struct
from pathlib import Path
from typing import Optional

from ..core.model import Brush, BrushRef
from ..core.plugin import AppAdapter, AppInstall, InstallResult, register_app
from ..core.system import IS_WINDOWS, file_version, find_programs
from ..formats import sai2 as fmt

EXE = "sai2.exe"
DEFAULT_GROUP = "导入笔刷"

_WHERE = {
    "scatter": "选择“散布”工具，在“散布形状”下拉列表里选择（画笔的“笔刷形态”下拉列表里也能选）",
    "brshape": "在画笔/水彩笔等工具的“笔刷形态”下拉列表里选择",
    "blotmap": "在“笔刷形态”下拉列表的渗化类里选择",
    "bristle": "在“笔刷形态”下拉列表的鬃毛类里选择",
    "brushtex": "在工具的“笔刷纹理”下拉列表里选择",
    "papertex": "在图层属性的“纸张质感”里选择",
}


def history_build(install_dir: Path) -> Optional[str]:
    """Newest build date listed in history.txt (``YYYY-MM-DD版`` headers, newest first)."""
    f = Path(install_dir) / "history.txt"
    try:
        text = fmt.decode_text(f.read_bytes()[:400_000])
    except OSError:
        return None
    dates = re.findall(r"^\s*(20\d\d-\d\d-\d\d)[a-z]?\s*版", text, re.M)
    if not dates:
        dates = re.findall(r"^\s*(20\d\d-\d\d-\d\d)", text, re.M)
    return max(dates) if dates else None


def pe_build(exe: Path) -> Optional[str]:
    """Link date from the PE header (matches the history.txt build date for official builds)."""
    try:
        with open(exe, "rb") as fh:
            head = fh.read(4096)
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if head[pe:pe + 4] != b"PE\0\0":
            return None
        ts = struct.unpack_from("<I", head, pe + 8)[0]
        d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        return d.strftime("%Y-%m-%d") if 2010 <= d.year <= 2100 else None
    except (OSError, struct.error, ValueError, OverflowError):
        return None


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = [p.location for p in find_programs(r"SAI") if p.location]
    roots = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
    roots += [f"{letter}:\\" for letter in "CDEF"]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            for child in Path(root).iterdir():
                if re.search(r"sai", child.name, re.I) and child.is_dir():
                    dirs.append(child)
        except OSError:
            continue
    out, seen = [], set()
    for d in dirs:
        key = str(d).lower().rstrip("\\")
        if key in seen:
            continue
        seen.add(key)
        if (Path(d) / EXE).is_file():
            out.append(Path(d))
    return out


def data_dir_for(install_dir: Path, build: Optional[str]) -> tuple[Path, bool]:
    """(settings folder, legacy layout?) for an install."""
    d = Path(install_dir)
    if fmt.is_settings_root(d / "settings", 2):  # portable packs that keep settings next to the exe
        return d / "settings", False
    legacy = build < fmt.SETTINGS_LAYOUT_BUILD if build else not (d / "init").is_dir()
    if legacy:
        return d, True
    return fmt.default_settings_dir(), False


@register_app
class Sai2Adapter(AppAdapter):
    id = "sai2"
    name = "PaintTool SAI Ver.2"
    native_format = "sai2"
    import_formats = ("sai2",)

    def detect(self) -> list[AppInstall]:
        if not IS_WINDOWS:
            return []
        regs = {str(p.location).lower().rstrip("\\"): p for p in find_programs(r"SAI") if p.location}
        res = []
        for d in _candidate_dirs():
            exe = d / EXE
            hist = history_build(d)
            build = hist or pe_build(exe)
            reg = regs.get(str(d).lower().rstrip("\\"))
            display = reg.version if reg else ""
            saier = "saier" in d.name.lower() or (d / "汉化说明.txt").exists()
            data_dir, legacy = data_dir_for(d, build)
            name = self.name + ("（Saier 汉化版）" if saier else "")
            res.append(AppInstall(
                app_id=self.id, name=name, version=build or display or file_version(exe), exe=exe,
                data_dir=data_dir, running=self.is_running(AppInstall(self.id, name, "", exe)),
                extra={"build": build or "", "build_source": "history.txt" if hist else "exe",
                       "display_version": display, "legacy_layout": legacy, "localized": "saier" if saier else "",
                       "init_dir": str(d / "init") if (d / "init").is_dir() else "",
                       "settings_exists": fmt.is_settings_root(data_dir)}))
        return res

    def list_brushes(self, inst: AppInstall) -> list[BrushRef]:
        if not inst.data_dir or not Path(inst.data_dir).is_dir():
            return []
        return fmt.list_settings(Path(inst.data_dir))

    def install(self, inst: AppInstall, brushes: list[Brush], **options) -> InstallResult:
        if not brushes:
            return InstallResult(False, "没有要安装的笔刷。")
        root = Path(inst.data_dir) if inst.data_dir else None
        legacy = bool(inst.extra.get("legacy_layout"))
        if root is None or not root.is_dir() or (not legacy and not fmt.is_settings_root(root)):
            return InstallResult(False, "没有找到 SAI2 的设置文件夹（文档\\SYSTEMAX Software Development\\SAIv2\\settings）。"
                                        "请先启动并关闭一次 SAI2，让它创建设置文件夹后再安装。")
        running = self.is_running(inst)
        cat_dir = root / "custool" / "nrm"
        can_tools = options.get("create_tools", True) and not legacy and cat_dir.is_dir()
        make_tools = can_tools and not running
        group = options.get("group_name") or DEFAULT_GROUP
        try:
            res = fmt.export_brushes(brushes, root, tools_dir=cat_dir if make_tools else None,
                                     first_id=fmt.next_tool_id(cat_dir) if make_tools else 1,
                                     prefer_native=options.get("prefer_native", True), legacy=legacy)
        except OSError as e:
            return InstallResult(False, f"写入 SAI2 设置文件夹失败：{e}")
        files = list(res.files)
        backups: list[Path] = []
        tool_error = ""
        if res.tools:
            try:
                changed, backups = fmt.register_tools(cat_dir, [t[0] for t in res.tools], group)
                files += [p for p in changed if p not in files]
            except Exception as e:  # never leave unregistered tool files behind
                tool_error = str(e)
                for _id, p, _n in res.tools:
                    try:
                        p.unlink()
                    except OSError:
                        pass
                    if p in files:
                        files.remove(p)
                res.tools = []
        wants_tools = any(not (b.source.format == "sai2" and b.source.native.get("sai2_asset_only")) for b in brushes)
        blocked = can_tools and running and wants_tools

        lines: list[str] = []
        if res.tools:
            names = "、".join(n for _i, _p, n in res.tools[:8]) + ("等" if len(res.tools) > 8 else "")
            lines.append(f"已在 SAI2 普通工具面板的工具组「{group}」里添加了 {len(res.tools)} 个自定义工具：{names}。"
                         f"在工具面板上方的工具组标签里切换到「{group}」即可看到。")
        if res.existing:
            names = "、".join(n for _i, n in res.existing[:8]) + ("等" if len(res.existing) > 8 else "")
            lines.append(f"这些工具之前已经安装过，没有重复添加：{names}。")
        by_kind: dict[str, list[str]] = {}
        for kind, name in res.assets:
            if name not in by_kind.setdefault(kind, []):
                by_kind[kind].append(name)
        for kind, names in by_kind.items():
            shown = "、".join(names[:8]) + ("等" if len(names) > 8 else "")
            lines.append(f"{fmt.ASSET_LABELS[kind]}：{shown} —— {_WHERE[kind]}。")
        if blocked:
            lines.append("SAI2 正在运行：形状和纹理已经复制好了，但自定义工具只能在 SAI2 关闭时写入"
                         "（SAI2 退出时会覆盖工具设置）。请关闭 SAI2 后再安装一次来创建工具。")
        if legacy:
            lines.append("这个版本的 SAI2 早于 2020-01-07，不支持自定义工具文件，只安装了形状和纹理，"
                         "请在工具设置里手动选择它们。")
        if tool_error:
            lines.append(f"创建自定义工具失败（已撤销）：{tool_error}")
        if backups:
            lines.append(f"修改前的工具组文件已备份到：{backups[0].parent}")
        build = inst.extra.get("build", "")
        feats = set()
        if "scatter" in by_kind:
            feats.add("scatter_shape")
        if "brshape" in by_kind:
            feats.add("scatter_tool")
        if res.tools:
            feats.add("custool")
        need = fmt.required_build(feats)
        if build and need and build < need:
            lines.append(f"注意：当前 SAI2 版本（{build}）太旧，需要 {need} 或更新的版本才能使用这些形状，请升级 SAI2。")
        for s in res.skipped:
            lines.append(f"跳过：{s}")
        if not files and not res.assets and not res.existing:
            msg = "\n".join(lines) or "没有安装任何内容。"
            if blocked:
                msg = "SAI2 正在运行。请先关闭 SAI2 再安装（SAI2 退出时会覆盖自定义工具设置）。"
            return InstallResult(False, msg, needs_app_closed=blocked)
        lines.append("请重新启动 SAI2，新的笔刷才会出现。" if running else "启动 SAI2 后即可使用。")
        return InstallResult(True, "\n".join(lines), files=files, needs_app_closed=blocked,
                             backup=backups[0] if backups else None)

    def install_files(self, inst: AppInstall, files: list[Path]) -> InstallResult:
        """Install a package written by the SAI2 codec, or loose .saitdat / shape BMP(+INI) files."""
        brushes: list[Brush] = []
        loose: list[Path] = []
        for f in map(Path, files):
            if f.is_dir():
                brushes += fmt.read_settings(f, categories=("nrm",))
            elif f.suffix.lower() == ".saitdat":
                b = fmt.brush_from_tool(f)
                if b:
                    brushes.append(b)
            elif f.suffix.lower() == ".bmp":
                loose.append(f)
        used = {(a.kind, a.name) for b in brushes for a in b.source.native.get("sai2_assets", {}).values()}
        for f in loose:
            kind = fmt.asset_kind_of(f)
            if kind and (kind, f.stem) not in used:
                brushes.append(fmt.brush_from_asset(f, kind))
        if not brushes:
            return InstallResult(False, "这些文件里没有可以安装到 SAI2 的笔刷。")
        return self.install(inst, brushes)
