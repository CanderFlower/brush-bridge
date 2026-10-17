# Krita resource bundles (.bundle) and paintop presets (.kpp)

These notes come from the Krita source (KDE/krita, commit `eaed83da`, mainly `libs/resources`,
`libs/image/brushengine/kis_paintop_preset.cpp`, `plugins/paintops/libpaintop`), from the stock
presets and bundles shipped with Krita 4 and 5, and from loading our own output in a real
Krita 5.3.4.1 (see *Validation*). Statements marked **(inferred)** were not checked in the Krita UI.

Code: `brush_bridge/formats/krita.py` (codec), `brush_bridge/apps/krita.py` (adapter).
The GBR/GIH/PAT helpers it uses live in `brush_bridge/formats/gimp.py`.

## Files

| File | What |
|---|---|
| `*.kpp` | one paintop preset: a PNG thumbnail with the settings in PNG text chunks |
| `*.bundle` | zip with presets plus the brush tips and patterns they use |
| `brushes/*.png` | grayscale PNG tip (`png_brush`) |
| `brushes/*.gbr`, `*.gih` | GIMP tips (`gbr_brush`), see [gimp.md](gimp.md) |
| `brushes/*.abr` | Photoshop brush sets (`abr_brush`); Krita 5 also loads loose `.abr` as a storage |
| `brushes/*.svg` | vector tips (`svg_brush`) |
| `patterns/*.pat`, `*.png` | textures |

### .kpp

A PNG (the preview, usually 200×200 RGBA). It has two `tEXt`/`zTXt`/`iTXt` chunks:

* `version`
  * `2.2`: the Krita 2.x–4 format. Krita 5 still loads it.
  * `5.0`: Krita 5 only. The XML can embed the resources the preset uses.
* `preset`: the XML.

```xml
<Preset paintopid="paintbrush" name="b)_Basic-5_Size" [embedded_resources="2"]>
  <resources>                                   <!-- 5.0 only -->
    <resource type="brushes" name=".." filename="x.png" md5sum="..">BASE64</resource>
  </resources>
  <param name="brush_definition" type="string"><![CDATA[<Brush type="png_brush" .../>]]></param>
  <param name="PressureSize" type="string"><![CDATA[true]]></param>
  ...
</Preset>
```

* Param types:
  * `string`: the value is in CDATA.
  * `internal`: plain text.
  * `bytearray`: base64 of the stored bytes. The Krita 4 texture params `Texture/Pattern/Pattern`
    and `PatternMD5` store a base64 *string* as a bytearray, so they end up base64-encoded twice.
* **Display name** (`kis_paintop_preset.cpp`):
  * Krita 5 takes the `name` attribute (line 325).
  * `KisPaintOpPreset::name()` shows `_` as a space (line 133).
  * Example: the file `b)_Basic-5_Size_default.kpp` with `name="b)_Basic-5_Size"` is listed as
    **b) Basic-5 Size**. The resource database stores this same string.

### `brush_definition`

```xml
<Brush type="auto_brush" BrushVersion="2" angle="0" spacing="0.1" useAutoSpacing="0" autoSpacingCoeff="1"
       density="1" randomness="0">
  <MaskGenerator id="default" type="circle" diameter="40" ratio="1" hfade="0.5" vfade="0.5" spikes="2"
                 antialiasEdges="1"/>
</Brush>
<Brush type="png_brush|gbr_brush|abr_brush|svg_brush" filename="x.png" md5sum="…" BrushVersion="2"
       scale="0.5" angle="0" spacing="0.1" useAutoSpacing="0" autoSpacingCoeff="1" brushApplication="0"
       ColorAsMask="1" AdjustmentMidPoint="127" BrightnessAdjustment="0" ContrastAdjustment="0"
       AutoAdjustMidPoint="0" AdjustmentVersion="2"/>
```

