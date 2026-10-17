# Clip Studio Paint tool databases (.sut / EditImageTool.todb)

These are notes from reverse-engineering Clip Studio Paint (CSP) 5.1.2 on Windows and sub tools
exported by older CSP versions. Nothing here comes from CELSYS documentation. Statements marked
**(inferred)** are based on value patterns and have not been confirmed inside the CSP UI.

Code: `brush_bridge/formats/csp.py` (codec), `csp_schema.py` (schema and tree), `csp_binary.py`
(blobs and materials), `brush_bridge/apps/csp.py` (adapter).

## Files

| File | Where | What |
|---|---|---|
| `*.sut` | exported by *Sub tool palette → Export sub tool* | one sub tool, SQLite 3 |
| `EditImageTool.todb` | `Documents\CELSYS\CLIPStudioPaintVer1_5_0\Tool\` | all of the user's tools, SQLite 3 |
| factory `EditImageTool.todb` | `<install>\CLIP STUDIO PAINT\Settings\PAINT\Tool\<language>\` | initial tool set for this version; read to learn the installed schema |
| material library | `Documents\CELSYS\CLIPStudioCommon\Material\` | brush tips, textures and other materials |

CSP keeps `EditImageTool.todb` open while it runs. brush-bridge only ever reads a **copy** of it.
SQLite is opened with `immutable=1` or on a temp copy, so CSP never sees a lock. The only time
brush-bridge writes to it is the opt-in direct install, and that refuses to run while CSP is open.

## Schema

Every table has `_PW_ID INTEGER PRIMARY KEY AUTOINCREMENT`. Columns are declared as
`Name TYPE DEFAULT NULL`, and untyped columns as `Name NULL DEFAULT NULL`.

* **Manager** (1 row): `ToolType, Version, RootUuid, CurrentNodeUuid, MaxVariantID, CommonVariantID,
  ObjectNodeUuid, PressureGraph, SavedCount, PressureGraphInitialized, UsePressureFile`.
  * `CommonVariantID` (44) and `ObjectNodeUuid` (`6a6b33e5…acd5e2c7`) are the same in every file we saw.
  * `MaxVariantID` is a monotonic counter. It can be larger than `max(Variant.VariantID)`.
* **Node**: the tool tree.
  * `NodeUuid` is 16 bytes. "No node" is a **single zero byte** `x'00'`, not NULL.
  * Links are `NodeFirstChildUuid`, `NodeNextUuid` (siblings) and `NodeSelectedUuid`.
  * Each parameter set has two IDs: `NodeVariantID` (current) and `NodeInitVariantID` (the values
    restored by "reset to initial settings").
  * Other columns: `NodeName, NodeInputOp, NodeOutputOp, NodeIcon, NodeInstalledState,
    NodeInstalledVersion, NodeDefaultIdentifier, NodeMaterial*, NodeCustomIcon`.
* **Variant**: one row per parameter set (`VariantID`). This is where the brush settings live.
  * A .sut contains only the columns that matter for that sub tool (186 in a CSP 4.x-era pen).
  * A todb contains every column (506 in the 5.1.2 user DB, 507 in the factory DB). Columns that
    don't apply are NULL.
* **MaterialFile** (.sut only): `InstallFolder, OriginalPath, OldMaterial, FileData, CatalogPath, MaterialUuid`.
* **UnicornData** (.sut only): `uid integer PRIMARY KEY not null, data varchar, date varchar`. It is empty.

### Tree

`Manager.RootUuid` points to the root, which has an empty name. Below it:

```
root → tool (depth 1, e.g. "Brush") → sub tool group (depth 2) → sub tool (depth 3)
```

* **Tools** are recognised by `NodeDefaultIdentifier`:
  * 10 zoom, 11 move, 12 operation, 13 move layer, 14 selection, 15 auto select, 16 eyedropper
  * 20 pen, 21 pencil, 22 brush, 23 airbrush, 24 decoration, 25 eraser, 26 blend, 27 liquify
  * 30 fill, 31 gradient, 32 figure, 33 frame, 34 ruler, 35 text, 36 balloon, 37 correct line
* Tools and groups have `NodeVariantID = 0`.
* **Drawing sub tools** use `NodeInputOp = NodeOutputOp`:
  * 10 brush (icons 103–130), 11 pen (101), 12 eraser (106), 13 copy stamp, 14 dot pen.
* A sub tool is a brush when its variant has a non-NULL `BrushSize`.
* `NodeInstalledState` **(inferred)**:
  * 2 = factory, unchanged
  * 1 = factory, modified
  * 0 = added by the user (also used in exported .sut files)
* In a .sut, `RootUuid` points directly to the single sub tool node.

### Versions

`Manager.Version` goes up with releases that add tool parameters.
`Node.NodeInstalledVersion` of factory sub tools uses the same counter to record the version that
introduced them.

* Verified: **145 = CSP 5.1.x**.
* From factory tools: 138 liquify, 139 mixing brushes, 141 new gradients, 142 circular text,
  143 rough pen, 144 lasso fill / magnetic lasso, 145 dynamic eraser.
* An exported sample with `Version 142` imports fine into 5.1.2.

There is no public table that maps these numbers to marketing versions. So brush-bridge compares a
file against the **installed** CSP at runtime: it copies the factory todb and reads its
`Manager.Version` and its Variant/Node column sets. A file is flagged when:

* its `Version` is higher than the installed one, or
* it has columns the installed CSP doesn't know.

The offered fix ("转换为旧版 CSP 可读取的格式") does three things:

1. Rebuilds `Variant` and `Node` without the unknown columns.
2. Clamps `Manager.Version` and `NodeInstalledVersion`.
3. Runs `VACUUM`.

**Not yet verified in a real CSP:** whether an older CSP actually rejects newer columns or only the
higher version number.

## Binary values

All binary values are big-endian.

### Effector blobs (`*Effector`, e.g. `BrushSizeEffector`)

The header is made of u32 words. Its size (word 0) depends on the CSP version:

* `0x28`: old files
* `0x2c`: CSP 1.x–4.x exports
* `0x30`: CSP 5 factory files

| word | meaning |
|---|---|
| 0 | header size |
| 1 | inputs offered by the UI (`0xf0`, `0x1f0`, …) |
| 2 | enabled inputs: `0x10` pen pressure, `0x20` tilt, `0x40` velocity, `0x80` random |
| 3–6 | minimum % for pressure, tilt, velocity, random |
| 7 | 0 |
| 8 | byte size of the pressure curve block after the header (0 = no curve) |
| 9 | byte size of a second curve block |
| 10 | (`≥0x2c`) 100 or 500, unknown |
| 11 | (`0x30`) 0 |

* **Curve block:** `u32 0x0c, u32 n, u32 0x10`, followed by `n` × (`f64 x`, `f64 y`).
* **Neutral model:** influence = `1 - minimum/100` for each enabled input.
* **Writing:** brush-bridge writes the `0x2c` layout with a linear 2-point curve, or the source
  pressure curve when it has one.
* **Colour change effectors** (`Brush{Hue,Saturation,Value,SubColor}ChangeEffector`): the column
  value is a signed amount (hue ±180, saturation/value ±100) and CSP applies
  `amount × input × r`, where `r` is uniformly random in `[random_min/100, 1]`. Factory brushes use
  `random_min = −100` for a two-sided jitter and e.g. 10 for a one-sided one. A negative amount with
  the effector disabled is a constant shift.
* **Pressure-driven colour:** Procreate shifts the colour at *light* pressure, so brush-bridge
  writes the pressure input with the inverted curve `1 − c(p)` (default `[(0,1),(1,0)]`). The
  range `[shift − down, shift + up]` becomes amount = the end with the larger magnitude and
  `random_min` = the other end relative to it.
* Only the per-tip (`BrushChangePatternColor`) columns have effectors; the per-stroke
  `BrushStroke*Change` columns are plain jitter ranges.

### `BrushInOutTarget` (BLOB, inferred)

`u32 0x0c, u32 count, u32 0x0c`, then `count` × (`u32 id`, `u32 enabled`, `u32 min %`). Seen ids
are `0x70`, `0x3e9` (probably size), `0x3fd` (probably opacity) and `0x411`. brush-bridge copies
this blob unchanged.

### `BrushRotationEffector` (INTEGER bit field, inferred)

* `0x01` is always set.
* `0x02` = follow line direction.
* `0x10` pressure, `0x20` tilt, `0x40` velocity, `0x80` random.
* With random set, `BrushRotationRandomScale` is the random range in %.

### Material references

Used by `BrushPatternImageArray`, `TextureImage`, `DualPatternImageArray` and `DualTextureImage`.

```
u32 8, u32 count
item: u32 item_size,
      u32 n + UTF-16LE path      ".:12:69:<uuid>:data:material_0.layer"   (user material)
                                  ".:Paint019:<uuid>:data:material_0.layer" (bundled material)
      u32 kind (2 user, 3 bundled)
      u32 n + UTF-16LE display name
      u32 1
      u32 n + UTF-16LE catalog path ".:12:69:<uuid>" / ".:Install:Paint019:<uuid>"
      u32 0
