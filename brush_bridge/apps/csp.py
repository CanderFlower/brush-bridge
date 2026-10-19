"""Clip Studio Paint adapter.

Detection, listing of the user's sub tools (read from a *copy* of ``EditImageTool.todb``) and
installation. Default installation (strategy B) exports .sut / .abr files the user drags into the
Sub Tool palette. Strategy A (``install(..., direct=True)``) inserts sub tools straight into the
tool database while CSP is closed; it is experimental and only validated on copies.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Optional

from ..core.model import Brush, BrushRef, BrushSource, join_group
from ..core.plugin import AppAdapter, AppInstall, InstallResult, register_app
from ..core.system import IS_WINDOWS, backup, documents_dir, export_dir, file_version, safe_filename, unique_path
from ..formats import csp_schema as S
from ..formats.csp import _fingerprint, build_variant, loses_images, variant_to_brush, write_sut
from ..formats.csp_binary import decode_material_refs, pack_material_folder
from ..formats.csp_schema import material_folder, material_root, store_resolver

# Direct database installation has only been verified on copies of EditImageTool.todb (integrity
# check + re-reading the tree); keep it opt-in until users confirm CSP loads the result.
CSP_DIRECT_INSTALL_EXPERIMENTAL = True
DEFAULT_GROUP = "brush-bridge"

_REF_COLUMNS = ("BrushPatternImageArray", "TextureImage", "DualPatternImageArray", "DualTextureImage")
_DRAG_HINT = ("导入方法：打开 Clip Studio Paint，先在「工具」面板选中要放入的工具（例如「毛笔」），"
              "再把文件拖进「子工具」面板（注意不是「工具」面板）即可。")


def data_dirs() -> list[Path]:
    """Documents\\CELSYS\\CLIPStudioPaintVer*, newest first, only those holding a tool database."""
    base = documents_dir() / "CELSYS"
    if not base.is_dir():
        return []
    dirs = [d for d in base.glob("CLIPStudioPaintVer*") if (d / "Tool" / "EditImageTool.todb").exists()]
    return sorted(dirs, key=lambda d: [int(x) if x.isdigit() else 0 for x in d.name[18:].split("_")], reverse=True)


def snapshot_todb(todb: Path) -> dict:
    """Read the whole tool tree from a private copy of ``todb`` (the original is never opened)."""
    tmp = Path(tempfile.mkdtemp(prefix="bb_csp_"))
    try:
        copy = tmp / "EditImageTool.todb"
        shutil.copyfile(todb, copy)
        for suffix in ("-wal", "-journal"):
            side = Path(str(todb) + suffix)
            if side.exists():
                shutil.copyfile(side, Path(str(copy) + suffix))
        con = sqlite3.connect(copy)
        con.row_factory = sqlite3.Row
        try:
            mgr = dict(con.execute("SELECT * FROM Manager").fetchone() or {})
            vcols = S.table_columns(con, "Variant")
            variants = {r["VariantID"]: dict(r) for r in con.execute("SELECT * FROM Variant")}
            nodes = list(S.walk_tree(con))
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"manager": mgr, "variant_columns": vcols, "variants": variants, "nodes": nodes}


def _materials_for(variant: dict, root: Path) -> list[dict]:
    rows = []
    for col in _REF_COLUMNS:
        for ref in decode_material_refs(variant.get(col)):
            folder = material_folder(ref, root)
            data = pack_material_folder(folder) if folder else None
            if data:
                rows.append({"InstallFolder": int("Install" in ref.catalog.split(":")), "OriginalPath": ref.path,
                             "OldMaterial": 0, "FileData": data, "CatalogPath": ref.catalog, "MaterialUuid": ""})
    return rows


def load_installed_brush(snap: dict, node: S.TreeNode, root: Path, todb: Path = Path()) -> Brush:
    v = snap["variants"][node.row["NodeVariantID"]]
    b = variant_to_brush(v, node.row, store_resolver(root))
    b.group = join_group(*node.path)
    mgr = snap["manager"]
    b.source = BrushSource(app="csp", format="csp", version=str(mgr.get("Version") or ""), path=str(todb),
                           index=node.row.get("_PW_ID", -1))
    known = {c for c, _t, _d in S.VARIANT_COLUMNS} | {"VariantID"}
    cols = [(c, t) for c, t in snap["variant_columns"] if c in known or v.get(c) is not None]
    b.source.native = {"csp": {
        "manager": mgr, "node": dict(node.row), "variant": v,
        "init_variant": snap["variants"].get(node.row.get("NodeInitVariantID")),
        "variant_columns": cols, "materials": _materials_for(v, root), "fingerprint": _fingerprint(b),
        "origin": "todb"}}
    return b


@register_app
class CspAdapter(AppAdapter):
    id = "csp"
    name = "Clip Studio Paint"
    native_format = "csp"
    import_formats = ("csp", "abr")

    # -- detection -----------------------------------------------------------------------------
    def detect(self) -> list[AppInstall]:
        if not IS_WINDOWS:
            return []
        res = []
        dirs = data_dirs()
        for d in S.candidate_install_dirs():
            exe = d / "CLIPStudioPaint.exe"
            version = file_version(exe)
            data = dirs[0] if dirs else None
            inst = AppInstall(app_id=self.id, name=f"Clip Studio Paint {version}".strip(), version=version, exe=exe,
                              data_dir=data)
            inst.running = self.is_running(inst)
            inst.extra = {
                "todb": str(data / "Tool" / "EditImageTool.todb") if data else "",
                "material_root": str(material_root()),
                "factory_todb": str(S.factory_todb(d) or ""),
                "data_dirs": [str(x) for x in dirs],
            }
            res.append(inst)
        return res

    # -- listing -------------------------------------------------------------------------------
    def list_brushes(self, inst: AppInstall) -> list[BrushRef]:
        todb = Path(inst.extra.get("todb") or "")
        if not todb.is_file():
            return []
        snap = snapshot_todb(todb)
        root = Path(inst.extra.get("material_root") or material_root())
        resolve = store_resolver(root)
        refs = []
        for n in snap["nodes"]:
            v = snap["variants"].get(n.row.get("NodeVariantID"))
            if not S.is_brush_variant(v) or n.row.get("NodeInputOp") not in S.PAINT_OPS:
                continue
            tips = decode_material_refs(v.get("BrushPatternImageArray")) if v.get("BrushUsePatternImage") else []
            size = v.get("BrushSize")
            detail = f"{size:g} {'mm' if v.get('BrushSizeUnit') == 2 else 'px'}" if isinstance(size, (int, float)) else ""
            if tips:
                detail += f"，{len(tips)} 个笔尖素材"
            refs.append(BrushRef(
                name=n.name, key=n.uuid.hex(), group=join_group(*n.path),
                loader=(lambda n=n: load_installed_brush(snap, n, root, todb)),
                thumbnail=(lambda t=tips[:1]: resolve(t[0]) if t else None),
                detail=detail))
        return refs

    # -- installation --------------------------------------------------------------------------
    def install(self, inst: AppInstall, brushes: list[Brush], **options) -> InstallResult:
        if not brushes:
            return InstallResult(False, "没有选择任何笔刷。")
        if options.get("direct", False):
            return self._install_direct(inst, brushes, **options)
        return self._export(brushes, options.get("out_dir"), target_version=options.get("csp_version") or inst.version)

    def _export(self, brushes: list[Brush], out_dir=None, intro: str = "", target_version: str = "") -> InstallResult:
        out = Path(out_dir) if out_dir else export_dir("CSP")
        out.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        lossy: list[Brush] = []
        for b in brushes:
            dst = unique_path(out / f"{safe_filename(b.name)}.sut")
            if loses_images(b):
                lossy.append(b)
            files.append(write_sut(b, dst, target_version=target_version or ""))
        msg = (intro + "\n\n" if intro else "") + f"已导出 {len(files)} 个 Clip Studio Paint 子工具文件（.sut）到：\n{out}\n{_DRAG_HINT}"
        if lossy:
            from ..core.plugin import codec

            stamp = time.strftime("%m%d-%H%M%S")
            abr = out / f"{safe_filename(lossy[0].name if len(lossy) == 1 else 'BrushBridge')}_{stamp}.abr"
            try:
                files += codec("abr").write(lossy, abr)
                msg += (f"\n\n其中 {len(lossy)} 个笔刷的笔尖/纹理是新生成的 CSP 素材（已内嵌在 .sut 中）。"
                        f"万一你的 CSP 版本无法读取，可改为把备用文件 {abr.name} 拖进「子工具」面板。")
            except Exception as e:  # noqa: BLE001 - the .sut files are still usable
                msg += f"\n\n（额外导出 .abr 失败：{e}）"
        notes = sorted({n for b in brushes for n in b.notes})
        if notes:
            msg += "\n\n转换提示：\n· " + "\n· ".join(notes)
        return InstallResult(True, msg, files=files)

    def _install_direct(self, inst: AppInstall, brushes: list[Brush], **options) -> InstallResult:
        todb = Path(options.get("todb") or inst.extra.get("todb") or "")
        if not todb.is_file():
            return InstallResult(False, "找不到 Clip Studio Paint 的工具数据库（EditImageTool.todb），已改为导出文件。\n"
                                 + self._export(brushes, target_version=inst.version).message)
        if self.is_running(inst):
            return InstallResult(False, "请先关闭 Clip Studio Paint，再安装笔刷（直接写入工具设置需要软件处于关闭状态）。",
                                 needs_app_closed=True)
        root = Path(inst.extra.get("material_root") or material_root())
        direct, fallback = [], []
        for b in brushes:
            row, _cols, _m, _n = build_variant(b)
            missing = [r for c in _REF_COLUMNS for r in decode_material_refs(row.get(c)) if not material_folder(r, root)]
            (fallback if missing or loses_images(b) else direct).append(b)
        bak = None
        names: list[str] = []
        if direct:
            bak = backup(todb)
            try:
                names = direct_install(todb, direct, tool=options.get("tool"), group=options.get("group") or DEFAULT_GROUP)
            except Exception as e:  # noqa: BLE001
                shutil.copy2(bak, todb)
                return InstallResult(False, f"写入 Clip Studio Paint 工具数据库失败，已恢复原文件：{e}", backup=bak)
        group = options.get("group") or DEFAULT_GROUP
        msg = ""
        if names:
            msg = (f"已把 {len(names)} 个笔刷写入 Clip Studio Paint（实验功能），启动软件后可在「{options.get('tool') or '毛笔'}」"
                   f"工具的「{group}」子工具组中找到。\n原设置已备份到：{bak}")
        if fallback:
            res = self._export(fallback, options.get("out_dir"),
                               intro=msg + ("\n\n" if msg else "") + f"{len(fallback)} 个笔刷使用了素材，无法直接写入，已改为导出文件。",
                               target_version=inst.version)
            res.backup = bak
            return res
        notes = sorted({n for b in direct for n in b.notes})
        if notes:
            msg += "\n\n转换提示：\n· " + "\n· ".join(notes)
        return InstallResult(True, msg, backup=bak)

    def install_files(self, inst: AppInstall, files: list[Path]) -> InstallResult:
        files = [Path(f) for f in files]
        ok = [f for f in files if f.suffix.lower() in (".sut", ".abr")]
        if not ok:
            return InstallResult(False, "Clip Studio Paint 只能导入 .sut 子工具文件或 .abr 笔刷文件。", files=files)
        out = export_dir("CSP")
        produced: list[Path] = []
        notes = []
        for f in ok:
            dst = f if f.parent.resolve() == out.resolve() else unique_path(out / f.name)
            if f.suffix.lower() == ".abr":
                from ..core.plugin import codec

                info = codec("abr").sniff(f)
                if info and codec("abr").compatibility(info, self.id, inst.version):
                    from ..formats.abr import convert_to_v62

                    dst = unique_path(out / f"{f.stem}_v6.abr")
                    convert_to_v62(f, dst)
                    notes.append(f"{f.name} 是新版 ABR 格式，已自动转换为 Clip Studio Paint 能读取的 v6.2 格式。")
                    produced.append(dst)
                    continue
            if dst != f:
                shutil.copy2(f, dst)
            produced.append(dst)
        msg = f"文件已保存到：\n{out}\n{_DRAG_HINT}"
        if notes:
            msg += "\n\n" + "\n".join(notes)
        return InstallResult(True, msg, files=produced)


# ---------------------------------------------------------------------------------------------
# Strategy A: insert sub tools into EditImageTool.todb (CSP must be closed)
# ---------------------------------------------------------------------------------------------
def _chain(nodes: dict[bytes, dict], first) -> list[dict]:
    out, seen = [], set()
    u = bytes(first) if isinstance(first, (bytes, bytearray)) else b""
    while len(u) == 16 and u in nodes and u not in seen:
        seen.add(u)
        out.append(nodes[u])
        nxt = nodes[u].get("NodeNextUuid")
        u = bytes(nxt) if isinstance(nxt, (bytes, bytearray)) else b""
    return out


def _insert(con: sqlite3.Connection, table: str, row: dict, cols: list[str]) -> None:
    cols = [c for c in cols if c != "_PW_ID"]
    con.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols])


def direct_install(todb: Path, brushes: list[Brush], tool: Optional[str] = None,
                   group: str = DEFAULT_GROUP) -> list[str]:
    """Append ``brushes`` as sub tools of group ``group`` under ``tool`` (default: the Brush tool)."""
    con = sqlite3.connect(todb)
    con.row_factory = sqlite3.Row
    try:
        ncols = [c for c, _ in S.table_columns(con, "Node")]
        vcols = [c for c, _ in S.table_columns(con, "Variant")]
        nodes = {bytes(r["NodeUuid"]): dict(r) for r in con.execute("SELECT * FROM Node")}
        mgr = dict(con.execute("SELECT * FROM Manager").fetchone())
        root = nodes[bytes(mgr["RootUuid"])]
        tools = _chain(nodes, root["NodeFirstChildUuid"])
        target = next((t for t in tools if tool and t.get("NodeName") == tool), None)
        target = target or next((t for t in tools if t.get("NodeDefaultIdentifier") == S.TOOL_BRUSH), None)
        if target is None:
            raise RuntimeError("找不到「毛笔」工具")
        groups = _chain(nodes, target["NodeFirstChildUuid"])
        variants = {r["VariantID"]: dict(r) for r in con.execute("SELECT * FROM Variant")}

        def template(op: int) -> tuple[dict, dict]:
            pool = [n for g in groups for n in _chain(nodes, g["NodeFirstChildUuid"])] + list(nodes.values())
            for n in pool:
                v = variants.get(n.get("NodeVariantID"))
                if n.get("NodeInputOp") == op and S.is_brush_variant(v):
                    return n, v
            for n in pool:
                v = variants.get(n.get("NodeVariantID"))
                if n.get("NodeInputOp") in S.PAINT_OPS and S.is_brush_variant(v):
                    return n, v
            raise RuntimeError("找不到可作为模板的笔刷子工具")

        max_vid = max(int(mgr.get("MaxVariantID") or 0), max((k for k in variants if isinstance(k, int)), default=0))
        grp = next((g for g in groups if g.get("NodeName") == group), None)
        with con:
            if grp is None:
                tmpl = next((g for g in groups), None) or target
                grp = dict(tmpl)
                grp.update({"NodeUuid": S.new_uuid(), "NodeName": group, "NodeInstalledState": 0, "NodeInstalledVersion": 0,
                            "NodeDefaultIdentifier": 0, "NodeVariantID": 0, "NodeInitVariantID": 0, "NodeShortCutKey": 0,
                            "NodeHidden": 0, "NodeLock": 0, "NodeNextUuid": S.EMPTY_UUID,
                            "NodeFirstChildUuid": S.EMPTY_UUID, "NodeSelectedUuid": S.EMPTY_UUID})
                _insert(con, "Node", grp, ncols)
                if groups:
                    con.execute("UPDATE Node SET NodeNextUuid=? WHERE NodeUuid=?", (grp["NodeUuid"], groups[-1]["NodeUuid"]))
                else:
                    con.execute("UPDATE Node SET NodeFirstChildUuid=?, NodeSelectedUuid=? WHERE NodeUuid=?",
                                (grp["NodeUuid"], grp["NodeUuid"], target["NodeUuid"]))
                nodes[bytes(grp["NodeUuid"])] = grp
            children = _chain(nodes, grp.get("NodeFirstChildUuid"))
            last = children[-1]["NodeUuid"] if children else None
            names = []
            for b in brushes:
                row, _cols, _mats, _nat = build_variant(b)
                op = S.OP_ERASER if b.is_eraser else S.OP_BRUSH
                tnode, tvar = template(op)
                var = dict(tvar)
                for k, val in row.items():
                    if k in var and k not in ("VariantID", "_PW_ID"):
                        var[k] = val
                node = dict(tnode)
                node.update({"NodeUuid": S.new_uuid(), "NodeName": b.name, "NodeShortCutKey": 0, "NodeLock": 0,
                             "NodeHidden": 0, "NodeInstalledState": 0, "NodeInstalledVersion": 0,
                             "NodeDefaultIdentifier": 0, "NodeInputOp": op, "NodeOutputOp": op,
                             "NodeIcon": S.ICON_ERASER if b.is_eraser else S.ICON_BRUSH,
                             "NodeMaterialIDExists": 0, "NodeMaterialContentID": 0, "NodeMaterialUuid": "",
                             "NodeNextUuid": S.EMPTY_UUID, "NodeFirstChildUuid": S.EMPTY_UUID,
                             "NodeSelectedUuid": S.EMPTY_UUID, "NodeVariantID": max_vid + 1,
                             "NodeInitVariantID": max_vid + 2, "NodeCustomIcon": None})
                for vid in (max_vid + 1, max_vid + 2):
                    var["VariantID"] = vid
                    _insert(con, "Variant", var, vcols)
                max_vid += 2
                _insert(con, "Node", node, ncols)
                if last is None:
                    con.execute("UPDATE Node SET NodeFirstChildUuid=?, NodeSelectedUuid=? WHERE NodeUuid=?",
                                (node["NodeUuid"], node["NodeUuid"], grp["NodeUuid"]))
                else:
                    con.execute("UPDATE Node SET NodeNextUuid=? WHERE NodeUuid=?", (node["NodeUuid"], last))
                last = node["NodeUuid"]
                names.append(b.name)
            con.execute("UPDATE Manager SET MaxVariantID=?", (max_vid,))
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"数据库校验失败：{ok}")
        return names
    finally:
        con.close()