* `angle` is in radians.
* For predefined tips, `scale` is the size divided by the native image size.
* `MaskGenerator` `id` is `default`, `soft` (curve fade) or `gauss`. `type` is `circle` or `rect`.
* The fades `hfade`/`vfade` (0..1) behave like Photoshop hardness: 1 = hard.
* `type="gbr_brush"` is also used for `.gih` pipes. For pipes, the selection mode is part of the
  `.gih` header.
* **PNG tip convention:**
  * A grayscale PNG with no alpha is a mask where **black = paint** and white = background.
  * An RGBA PNG uses alpha. With `brushApplication` 0 it acts as a mask; otherwise it is a color image.
  * Our model uses 255 = paint, so the writer inverts the image.
  * Our GBR/GIH tips are 255 = paint, the same as GIMP.

### Curve options (dynamics)

Each curve option `<X>` is written as these params (`Size`, `Opacity`, `Flow`, `Rotation`, `Ratio`,
`Scatter`, `Spacing`, `Softness`, `Sharpness`, `Mix`, `Darken`, `h`, `s`, `v`, `Texture/Strength/`, …):

| Param | Meaning |
|---|---|
| `Pressure<X>` | option checkbox ("Pressure" is a historical name) |
| `<X>UseCurve` | "Enable pen settings": sensors apply |
| `<X>Sensor` | `<params id="sensorslist"><ChildSensor id="pressure"><curve>0,0;1,1;</curve></ChildSensor>…</params>` |
| `<X>UseSameCurve`, `<X>commonCurve` | one curve for all sensors |
| `<X>Value` | strength / base value (Opacity, Flow: the slider value) |
| `<X>curveMode` | combine sensors: 0 multiply, 1 add, 2 max, 3 min, 4 difference |

* Sensor ids: `pressure`, `pressurein`, `xtilt`, `ytilt`, `tiltdirection`, `tiltelevation`, `speed`,
  `drawingangle`, `rotation`, `distance`, `time`, `fade`, `fuzzy` (per dab), `fuzzystroke`,
  `perspective`, `tangentialpressure`.
* Unknown ids make Krita drop the sensor.
* A curve is `x,y;x,y;` in 0..1.

### .bundle

* Zip contents:
  * `mimetype`: `application/x-krita-resourcebundle`. Stored (not deflated) and first, like ODF.
  * `META-INF/manifest.xml`: namespace `urn:oasis:names:tc:opendocument:xmlns:manifest:1.0`. Has one
    `file-entry` per resource with `media-type` (the resource type folder), `full-path` and `md5sum`,
    plus optional `<manifest:tags>`.
  * `meta.xml`: `meta:meta` with `meta:generator` (e.g. `Krita (5.2.2)`), `dc:title`, `dc:author`,
    `meta:creation-date`, `meta:bundle-version`, `meta:meta-userdefined` (email/license/website/tag).
  * `preview.png`.
  * Resource folders.
* **Krita 4 vs 5:**
  * Krita 5 bundles can contain `seexpr_scripts`, `fonts`, `css_styles` and `.myb` MyPaint presets,
    which Krita 4 cannot read.
  * They can also contain kpp `5.0` presets.
  * They may contain only *changed* resource versions (`name.0001.kpp`).
  * brush-bridge reports the minimum version as `最低版本` in `FileInfo.details`. `compatibility()`
    warns when a Krita 5 bundle goes to Krita 4. There is a "转换为 Krita 4 资源包" fix that re-packs
    the file in the format below.

## Where Krita keeps resources (Windows)

* Resource folder:
  * Default `%APPDATA%\krita`.
  * It can be overridden by `ResourceDirectory=` in the root group of `%LOCALAPPDATA%\kritarc`
    (`KoResourcePaths::getAppDataLocation`).
  * `kritarunner.exe` uses its own `kritarunnerrc` and `%APPDATA%\kritarunner`.
