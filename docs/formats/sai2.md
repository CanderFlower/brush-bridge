# PaintTool SAI Ver.2: brush files

Modules: `brush_bridge/formats/sai2.py` (codec `sai2`) and `brush_bridge/apps/sai2.py` (app `sai2`).
Tests: `tests/test_sai2.py`.

Everything below comes from our own inspection of SAI2 builds 2021-02-28 (the Saier Chinese
localisation) and 2021-05-28 (official), plus their `history.txt`, the `init` folders, the strings
inside `sai2.exe`, and a real user settings folder. No official specification exists.

## 1. Where SAI2 keeps brushes

| Build | Settings folder |
|---|---|
| ≥ 2020-01-07 | `%USERPROFILE%\Documents\SYSTEMAX Software Development\SAIv2\settings` (shared by every SAI2 install of the user) |
| < 2020-01-07 | folders next to `sai2.exe` (`brshape`, `blotmap`, `bristle`, `scatter`, `brushtex`, `papertex`); there are no custom tool files |
| portable packs | some packs ship a `settings` folder next to `sai2.exe`; the adapter uses it if it contains at least two of the folders below |

The install folder holds the factory defaults in `init\` (same layout as `settings`). SAI2 copies
them on first start. We never write into the install folder of a ≥ 2020-01-07 build.

Settings layout:

```
settings\
  sai2.ini                     UI settings (not touched)
  settings2.ssd                binary UI/state blob (not touched)
  brushfom\blotmap\*.bmp       "渗化" brush forms   (256x256 grey noise)
  brushfom\bristle\*.bmp       "鬃毛" brush forms   (63x63, black dot per hair on white)
  brushfom\brshape\*.bmp[+ini] "笔刷形状"           (grey, white = paint)
  scatter\*.bmp[+ini]          "散布形状"           (grey, white = paint)
  brushtex\*.bmp               brush textures      (512x512 grey, tileable)
  papertex\*.bmp               paper textures      (512x512 grey, tileable)
  custool\nrm\                 custom tools of normal layers   ("普通")
  custool\ink\                 custom tools of linework layers ("墨水")
  custool\shp\                 custom tools of shape layers    ("形状")
  custool\*\bak\               previous versions of changed files (written by SAI2)