```

How a path resolves to a folder in the material library:

* **User materials** live at `Material\12\69\<uuid>\`.
* **Bundled materials** live at `Material\Install\Paint019\<uuid>\`.
* Material folders contain:
  * `catalog.xml`, `catalog.zip`, `info.xml`, `info.zip`
  * `data\material_0.layer`
  * `thumbnail\thumbnail.png`
  * `icedata\layerData.xml`
  * `*.dat`
* Bundled materials have **no `data` folder** in the library. Their payload ships inside the CSP
  installation.

## Embedded materials (`MaterialFile.FileData`)

`FileData` is a POSIX ustar archive. Every member has mode `0777` and uname `compress`. The members are:

```
catalog.zip, info.zip, data/material_0.layer, thumbnail/thumbnail.png, icedata/layerData.xml
```

* `OriginalPath` matches the path stored in the reference blob.
* `InstallFolder` is 1 for bundled materials.
* Packing a user-library folder with the same five members reproduces the archive CSP embeds:
  `catalog.zip`, `info.zip`, `material_0.layer` and `thumbnail.png` come out byte-identical.
  `layerData.xml` differs by 3 bytes.
* `*.zip` and `*.layer` are not zip files. They use a **C2F** container:
  * Magic: `\x89C2F\r\n\x1a\n`.
  * Chunks: u32 LE length, 4-byte tag, payload, CRC.
  * The `.layer` inside is a full `.clip`-style canvas database (about 56 `ParamScheme` tables,
    `Offscreen` block data, …).
  * The first chunk is **encrypted**.
* brush-bridge does **not** decrypt CSP's own materials, but it **can generate new ones**
  (`brush_bridge/formats/csp_material.py`). See "Generating materials" below. As a result:
  * **Reading:** materials written by brush-bridge (plain chunks) are decoded at full resolution.
    For CSP's own (encrypted) materials, tips and textures come from `thumbnail.png`. It is black ink
    on transparency and at most about 300 px. Tip mask = `alpha × (255 − luminance)`; textures are
    composited over white.
  * **Writing:** for brushes that came from CSP, the archives are copied verbatim (from the .sut, or
    packed from the user's library). Bundled materials without `data` are referenced only and are
    not embedded. Custom tips, textures and dual tips from other apps are written as newly generated
    materials embedded in the `.sut`. The adapter still exports an ABR v6.2 file as a fallback.

## Generating materials

These findings come from decoding every blob of four real materials (two brush tips and two paper
textures). The builder reproduces CSP's own `Offscreen` attribute, empty and pixel `BlockData`, and
`ResizableImageInfo` **byte-for-byte**. Generated materials have not yet been confirmed inside CSP.

* **C2F chunks**
  * Each chunk is u32 LE length, 4-byte tag, payload, then CRC32 LE over tag + payload.
  * `HEAD` and `TAIL` are empty.
  * A `dATA` payload starts with a 2-byte flag: `01 00` = encrypted (8 extra bytes), `00 00` = plain.
  * CSP writes the first 5120 database bytes encrypted and the rest plain.
  * brush-bridge writes all chunks plain, as either the same split or a single chunk.
  * `catalog.zip` / `info.zip` are C2F around a real zip holding `catalog.xml` / `info.xml`.
* **`material_0.layer`**
  * SQLite with a 4096-byte page size.
  * The schema plus `ParamScheme` / `ElemScheme` rows are shipped in `csp_material_template.json`.
  * `Canvas` holds W/H. `CanvasPreview` is a transparent RGBA PNG.
  * There are three `Offscreen` rows: a 5-channel empty, a 1-channel empty, and a 1-channel pixel layer.
* **Offscreen `Attribute`**
  * Sections are `Parameter` `[W, H, blocksX, blocksY, …]`, `InitColor`, and `BlockSize`.
  * Names are UTF-16BE; words are BE u32.
* **`BlockData`**
  * The image is split into 256×256 tiles.
  * Each tile is a `BlockDataBeginChunk` record: idx, raw size, 256, 256, an exists flag, then
    `u32 BE len+4` + `u32 LE len` + zlib data, closed by `BlockDataEndChunk`.
  * After the tiles come `BlockStatus` and `BlockCheckSum` (Adler-32 of the LE length + zlib bytes).
* **Pixels** are ink density, where 255 = paint.
  * CSP stores 255 − grey of an imported image.
  * Tips are written from the neutral mask directly. Textures are written as 255 − L.
* **Material type**
  * `layerData.xml` and `ResizableImageInfo` (u32 pair at offset 16) select the type.
  * (0, 0) = `BrushPattern` (画像素材:ブラシ).
  * (1, 3) = `PaperTexture` (単色パターン:テクスチャ).
* **References**
  * `OriginalPath` = `.:NN:NN:<uuid>:data:material_0.layer`, and the reference kind is 2.
  * CSP renumbers the `NN` folders on import.

## Parameter mapping (Variant → neutral `Brush`)

| CSP column(s) | Brush field | Notes |
|---|---|---|
| `BrushSize`, `BrushSizeUnit` | `size_px` | unit 0 px, 2 mm (factory "Rough pen" = 1.2 mm), 1 cm / 3 in / 4 pt **(inferred)**; physical units are converted at 350 dpi |
| `BrushSizeEffector` | `size_dynamic` | |
| `Opacity` / `BrushOpacityEffector` | `opacity` / `opacity_dynamic` | % |
| `BrushFlow` / `BrushFlowEffector` | `flow` / `flow_dynamic` | "brush density" |
| `BrushHardness` | `hardness` | |
| `BrushInterval`, `BrushAutoIntervalType` | `spacing` | type 0 = fixed %; 1/2/3 = automatic presets, approximated as 5/10/25 % |
| `BrushThickness`, `BrushVerticalThicknes` | `roundness`, `angle` (+90°) | |
| `BrushRotation`, `BrushRotationEffector`, `BrushRotationRandomScale` (+ `…InSpray` when spray is on) | `angle`, `angle_follow_stroke`, `angle_jitter` | |
| `BrushThicknessEffector` (random) | `roundness_jitter` | |
| `BrushUsePatternImage`, `BrushPatternImageArray` | `tips` | full resolution for generated materials, thumbnails otherwise |
| `BrushPatternOrderType` | `tip_order` | 3 = random **(inferred)** |
| `BrushPatternReverseHorizontal/Vertical` | `flip_x/flip_y` | |
| `BrushUseSpray`, `BrushSpraySize(+Unit, SyncBrushSize)`, `BrushSprayDensity(+Effector)` | `scatter`, `scatter_both_axes`, `count`, `count_jitter` | scatter = spray diameter ÷ brush size |
| `BrushUseIn/Out`, `BrushIn/OutLength(+Unit)`, `BrushInOutType` | `taper_start/end` | length px ÷ 100 (type 1 = %) |
| `TextureImage`, `TextureCompositeMode`, `TextureScale2`, `TextureDensity(+Effector)`, `TextureReverseDensity`, `TextureBrightness`, `TextureContrast`, `TextureForPlot` | `texture` | composite mode 0 = multiply, 1 = subtract **(inferred)** |
| `UseDualBrush`, `Dual*` | `dual` | |
| `BrushChangePatternColor` / `BrushChangeStrokeColor` + `Brush[Stroke]Hue/Saturation/Value/SubColor…` | `color` | hue range 0–180 → 0–1 |
| `BrushUseWaterColor`, `BrushMixColor`, `BrushMixAlpha`, `BrushMixColorExtension` | `wet` | load = paint amount, wetness = 1 − paint density |
| `CompositeMode` | `blend_mode` / `is_eraser` | see below |
| `AntiAlias` | `antialias` | 0–3 → bool |
| `Stickness`, `BrushUseRevision/BrushRevision` | `stabilization` | |
| `BrushUseWaterEdge`, `BrushWaterEdgeRadius(+Unit)`, `…AlphaPower`, `…ValuePower`, `…Blur(+Unit)`, `…AfterDrag` | `edge` | opacity = AlphaPower %, darken = ValuePower %; radius defaults to 5 % of the size (1–20 px) |
| `BrushHue/Saturation/ValueChange` + effector (pressure / tilt bit) | `color.*_pressure`, `color.*_tilt`, `color.pressure_curves` | pressure uses the inverted curve; only one input per channel |

`CompositeMode` follows the layer blend-mode list **(inferred)**:

| Values | Blend modes |
|---|---|
| 0–5 | 0 normal, 1 darken, 2 multiply, 3 colour burn, 4 linear burn, 5 subtract |
| 6–11 | 6 lighten, 7 screen, 8 colour dodge, 9 glow dodge, 10 add, 11 add (glow) |
| 12–17 | 12 overlay, 13 soft light, 14 hard light, 15 difference, 16 vivid light, 17 linear light |
| 18–23 | 18 pin light, 19 hard mix, 20 exclusion, 21 darker colour, 22 lighter colour, 23 divide |
| 24–26 | 24 hue, 25 saturation, 26 colour |
| 27 | transparent (every eraser uses it) |

Factory brushes use only 0, 2, 27 and 31. Value 31 is probably "compare density" and is read as normal.

**Lossless round trip:** `Brush.source.native["csp"]` keeps the following:

* the raw Manager, Node and Variant rows
* the column list and the material archives
* a fingerprint of the mapped values taken at read time

When writing, only the columns whose mapped value actually changed are overwritten. A .sut that is
read and written back unchanged therefore has an identical Variant row.

## Writing a .sut

brush-bridge writes one file per brush. Each file contains:

* Manager: `ToolType 0`, `Version 142` (or the source version), `RootUuid` = the node,
  `CurrentNodeUuid x'00'`, `MaxVariantID 2`, `CommonVariantID 44`, the constant
  `ObjectNodeUuid`, `SavedCount 1`.
* One Node: brush op 10 / icon 103, or eraser op 12 / icon 106. `VariantID 1`, `InitVariantID 2`.
* Two identical Variant rows using the 186-column brush column set. Defaults are the most common
  values among the factory brushes.
* The embedded materials and an empty UnicornData table.

**Import:** in CSP, select the target tool, then drag the file onto the **Sub Tool** palette. If you
drop it onto the *Tool* palette, CSP shows "此文件无法添加到工具组面板".

## Direct install (experimental, `install(..., direct=True)`)

This is only allowed while CSP is closed. The steps are:

1. `system.backup(EditImageTool.todb)`.
2. Find the Brush tool (`NodeDefaultIdentifier 22`) or the tool named in `tool=`.
3. Find or create a sub tool group `brush-bridge`, appended to the end of the tool's child list.
4. Append each sub tool node:
   * The node is copied from an existing brush/eraser sub tool, so it carries all 506+ columns.
   * The neutral values are applied on top.
   * Two new VariantIDs (`MaxVariantID+1/+2`) are allocated, and `Manager.MaxVariantID` is bumped.
5. Run `PRAGMA integrity_check`. If anything fails, the backup is restored.

Brushes that reference materials missing from the local library fall back to the .sut export, as do
brushes with custom tips. This path has been validated on copies only:

* integrity check passes
* the tree stays a single acyclic list with every node reachable
* the new sub tools re-read correctly

Whether CSP accepts the edited database still has to be confirmed by a user.