* **Krita 5:**
  * `KisResourceLocator::findStorages` scans the resource folder **recursively** for `*.bundle`,
    `*.abr` and `*.asl`.
  * A new file becomes an active storage at the next start.
  * State lives in `resourcecache.sqlite` (tables `storages`, `resources`, `resource_types`,
    `storage_types`, `versioned_resources`, …).
  * Loose `.kpp` files in `paintoppresets\` belong to the "folder" storage.
  * Krita reads both kinds at startup.
* **Krita 4:** bundles go in `<resdir>\bundles`, and `.abr` files in `brushes\`.

## Adapter (`apps/krita.py`)

* `detect()`:
  * Looks for uninstall registry entries matching `^Krita\b`, then falls back to `Program Files\Krita (x64)`
    and `Krita`.
  * The exe is `bin\krita.exe`. The version comes from the file version, else the registry.
  * `extra` contains `resource_dir`, `bundles_dir`, `major` and `kritarc`.
  * Returns `[]` when Krita isn't installed or the OS isn't Windows.
* `list_brushes()`:
  * Copies `resourcecache.sqlite` into memory with the sqlite backup API (read-only, no lock held
    on Krita's file).
  * Lists active presets from active storages.
  * Bundle presets are resolved through the codec. Folder presets resolve to `paintoppresets\<file>`.
  * Without a database it scans files instead (`*.bundle` recursively, plus `paintoppresets\*.kpp`).
* `install()`:
  * Writes one `.bundle` to the export folder, then copies it into `<resdir>\bundles`.
  * The message tells the user to restart Krita if it's running. Otherwise the bundle loads at next start.
* `install_files()` routes files by type:

  | File type | Destination |
  |---|---|
  | `.bundle` | `bundles\` |
  | `.abr` | `bundles\` (Krita 5) or `brushes\` (Krita 4) |
  | `.kpp` | `paintoppresets\` |
  | `.gbr`/`.gih`/`.png`/`.svg` | `brushes\` |
  | `.pat` | `patterns\` |

* Writing into the folder while Krita runs is safe; Krita only picks the files up at next start.

## What brush-bridge writes

* **Bundle** (default):
  * `mimetype` first and stored, the manifest with md5s, meta.xml with the generator
    `Krita (5.2.0) via brush-bridge`, and preview.png.
  * `paintoppresets/<prefix><slug>.kpp` (kpp **2.2**, so Krita 4 and 5 both load it).
  * `brushes/` tips:
    * one tip: grayscale PNG with black = paint, plus `brush_spacing`/`brush_name` text chunks;
    * several tips: GIH.
  * `patterns/*.png` textures.
  * File names are ASCII slugs, so zip and old-Krita path handling never matters. Display names keep
    Unicode in the XML `name`. Tips are capped at 2500 px and textures at 2048 px.
* **Standalone .kpp:**
  * Version `5.0` when it needs a tip or texture, with the resources embedded.
  * Otherwise `2.2`, which is self-contained with an `auto_brush`.
* Presets carry the full option set copied from Krita's own defaults (`_base_params`). Compared with
  the stock preset `b)_Basic-5_Size`, no param key is missing.

## Field mapping

| brush-bridge `Brush` | Krita preset | Notes |
|---|---|---|
| `name` | `Preset@name` | `_` is shown as a space when reading |
| `tips` (1) | `png_brush` tip, `brush_definition@filename` + `requiredBrushFile` | inverted gray PNG |
| `tips` (>1), `tip_order` | `gbr_brush` + `.gih`, `sel0` random/incremental | |
| no tips: `hardness`, `roundness`, `angle` | `auto_brush` circle: `hfade`/`vfade`, `ratio`, `angle` (rad) | read: `soft`/`gauss` fades are approximated |
| `size_px` | auto: `diameter`; predefined: `scale` × native size | |
| `spacing` | `brush_definition@spacing` (fraction) | auto spacing → `coeff·√size / size` |
| `angle` (bitmap tips) | `brush_definition@angle` | |
| `roundness` (bitmap tips) | — | warned; use Krita's Ratio option |
| `opacity`, `opacity_dynamic` | `OpacityValue`, `PressureOpacity` + `OpacitySensor` | |
| `flow`, `flow_dynamic` | `FlowValue`, `FlowSensor` | |
| `size_dynamic` | `PressureSize`, `SizeUseCurve`, `SizeSensor` | |
| `Dynamic.pressure` = p | sensor `pressure`, curve `0,1-p;1,1;` | read: span of the curve |
| `Dynamic.tilt` | sensor `declination` (read: any tilt sensor) | |
| `Dynamic.velocity` | sensor `speed`, curve `0,1;1,1-v;` | |
| `Dynamic.jitter` | sensor `fuzzy` (read: `fuzzy`/`fuzzystroke`) | |
| `angle_follow_stroke` | `Rotation` + sensor `drawingangle` | |
| `angle_jitter` | `Rotation` + `fuzzy` (`RotationValue` = amount) | |
| `roundness_jitter` | `Ratio` + `fuzzy` curve | |
| `scatter`, `scatter_both_axes` | `PressureScatter`, `ScatterValue` (max 5), `Scattering/AxisX/Y` | |
| `blend_mode` | `CompositeOp` (`dodge`, `burn`, `diff`, `luminize`, …) | |
| `is_eraser` | `CompositeOp=erase` (read: also `EraserMode`) | |
| `color` HSV jitter | `h`/`s`/`v` options + `fuzzy` | `ColorSource/Type` random → hue jitter |
| `texture.image`, `name` | `patterns/*.png`; `Texture/Pattern/PatternFileName`, `PatternMD5Sum`, `Name` (K5) + `Pattern`, `PatternMD5` bytearrays (K4) | |
| `texture.scale`, `brightness`, `contrast`, `invert` | `Texture/Pattern/Scale`, `Brightness`, `Contrast` (1 + c), `Invert` | |
| `texture.mode` | `Texture/Pattern/TexturingMode` 0–13 | multiply, subtract, lightness, gradient, darken, overlay, color_dodge, color_burn, add, linear_burn, hard_mix, hard_mix_softer, height, linear_height |
| `texture.depth`, `depth_dynamic` | `Texture/Strength/Value` + sensors | |
| `dual` (dual brush) | masking brush: `MaskingBrush/Enabled`, `MaskingCompositeOp`, `MasterSizeCoeff`, `Preset/brush_definition` | |
| `wet` | — (read: `colorsmudge` engine → wet mix/load, approximated) | warned on write |
| `stabilization`, tapers, `count`, `flip_*`, fg/bg jitter | — | warned |

Non-`paintbrush` engines (smudge, bristle, spray, MyPaint…) are read as an approximate pixel brush
with a warning. For a Krita→Krita round trip, unmanaged `paintbrush` params are kept in
`source.native` and written back.

## Validation

* Unit tests: `tests/test_krita_gimp.py`. They check structure against the real stock presets and
  bundles fetched by `scripts/fetch_samples.py`, pinned to KDE/krita `eaed83da`.
* Runtime check, done once by hand with the portable **Krita 5.3.4.1** (`krita-x64-5.3.4.1.zip` from
  download.kde.org):
  * `kritarunner` pointed at a scratch resource folder holding:
    * a synthetic bundle (bitmap tip + texture + dual brush, auto tip, 3-tip GIH, Chinese name);
    * all 117 stock Krita 4 presets re-written by brush-bridge;
    * the 6 RGBA_brushes presets re-written (textures);
    * a standalone kpp 5.0 with embedded resources.
  * Result: **all 127 presets** were registered. Their tips and patterns were present in
    `Krita.instance().resources()` (74 brushes, 26 patterns). `Preset.toXML()` returned our XML, and
    nothing about our files appeared in the log or stderr.
  * Running `list_brushes()` on the `resourcecache.sqlite` that Krita produced gave 266 presets, with
    names identical to the database.

## Limitations

* Brush tip roundness, tip flip and mirror options are not stored per preset.
* Tilt and rotation-driven rotation are ignored when reading.
* `soft`/`gauss` mask generators and their curves become a plain hardness.
* Colored RGBA tips lose color.
* Krita 4 compatibility of our 2.2 presets is **(inferred)** from the Krita 4 sources and stock
  files; it was not tested in a real Krita 4.
* Bundles installed with the adapter appear after a Krita restart. There is no live reload.
