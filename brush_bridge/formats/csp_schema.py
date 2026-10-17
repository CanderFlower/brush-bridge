"""Clip Studio Paint tool-database schema knowledge shared by the .sut codec and the CSP adapter.

A ``.sut`` file (one exported sub tool) and the user's ``EditImageTool.todb`` (all tools) are
SQLite databases of the same family: ``Manager`` (one row), ``Node`` (tool tree, linked lists of
16-byte UUIDs), ``Variant`` (one row per parameter set; the column set grows with every CSP
release) and, in .sut files only, ``MaterialFile`` (embedded brush-tip / texture materials).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import uuid as _uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional

from PIL import Image

from .csp_binary import MaterialRef, encode_effector, material_dir_parts

SQLITE_MAGIC = b"SQLite format 3\x00"
EMPTY_UUID = b"\x00"  # CSP stores "no node" as a single zero byte

# Manager.Version anchors that were verified on real installs. Each CSP release that adds tool
# parameters bumps it; Node.NodeInstalledVersion of factory sub tools uses the same counter.
VERIFIED_VERSIONS = {145: "5.1"}
DEFAULT_WRITE_VERSION = 142  # verified to import into CSP 5.1.2 (sample exported by an older CSP)

# Constants that are identical in every CSP tool database we have seen.
COMMON_VARIANT_ID = 44
OBJECT_NODE_UUID = bytes.fromhex("6a6b33e53d5b49269b660555acd5e2c7")

# Top-level tool identifiers (Node.NodeDefaultIdentifier of tool nodes)
TOOL_PEN, TOOL_PENCIL, TOOL_BRUSH, TOOL_AIRBRUSH, TOOL_DECORATION, TOOL_ERASER, TOOL_BLEND = 20, 21, 22, 23, 24, 25, 26
TOOL_NAMES = {20: "Pen", 21: "Pencil", 22: "Brush", 23: "Airbrush", 24: "Decoration", 25: "Eraser", 26: "Blend"}

# Node.NodeInputOp / NodeOutputOp of drawing sub tools: 10 brush, 11 pen, 12 eraser, 13 copy stamp, 14 dot pen
PAINT_OPS = {10, 11, 12, 13, 14}
OP_BRUSH, OP_PEN, OP_ERASER = 10, 11, 12
ICON_BRUSH, ICON_PEN, ICON_ERASER = 103, 101, 106

MANAGER_COLUMNS = [
    ("ToolType", "INTEGER"), ("Version", "INTEGER"), ("RootUuid", "BLOB"), ("CurrentNodeUuid", "BLOB"),
    ("MaxVariantID", "INTEGER"), ("CommonVariantID", "INTEGER"), ("ObjectNodeUuid", "BLOB"),
    ("PressureGraph", "BLOB"), ("SavedCount", "INTEGER"), ("PressureGraphInitialized", "INTEGER"),
    ("UsePressureFile", "INTEGER"),
]
NODE_COLUMNS = [
    ("NodeUuid", "BLOB"), ("NodeName", "TEXT"), ("NodeShortCutKey", "INTEGER"), ("NodeLock", "INTEGER"),
    ("NodeInputOp", "INTEGER"), ("NodeOutputOp", "INTEGER"), ("NodeRangeOp", "INTEGER"), ("NodeIcon", "INTEGER"),
    ("NodeIconColor", "INTEGER"), ("NodeHidden", "INTEGER"), ("NodeInstalledState", "INTEGER"),
    ("NodeInstalledVersion", "INTEGER"), ("NodeHideByGrade", "INTEGER"), ("NodeDefaultIdentifier", "INTEGER"),
    ("NodeMaterialIDExists", "INTEGER"), ("NodeMaterialContentID", "INTEGER"), ("NodeMaterialUuid", "TEXT"),
    ("NodeNextUuid", "BLOB"), ("NodeFirstChildUuid", "BLOB"), ("NodeSelectedUuid", "BLOB"),
    ("NodeVariantID", "INTEGER"), ("NodeInitVariantID", "INTEGER"), ("NodeCustomIcon", ""),
]
MATERIAL_COLUMNS = [
    ("InstallFolder", "INTEGER"), ("OriginalPath", "TEXT"), ("OldMaterial", "INTEGER"), ("FileData", "BLOB"),
    ("CatalogPath", "TEXT"), ("MaterialUuid", "TEXT"),
]

_VARIANT_SHOW_PARAM = bytes.fromhex("0000000c0000000700000004000003e8000000140000041a0000041c000003fc000000280000047f")
_IN_OUT_TARGET = bytes.fromhex(
    "0000000c000000150000000c00000070" + "".join(
        f"0000000000000000{x:08x}" for x in (
            0x3e9, 0x3fd, 0x411, 0x41b, 0x43a, 0x43c, 0x445, 0x44c, 0x450, 0x452, 0x454, 0x46c, 0x46e,
            0xc3c0, 0xc739, 0xc74d, 0xc761, 0xc76b, 0xc7bc, 0xc7be)) + "0000000000000000")

# Columns of a brush-type sub tool as exported by CSP (.sut, Manager.Version 142) with neutral
# defaults (most common value among the factory brush sub tools). "EFFECTOR" = neutral effector.
VARIANT_COLUMNS: list[tuple[str, str, object]] = [
    ('VariantShowSeparator', 'INTEGER', 0),
    ('VariantShowParam', 'BLOB', None),
    ('Opacity', 'INTEGER', 100),
    ('AntiAlias', 'INTEGER', 1),
    ('CompositeMode', 'INTEGER', 0),
    ('FlickerReduction', 'INTEGER', 0),
    ('FlickerReductionBySpeed', 'INTEGER', 0),
    ('FlickerReductionBySpeedType', 'INTEGER', 1),
    ('Stickness', 'INTEGER', 0),
    ('TextureImage', '', None),
    ('TextureCompositeMode', 'INTEGER', 0),
    ('TextureReverseDensity', 'INTEGER', 0),
    ('TextureStressDensity', 'INTEGER', 0),
    ('TextureScale2', 'REAL', 100.0),
    ('TextureRotate', 'REAL', 0.0),
    ('TextureBrightness', 'INTEGER', 0),
    ('TextureContrast', 'INTEGER', 0),
    ('BrushSize', 'REAL', 80.0),
    ('BrushSizeUnit', 'INTEGER', 0),
    ('BrushSizeEffector', 'BLOB', 'EFFECTOR'),
    ('BrushSizeSyncViewScale', 'INTEGER', 0),
    ('BrushAtLeast1Pixel', 'INTEGER', 0),
    ('BrushOpacityEffector', 'BLOB', 'EFFECTOR'),
    ('BrushFlow', 'INTEGER', 100),
    ('BrushFlowEffector', 'BLOB', 'EFFECTOR'),
    ('BrushAdjustFlowByInterval', 'INTEGER', 0),
    ('BrushHardness', 'INTEGER', 100),
    ('BrushInterval', 'REAL', 10.0),
    ('BrushIntervalEffector', 'BLOB', 'EFFECTOR'),
    ('BrushAutoIntervalType', 'INTEGER', 0),
    ('BrushContinuousPlot', 'INTEGER', 0),
    ('BrushThickness', 'INTEGER', 100),
    ('BrushThicknessEffector', 'BLOB', 'EFFECTOR'),
    ('BrushVerticalThicknes', 'INTEGER', 0),
    ('BrushRotation', 'REAL', 0.0),
    ('BrushRotationEffector', 'INTEGER', 3),
    ('BrushRotationRandomScale', 'INTEGER', 100),
    ('BrushRotationInSpray', 'REAL', 0.0),
    ('BrushRotationEffectorInSpray', 'INTEGER', 1),
    ('BrushRotationRandomInSpray', 'INTEGER', 100),
    ('BrushUsePatternImage', 'INTEGER', 1),
    ('BrushPatternImageArray', 'BLOB', None),
    ('BrushPatternOrderType', 'INTEGER', 0),
    ('BrushPatternOrderType2', 'INTEGER', 0),
    ('BrushPatternReverseHorizontal', 'INTEGER', 0),
    ('BrushPatternReverseVertical', 'INTEGER', 0),
    ('BrushPatternNameVersion', 'INTEGER', 0),
    ('TextureForPlot', 'INTEGER', 0),
    ('TextureDensity', 'INTEGER', 50),
    ('TextureDensityEffector', 'BLOB', 'EFFECTOR'),
    ('BrushUseWaterColor', 'INTEGER', 0),
    ('BrushUseWaterColor2', 'INTEGER', 0),
    ('BrushWaterColor', 'INTEGER', 0),
    ('BrushMixColor', 'INTEGER', 50),
    ('BrushMixColorEffector', 'BLOB', 'EFFECTOR'),
    ('BrushColorMixingMode', 'INTEGER', 0),
    ('BrushLMSLinearity', 'INTEGER', 0),
    ('BrushMixAlpha', 'INTEGER', 50),
    ('BrushMixAlphaEffector', 'BLOB', 'EFFECTOR'),
    ('BrushMixColorExtension', 'INTEGER', 10),
    ('BrushBlurLinkSize', 'INTEGER', 0),
    ('BrushBlur', 'REAL', 5.0),
    ('BrushBlurUnit', 'INTEGER', 0),
    ('BrushBlurEffector', 'BLOB', 'EFFECTOR'),
    ('BrushChangePatternColor', 'INTEGER', 0),
    ('BrushHueChange', 'INTEGER', 0),
    ('BrushHueChangeEffector', 'BLOB', 'EFFECTOR'),
    ('BrushSaturationChange', 'INTEGER', 0),
    ('BrushSaturationChangeEffector', 'BLOB', 'EFFECTOR'),
    ('BrushValueChange', 'INTEGER', 0),
    ('BrushValueChangeEffector', 'BLOB', 'EFFECTOR'),
    ('BrushSubColor', 'INTEGER', 0),
    ('BrushSubColorEffector', 'BLOB', 'EFFECTOR'),
    ('BrushChangeStrokeColor', 'INTEGER', 0),
    ('BrushStrokeHueChange', 'INTEGER', 0),
    ('BrushStrokeSaturationChange', 'INTEGER', 0),
    ('BrushStrokeValueChange', 'INTEGER', 0),
    ('BrushStrokeSubColor', 'INTEGER', 0),
    ('BrushChangeColorTarget', 'INTEGER', 1),
    ('BrushRibbon', 'INTEGER', 0),
    ('BrushBlendPatternByDarken', 'INTEGER', 0),
    ('BrushUseSpray', 'INTEGER', 0),
    ('BrushSpraySize', 'REAL', 2.0),
    ('BrushSpraySizeUnit', 'INTEGER', 0),
    ('BrushSpraySizeEffector', 'BLOB', 'EFFECTOR'),
    ('BrushSpraySizeSyncBrushSize', 'INTEGER', 0),
    ('BrushSprayDensity', 'INTEGER', 5),
    ('BrushSprayDensityEffector', 'BLOB', 'EFFECTOR'),
    ('BrushSprayBias', 'INTEGER', 0),
    ('BrushSprayUseFixedPoint', 'INTEGER', 0),
    ('BrushSprayFixedPointArray', '', None),
    ('BrushUseRevision', 'INTEGER', 0),
    ('BrushRevision', 'INTEGER', 10),
    ('BrushRevisionBySpeed', 'INTEGER', 1),
    ('BrushRevisionByViewScale', 'INTEGER', 1),
    ('BrushRevisionBezier', 'INTEGER', 0),
    ('BrushInOutTarget', 'BLOB', None),
    ('BrushInOutType', 'INTEGER', 0),
    ('BrushInOutBySpeed', 'INTEGER', 1),
    ('BrushUseIn', 'INTEGER', 1),
    ('BrushInLength', 'REAL', 20.0),
    ('BrushInLengthUnit', 'INTEGER', 0),
    ('BrushInRatio', 'REAL', 30.0),
    ('BrushUseOut', 'INTEGER', 1),
    ('BrushOutLength', 'REAL', 20.0),
    ('BrushOutLengthUnit', 'INTEGER', 0),
    ('BrushOutRatio', 'REAL', 30.0),
    ('BrushSharpenCorner', 'INTEGER', 0),
    ('BrushUseWaterEdge', 'INTEGER', 0),
    ('BrushWaterEdgeRadius', 'REAL', 3.0),
    ('BrushWaterEdgeRadiusUnit', 'INTEGER', 0),
    ('BrushWaterEdgeAlphaPower', 'INTEGER', 20),
    ('BrushWaterEdgeValuePower', 'INTEGER', 0),
    ('BrushWaterEdgeAfterDrag', 'INTEGER', 0),
    ('BrushWaterEdgeBlur', 'REAL', 0.0),
    ('BrushWaterEdgeBlurUnit', 'INTEGER', 0),
    ('BrushUseVectorEraser', 'INTEGER', 0),
    ('BrushVectorEraserType', 'INTEGER', 0),
    ('BrushVectorEraserReferAllLayer', 'INTEGER', 1),
    ('BrushEraseAllLayer', 'INTEGER', 0),
    ('BrushEnableSnap', 'INTEGER', 1),
    ('BrushUseVectorMagnet', 'INTEGER', 0),
    ('BrushVectorMagnetPower', 'INTEGER', 30),
    ('BrushUseReferLayer', 'INTEGER', 0),
    ('BrushAdjustVelocity', 'INTEGER', 1),
    ('UseDualBrush', 'INTEGER', 0),
    ('DualBrushCompositeMode', 'INTEGER', 1),
    ('ChangeRGBByDual', 'INTEGER', 0),
    ('SyncDualBrushSize', 'INTEGER', 0),
    ('DualAntiAlias', 'INTEGER', 2),
    ('DualSize', 'REAL', 30.0),
    ('DualSizeUnit', 'INTEGER', 0),
    ('DualSizeEffector', 'BLOB', 'EFFECTOR'),
    ('DualFlow', 'INTEGER', 100),
    ('DualFlowEffector', 'BLOB', 'EFFECTOR'),
    ('DualAdjustFlowByInterval', 'INTEGER', 1),
    ('DualHardness', 'INTEGER', 100),
    ('DualInterval', 'REAL', 10.0),
    ('DualIntervalEffector', 'BLOB', 'EFFECTOR'),
    ('DualAutoIntervalType', 'INTEGER', 2),
    ('DualContinuousPlot', 'INTEGER', 0),
    ('DualThickness', 'INTEGER', 100),
    ('DualThicknessEffector', 'BLOB', 'EFFECTOR'),
    ('DualVerticalThicknes', 'INTEGER', 0),
    ('DualRotation', 'REAL', 0.0),
    ('DualRotationEffector', 'INTEGER', 3),
    ('DualRotationRandomScale', 'INTEGER', 100),
    ('DualRotationInSpray', 'REAL', 0.0),
    ('DualRotationEffectorInSpray', 'INTEGER', 1),
    ('DualRotationRandomInSpray', 'INTEGER', 100),
    ('DualUsePatternImage', 'INTEGER', 0),
    ('DualPatternImageArray', '', None),
    ('DualPatternOrderType', 'INTEGER', 0),
    ('DualPatternReverseHorizontal', 'INTEGER', 0),
    ('DualPatternReverseVertical', 'INTEGER', 0),
    ('DualTextureImage', 'BLOB', None),
    ('DualTextureCompositeMode', 'INTEGER', 0),
    ('DualTextureReverseDensity', 'INTEGER', 0),
    ('DualTextureStressDensity', 'INTEGER', 0),
    ('DualTextureScale', 'REAL', 100.0),
    ('DualTextureRotate', 'REAL', 0.0),
    ('DualTextureBrightness', 'INTEGER', 0),
    ('DualTextureContrast', 'INTEGER', 0),
    ('DualTextureForPlot', 'INTEGER', 0),
    ('DualTextureDensity', 'INTEGER', 50),
    ('DualTextureDensityEffector', 'BLOB', 'EFFECTOR'),
    ('DualRibbon', 'INTEGER', 0),
    ('DualBlendPatternByDarken', 'INTEGER', 0),
    ('DualBlendPatternByDarken2', 'INTEGER', 0),
    ('DualUseSpray', 'INTEGER', 0),
    ('DualSpraySize', 'REAL', 2.0),
    ('DualSpraySizeUnit', 'INTEGER', 0),
    ('DualSpraySizeEffector', 'BLOB', 'EFFECTOR'),
    ('DualSpraySizeSyncBrushSize', 'INTEGER', 0),
    ('DualSprayDensity', 'INTEGER', 1),
    ('DualSprayDensityEffector', 'BLOB', 'EFFECTOR'),
    ('DualSprayBias', 'INTEGER', 0),
    ('DualSprayUseFixedPoint', 'INTEGER', 0),
    ('DualSprayFixedPointArray', '', None),
    ('FillReferVectorCenter', 'INTEGER', 0),
    ('FillUseExpand', 'INTEGER', 0),
    ('FillExpandLength', 'REAL', 10.0),
    ('FillExpandLengthUnit', 'INTEGER', 0),
    ('FillExpandType', 'INTEGER', 2),
    ('FillColorMargin', 'REAL', 10.0),
]


def variant_defaults() -> dict[str, object]:
    out: dict[str, object] = {}
    for name, _typ, val in VARIANT_COLUMNS:
        if val == "EFFECTOR":
            val = encode_effector()
        elif name == "VariantShowParam":
            val = _VARIANT_SHOW_PARAM
        elif name == "BrushInOutTarget":
            val = _IN_OUT_TARGET
        out[name] = val
    return out


def column_ddl(cols: list[tuple[str, str]]) -> str:
    parts = ["_PW_ID INTEGER PRIMARY KEY AUTOINCREMENT"]
    for name, typ in cols:
        parts.append(f"{name} {typ or 'NULL'} DEFAULT NULL")
    return ", ".join(parts)


def create_sut_schema(con: sqlite3.Connection, variant_cols: list[tuple[str, str]]) -> None:
    con.execute(f"CREATE TABLE Manager({column_ddl(MANAGER_COLUMNS)})")
    con.execute(f"CREATE TABLE Node({column_ddl(NODE_COLUMNS)})")
    con.execute(f"CREATE TABLE Variant({column_ddl(variant_cols)})")
    con.execute(f"CREATE TABLE MaterialFile({column_ddl(MATERIAL_COLUMNS)})")
    con.execute("CREATE TABLE UnicornData (uid integer PRIMARY KEY not null,data varchar, date varchar)")


def new_uuid() -> bytes:
    return _uuid.uuid4().bytes


def is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open a database without ever taking a write lock (immutable=1 skips locking entirely)."""
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def table_columns(con: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    return [(r[1], r[2] or "") for r in con.execute(f"PRAGMA table_info({table})") if r[1] != "_PW_ID"]


def version_label(manager_version: int, installed: Optional["Schema"] = None) -> str:
    if manager_version in VERIFIED_VERSIONS:
        return f"Clip Studio Paint {VERIFIED_VERSIONS[manager_version]}"
    if installed and installed.version:
        if manager_version == installed.version:
            return f"与已安装的 Clip Studio Paint {installed.app_version} 相同"
        if manager_version > installed.version:
            return f"比已安装的 Clip Studio Paint {installed.app_version} 更新"
        return f"比已安装的 Clip Studio Paint {installed.app_version} 更旧"
    best = max((v for v in VERIFIED_VERSIONS if v <= manager_version), default=None)
    if best is None:
        return "早于 Clip Studio Paint 5.1"
    return f"Clip Studio Paint {VERIFIED_VERSIONS[best]} 或更新"


# ---------------------------------------------------------------------------------------------
# Node tree
# ---------------------------------------------------------------------------------------------
@dataclass
class TreeNode:
    row: dict
    depth: int
    path: list[str] = field(default_factory=list)  # names of ancestors (excluding the root)
    tool_id: int = 0  # NodeDefaultIdentifier of the top-level tool

    @property
    def uuid(self) -> bytes:
        return self.row["NodeUuid"]

    @property
    def name(self) -> str:
        return self.row.get("NodeName") or ""


def _valid(u) -> bool:
    return isinstance(u, (bytes, bytearray)) and len(u) == 16


def walk_tree(con: sqlite3.Connection) -> Iterator[TreeNode]:
    """Depth-first walk following RootUuid -> NodeFirstChildUuid / NodeNextUuid."""
    rows = {}
    for r in con.execute("SELECT * FROM Node"):
        d = dict(r)
        if _valid(d.get("NodeUuid")):
            rows[bytes(d["NodeUuid"])] = d
    mgr = con.execute("SELECT RootUuid FROM Manager").fetchone()
    root = bytes(mgr[0]) if mgr and _valid(mgr[0]) else None
    if root not in rows:
        # A .sut without a proper root: just yield every node at depth 0
        for d in rows.values():
            yield TreeNode(d, 0)
        return
    seen: set[bytes] = set()

    def rec(u: bytes, depth: int, path: list[str], tool_id: int) -> Iterator[TreeNode]:
        while _valid(u) and u in rows and u not in seen:
            seen.add(u)
            d = rows[u]
            tid = (d.get("NodeDefaultIdentifier") or 0) if depth == 1 else tool_id
            node = TreeNode(d, depth, path, tid)
            yield node
            child = d.get("NodeFirstChildUuid")
            if _valid(child):
                yield from rec(bytes(child), depth + 1, path + [d.get("NodeName") or ""], tid)
            nxt = d.get("NodeNextUuid")
            u = bytes(nxt) if _valid(nxt) else b""

    yield from rec(root, 0, [], 0)


def is_brush_variant(v: Optional[dict]) -> bool:
    return bool(v) and v.get("BrushSize") is not None


# ---------------------------------------------------------------------------------------------
# Installed CSP schema (for compatibility checks)
# ---------------------------------------------------------------------------------------------
@dataclass
class Schema:
    version: int
    app_version: str
    variant_columns: set[str]
    node_columns: set[str]
    source: Path


def candidate_install_dirs() -> list[Path]:
    dirs: list[Path] = []
    try:
        from ..core.system import find_programs

        for p in find_programs(r"CLIP STUDIO"):
            if p.location:
                dirs.append(Path(p.location))
    except Exception:
        pass
    for root in {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", ""),
                 os.environ.get("ProgramFiles(x86)", "")}:
        if root:
            base = Path(root) / "CELSYS"
            if base.is_dir():
                dirs += [d for d in base.glob("CLIP STUDIO*") if d.is_dir()]
    out, seen = [], set()
    for d in dirs:
        for cand in (d / "CLIP STUDIO PAINT", d):
            if (cand / "CLIPStudioPaint.exe").exists() and str(cand).lower() not in seen:
                seen.add(str(cand).lower())
                out.append(cand)
    return out


def factory_todb(install_dir: Path) -> Optional[Path]:
    base = Path(install_dir) / "Settings" / "PAINT" / "Tool"
    for lang in ("english", "japanese", "chinese_sc", "other"):
        p = base / lang / "EditImageTool.todb"
        if p.exists():
            return p
    hits = sorted(base.glob("*/EditImageTool.todb")) if base.is_dir() else []
    return hits[0] if hits else None


def read_schema(todb: Path, app_version: str = "") -> Optional[Schema]:
    """Read Manager.Version + column sets from a tool DB via a private copy (never locks it)."""
    tmp = Path(tempfile.mkdtemp(prefix="bb_csp_"))
    try:
        copy = tmp / "schema.todb"
        shutil.copyfile(todb, copy)
        con = sqlite3.connect(copy)
        try:
            ver = con.execute("SELECT Version FROM Manager").fetchone()[0] or 0
            vcols = {c for c, _ in table_columns(con, "Variant")}
            ncols = {c for c, _ in table_columns(con, "Node")}
        finally:
            con.close()
        return Schema(int(ver), app_version, vcols, ncols, Path(todb))
    except (OSError, sqlite3.Error, TypeError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@lru_cache(maxsize=4)
def installed_schema(app_version: str = "") -> Optional[Schema]:
    """Schema of the installed CSP (matching ``app_version`` when several are installed)."""
    from ..core.system import file_version

    found: list[Schema] = []
    for d in candidate_install_dirs():
        todb = factory_todb(d)
        if not todb:
            continue
        ver = file_version(d / "CLIPStudioPaint.exe")
        s = read_schema(todb, ver)
        if s:
            found.append(s)
            if app_version and ver == app_version:
                return s
    if not found:
        return None
    return min(found, key=lambda s: s.version) if not app_version else found[0]


# ---------------------------------------------------------------------------------------------
# Downgrade (make a newer .sut loadable by an older CSP)
# ---------------------------------------------------------------------------------------------
def downgrade_sut(src: Path, dst: Path, target: Schema) -> Path:
    """Copy ``src`` to ``dst`` dropping columns unknown to ``target`` and lowering version fields."""
    shutil.copyfile(src, dst)
    con = sqlite3.connect(dst)
    try:
        for table, known in (("Variant", target.variant_columns), ("Node", target.node_columns)):
            cols = table_columns(con, table)
            keep = [(c, t) for c, t in cols if c in known]
            if len(keep) == len(cols):
                continue
            names = ", ".join(["_PW_ID"] + [c for c, _ in keep])
            con.execute(f"CREATE TABLE {table}__bb({column_ddl(keep)})")
            con.execute(f"INSERT INTO {table}__bb({names}) SELECT {names} FROM {table}")
            con.execute(f"DROP TABLE {table}")
            con.execute(f"ALTER TABLE {table}__bb RENAME TO {table}")
        con.execute("UPDATE Manager SET Version=? WHERE Version>?", (target.version, target.version))
        con.execute("UPDATE Node SET NodeInstalledVersion=? WHERE NodeInstalledVersion>?", (target.version, target.version))
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    return dst


# ---------------------------------------------------------------------------------------------
# Local material library (Documents\CELSYS\CLIPStudioCommon\Material), read-only
# ---------------------------------------------------------------------------------------------
def material_root() -> Path:
    from ..core.system import documents_dir

    return documents_dir() / "CELSYS" / "CLIPStudioCommon" / "Material"


def material_folder(ref: MaterialRef, root: Path) -> Optional[Path]:
    parts = material_dir_parts(ref.path)
    if not parts:
        return None
    for cand in (root.joinpath(*parts), root.joinpath("Install", *parts)):
        if cand.is_dir():
            return cand
    return None


def store_resolver(root: Path):
    cache: dict[str, Optional[Image.Image]] = {}

    def resolve(ref: MaterialRef) -> Optional[Image.Image]:
        if ref.path not in cache:
            im = None
            folder = material_folder(ref, root)
            thumb = folder / "thumbnail" / "thumbnail.png" if folder else None
            if thumb and thumb.is_file():
                try:
                    with Image.open(thumb) as f:
                        im = f.copy()
                except OSError:
                    im = None
            cache[ref.path] = im
        return cache[ref.path]

    return resolve