```

A drop-down list in SAI2 shows the file stem of each BMP. SAI2 scans the folders only at startup.

### BMP assets

- 8 bpp indexed with an identity grey palette (1078-byte header offset). That is exactly what Pillow
  writes for an `L` image saved as BMP. Every shipped asset uses it. The codec flags other bit depths
  and offers a "convert to 8-bit grey" fix.
- Scatter shapes and brush shapes: **white (255) = paint**, black = transparent. This matches the
  model's tip polarity, so no inversion is needed. The shipped shapes come in many sizes, up to about
  1000 px. The writer limits the longest side to 1024.
- Textures (`brushtex`, `papertex`): 512x512 grey, white = paper, tileable. The writer centre-crops
  to a square, tiles small textures (and records the scale change in `texscl`), and cross-fades the
  seam if the wrap-around edge is visibly discontinuous.
- Bristle BMPs mark each hair with a black dot. On reading they become a dot-pattern tip
  (an approximation).

### Shape INI files (`scatter\*.ini`, `brushfom\brshape\*.ini`)

These are Windows INI files with a single `[Parameters]` section, tab-indented `Key = Value` lines
and long `;` comment blocks. Encoding: the shipped files are Shift-JIS (Japanese builds) or GBK
(Chinese localisation); files written by SAI users are often UTF-8. `decode_text()` tries UTF-8
(with or without a BOM) first, then picks cp932 or GBK by the number of kana and CJK characters.
The writer emits UTF-8 without a BOM, with LF line endings, in SAI2's comment style.

| Key | Range / default | Model field |
|---|---|---|
| `Opacity` (brshape only) | 0..100, 100 | `flow` |
| `AngleControl` | 0 none, 1 auto (follow stroke), 2 pen barrel; default 1 | `angle_follow_stroke` (2 → warning) |
| `Angle` | -180..+180, 0 | `angle` |
| `AngleJitter` | 0..100 % | `angle_jitter` (fraction of 360°) |
| `Scaling` | 0..200 %, 100 | multiplies the tool size |
| `ShapeSize` (scatter) | 1..5000 px, 100 | `size_px` of a stand-alone scatter shape |
| `SizeJitter` | 0..100 % | `size_dynamic.jitter` |
| `Spacing` | scatter 0..1000 %, brshape 0..50 %; default 25 | `spacing` |
| `Scattering` | scatter 0..1000 %, brshape 0..50 % | `scatter` |
| `WxHRatio` | `100:1` .. `100:100` .. `1:100` | `roundness` = min/max |
| `WxHJitter` | 0..100 % | `roundness_jitter` |
| `Count`, `CountJitter` | 1..20, 0..100 % | `count`, `count_jitter` |
| `AllDirScattering` | 0 perpendicular to the stroke, 1 all directions | `scatter_both_axes` |
| `GaussianDistribution`, `AbsoluteSize`, `IntegerPosition`, `HighQualitySampling` | 0/1 | kept natively |
| `ColJitter`, `HueJitter`, `SaturationJitter`, `BrightnessJitter` | 0..100 % | `color.*_jitter` |
| `ApplyToEachShape` | 0 per stroke, 1 per shape | `color.per_tip` |

A loose BMP is claimed by the codec only if it has a sibling INI with these keys, or if it lives
in a SAI2 asset folder inside a settings-like folder (at least two of the folders above). Every
other BMP is left to the generic image codec. An INI with `Opacity` and none of the scatter-only keys
is a brush shape; anything else is a scatter shape.

## 2. Custom tools: `.saitdat`, `.saitgrp`, `.saitset`, `.saitlnk`

All four are UTF-8 text with LF line endings. Each line is `key=T:value`; plain index lines are
`n=m`. A line containing only `.` ends a section, and the file ends with `--EOF--` plus a newline.

The type letter `T` is one of:
- `S` ASCII string
- `U` UTF-8 string
- `I` signed 32-bit integer

`parse_sait`/`dump_sait` round-trip the user's files byte for byte; the tests check this.

### `custool\<cat>\N.saitdat` (one tool; N = tool id)

Section 0 is the header:

| Key | Meaning |
|---|---|
| `tidstr` | tool type: `pencil airbrush brush water marker eraser selpen selers blur effpen scatter smudge binpen dotpen biners bselpen bselers` (normal), `inkpen inkers inkchg inkcurv inkline inkprs binkpen …` (linework) |
| `xidstr` | empty |
| `vkc` | shortcut key (virtual key code) |
| `tabflt`, `bzintf` | UI flags |
| `tfmode` | 0 = one parameter set; 2 = the tool has a second parameter set (section 2) |
| `name` (U) | display name |
| `addt` (U) | memo. The writer stores `brush-bridge 转换自 <app>` here |

Section 1 holds the parameters. Section 2 holds the second parameter set (empty unless
`tfmode=2`). Section 3 is always empty. Key order differs per tool type; the writer copies the
order SAI2 2021 uses (`tool_template()`). Every parameter section starts with
`modstr=I:1836216174`.

| Key | Meaning | Model mapping |
|---|---|---|
| `cursize` | size in 0.1 px | `size_px = cursize/10` |
| `curszscl` | size slider scale 0 (×0.1) / 1 (×1) / 10 (×10); UI only | written from the size |
| `minsize` | minimum size % | `min_size_px`; `size_dynamic.pressure = 1 - minsize/100` if `szprsf` |
| `curdens`, `mindens` | density % / minimum density % | `opacity`; `opacity_dynamic.pressure = 1 - mindens/100` if `dnprsf` |
| `wavno` | edge-hardness preset 0..4 (airbrush 0, pencil 4) | `hardness = wavno/4` |
| `hardness` | "sharpness of hardest edge" 0..100 | native only |
| `fomcat`, `fomnam` | brush form: 0 none, 1 blotmap, 2 bristle, 3 brshape, 4 scatter (the scatter tool); name = BMP stem | tips / texture |
| `blot*` | blotmap amount, scale, invert | blotmap → texture (approximation) |
| `brsl*` | bristle parameters | native only |
| `scatden` | shape density | `flow` |
| `scatanc scatang scatanj scatscl scatszj scatspc scatsct scathqsf` | same as the INI keys `AngleControl Angle AngleJitter Scaling SizeJitter Spacing Scattering HighQualitySampling` | see the INI table |
| `scatsiz`, `scatszscl`, `scatabsf` | scatter tool: absolute shape size (assumed 0.1 px) | `size_px` if `scatabsf` |
| `scatwth`, `scatwhj` | W:H ratio, ±100 (sign unverified), jitter | `roundness = 1 - abs/100` |
| `scatcnt scatcnj scataldf scatgauf scatintf scatcolj scathuej scatsatj scatbrij scataesf` | same as the INI keys `Count CountJitter AllDirScattering …` | see the INI table |
| `texcat`, `texnam` | texture on/off (written as 2), name in `brushtex` (falls back to `papertex`) | `texture` |
| `texval`, `texscl`, `texinvf` | strength %, scale %, invert | `texture.depth`, `.scale`, `.invert` |
| `blend`, `dilution`, `persist` | colour mixing, water content, colour persistence (brush/water/marker) | `wet.mix`, `wet.wetness` (`load = 1 - dilution`), `wet.color_stretch` |
| `szprsf`, `dnprsf`, `blprsf`, `dlprsf` | pressure → size / density / blend / dilution enabled | dynamics |
| `szsens`, `dnsens`, `blsens` | pressure sensitivity | native only |
| `sbpos_patlist previewbg showf_*` | UI state | written as defaults |

Tool types and conversion:
- `eraser`, `biners`, `inkers`, `binkers` → `is_eraser`.
- `bin*` and `dotpen` → `antialias=False`.
- Selection tools and the linework edit tools (`selpen selers bselpen bselers inkchg binkchg inkprs`) are skipped.
- Blur, smudge and linework tools convert with a warning.

### `custool\<cat>\_N.saitgrp` (one tool group = one tab in the tool panel)

Section 0:
- `vsbpos` (scroll position)
- `name=U:` (tab name)
- `csrbid0..2` = `0x90000000 | selected tool id` (signed), or 0

Section 1: `slot=toolid` lines. Slots may have gaps, which show as empty cells.

### `custool\<cat>\_0.saitset` (tab order)

Section 0: `curgid` (current group), `tabno`, `newgid`. Section 1: `index=groupid` in tab order.

`newgid` is *not* simply "next id": a real file had `newgid=3` with 4 groups. The writer leaves it
alone and picks `max(existing group ids) + 1` for a new group.

`.saitlnk` files (links between the tool slots of different layer types) are not used by the
converter.

`custool\<cat>\bak\` holds the previous version of every file SAI2 changed. New tool ids are
chosen above the highest id in the main folder and in `bak` so that no id is reused.

## 3. Version timeline (from `history.txt`)

`history.txt` is in the install folder. It is UTF-8 with a BOM, newest entry first, with headers
like `2020-01-07b版`. The adapter takes the newest date as the build. If there is no
`history.txt`, it uses the PE link timestamp of `sai2.exe` (which matched the history date for
both installs examined). The registry `DisplayVersion` is only `2.0` for official builds.

| Build | Feature (what the codec checks) |
|---|---|
| 2016-01-19 | bristle brush-form parameters |
| 2018-09-17 | scatter shapes (`scatter` folder + INI) |
| 2019-05-18 | scatter tool, effect pen, brush shapes (`brshape`) |
| 2019-07-28 | brush-shape angle / angle jitter |
| 2020-01-07 | custom tool files (`custool`) and the `Documents\SYSTEMAX Software Development\SAIv2\settings` folder |

`FileInfo.details["min_build"]` holds the minimum build a file needs, and
`Sai2Codec.compatibility()` names the missing features for older builds.

## 4. Writing and installing

`Sai2Codec.write()` creates a package folder (never overwrites; a unique name is chosen) with
`scatter\`, `brushfom\brshape\`, `brushtex\`, `custool\nrm\N.saitdat` and `安装说明.txt` (manual
instructions).

How each model brush is written (`choose_mode`):
- **scatter**: `count > 1`, count jitter, scatter > 50 %, spacing > 50 %, or colour jitter. The tip
  goes to `scatter\<name>.bmp`/`.ini`, and the tool is the scatter tool.
- **brshape**: any other brush with a tip. The tip goes to `brushfom\brshape`, and the tool is a
  brush (spacing and scatter are clamped to 50 %).
- **round**: no tip. The tool is a pencil (hardness ≥ 0.9, size ≤ 30), an airbrush (hardness ≤ 0.3)
  or a brush. A wet brush becomes the brush or watercolour tool; an eraser becomes the eraser tool.
- **texture**: the texture goes to `brushtex\<name>.bmp` with `texcat=2`.
- Brushes that were read from SAI2 are written back with their original sections and BMP/INI
  bytes (lossless). Only the name and the asset references change.

`Sai2Adapter.install()` writes into the settings folder:
1. Assets go into their folders. A name collision gets `name (2)`; an identical existing file is
   reused. Files are opened with `xb`, so they are never overwritten. This step is allowed while
   SAI2 runs; SAI2 picks the files up on its next start.
2. Custom tools are written only while **no** `sai2.exe` is running, because SAI2 rewrites custool
   on exit. Otherwise the result has `needs_app_closed=True` and the assets are still installed.
   New tools get new ids in `custool\nrm`. They are added to the group 「导入笔刷」 (option
   `group_name`), which is created as `_N.saitgrp` and appended to `_0.saitset`. A tool already
   registered with identical bytes is not added again.
3. Every modified file (`_0.saitset`, or an existing group file) is first copied with
   `system.backup()` to `custool\nrm\brush-bridge-backups\`. If registration fails, the new tool
   files are deleted again.
4. For a legacy (< 2020-01-07) layout, only assets are installed (`brshape` etc. next to the exe).

## 5. Limitations

- Bristle and blotmap brush forms are approximated (dot tip / texture). `hardness`, the `brsl*`
  bristle settings, the pressure sensitivity curves and the second parameter set (`tfmode=2`) are
  kept only for SAI2 → SAI2 round trips.
- `texcat=1` (seen in some default tools) has an unknown meaning. It is read like 2.
- The sign of `scatwth` (width vs. height) and the unit of `scatsiz` are unverified.
- SAI2 has a limit on the number of custom tools ("Number of custom tools is reached to limit");
  the exact number is unknown.
- `settings2.ssd` and `sai2.ini` are not read or written. Shape-layer tools (`custool\shp`) are
  not converted.
- `system.backup()` puts its copies in `custool\nrm\brush-bridge-backups\`. SAI2 already keeps a
  `bak\` sub-folder there, but we have not checked in the GUI that SAI2 ignores the extra folder.
- SAI2 has no blend modes, dual brushes, tilt or velocity dynamics. These are dropped with a
  warning.
