# GIMP brushes (.gbr / .gih / .vbr)

These notes come from the GIMP sources (GNOME/gimp: `app/core/gimpbrush-header.h`,
`gimpbrush-load.c`, `gimpbrushpipe-load.c`, `gimpbrushgenerated-load.c`,
`libgimpbase/gimpparasiteio.c`) and from the stock brushes in GNOME/gimp-data (commit `dba1b6d3`,
which is where GIMP 3 keeps its data files). Our writer re-encodes the real `Smoke.gbr` byte for byte.

Code: `brush_bridge/formats/gimp.py` (codec plus the GBR/GIH/PAT helpers the Krita codec also uses) and
`brush_bridge/apps/gimp.py` (adapter).

## GBR (sampled brush)

All integers are big-endian u32.

| Offset | v1 | v2 |
|---|---|---|
| 0 | `header_size` (20 + name length) | `header_size` (28 + name length) |
| 4 | `version` = 1 | `version` = 2 |
| 8 | `width` | `width` |
| 12 | `height` | `height` |
| 16 | `bytes` (1 or 4) | `bytes` (1 or 4) |
| 20 | name | `magic` = `GIMP` |
| 24 | pixels | `spacing` (percent of the brush size) |
| 28 | | name (UTF-8, NUL-terminated) then pixels |

* **Pixels:**
  * `bytes=1` is an 8-bit gray mask with **255 = full paint** and 0 = nothing. Our model uses the same
    convention, so nothing is inverted.
  * `bytes=4` is an RGBA "pixmap" (color brush). The mask is its alpha.
* **Versions:**
  * v1 has no magic and no spacing. GIMP assumes 25 % spacing, and the name runs to `header_size`.
  * v3 is CinePaint: like v2 but `bytes = 18`, with 16-bit float gray stored as the upper half of an
    IEEE float32. GIMP reads it; Krita does not. Sniffing reports it, and the fix
    "转换为标准 GBR v2" re-encodes it to 8-bit v2.
* GIMP limits brushes to 10000 px per side. Names are read as UTF-8, max 256 bytes in our writer.
* We always write **v2** with 8-bit gray (`bytes=1`). GIMP 2.x, GIMP 3.x, Krita, MyPaint and others
  read it.

## GIH (image pipe, "animated" brush)

```
<name>\n
<ncells> ncells:<n> cellwidth:<w> cellheight:<h> step:100 dim:1 cols:1 rows:1 placement:constant rank0:<n> sel0:random\n
<GBR cell 1><GBR cell 2>…
```

* `sel0` is `incremental`, `angular`, `random`, `velocity`, `pressure`, `xtilt` or `ytilt`.
* A pipe can have several dimensions (`dim`, `rank0..`, `sel0..`). We merge them into a flat tip list
  and warn.
* The selection becomes `Brush.tip_order`: `random` and the sensor-driven modes map to random,
  everything else to sequential.
* The writer emits `dim:1` with `sel0` set to `random` or `incremental`, and the brush spacing in
  every cell.

## VBR (generated brush, text)

```
GIMP-VBR            GIMP-VBR
1.0                 1.5
<name>              <name>
<spacing %>         circle|square|diamond
<radius px>         <spacing %>
<hardness 0..1>     <radius px>
<aspect ≥1>         <spikes 2..20>
<angle deg>         <hardness 0..1>
                    <aspect ≥1>
                    <angle deg>
```

* Reading:
  * A circle with 2 spikes becomes a computed round tip: size = 2·radius, roundness = 1/aspect,
    hardness and angle carried over.
  * Other shapes are rasterized and warned.
* Writing: on request (`prefer_vbr` or a `.vbr` target), a tip-less brush is written as VBR 1.5.
* VBR 1.5 needs GIMP ≥ 2.4, and `compatibility()` says so. Krita can't import `.vbr`; there is a
  "转换为 GBR" fix.

## Version notes

* **GIMP 2.10 vs 3.0:**
  * Same brush formats.
  * GIMP 3 moved data files to the `gimp-data` repository.
  * GIMP 3 uses its own user folder: `%APPDATA%\GIMP\3.0` instead of `%APPDATA%\GIMP\2.10`. It may
    offer to migrate the 2.10 profile on first start.
* Both read Photoshop `.abr` natively from the brushes folder, so the adapter also accepts `.abr`.

## Adapter (`apps/gimp.py`)

* `detect()`:
  * Looks for uninstall registry entries matching `^GIMP\b` or `GNU Image Manipulation Program`.
  * Fallbacks: `Program Files\GIMP 3`, `GIMP 2`, `GIMP` and `%LOCALAPPDATA%\Programs\GIMP*`.
  * The exe is `bin\gimp-X.Y.exe`; `gimp-console` is skipped.
  * The version (series `major.minor`) comes from the file version, the registry or the exe name.
  * Returns `[]` if nothing is found.
* User folder: `%APPDATA%\GIMP\<series>`, overridable by `GIMP2_DIRECTORY`/`GIMP3_DIRECTORY` like GIMP
  itself. `data_dir` is `<user folder>\brushes`.
* `list_brushes()`: `.gbr/.gih/.vbr` files (the name comes from the header). `.abr` files are listed
  through the ABR codec.
* `install()`:
  * Writes `.gbr` (one tip) or `.gih` (several tips) into the export folder, then copies them into
    the brushes folder with unique names.
  * GIMP shows new files after **Brushes ▸ Refresh brushes** or a restart, and the message says which
    one applies.
* `install_files()` accepts `.gbr/.gih/.vbr/.abr`.

## Field mapping

| brush-bridge `Brush` | GIMP | Notes |
|---|---|---|
| `name` | GBR name / GIH first line / VBR name | |
| `tips[0]` | GBR gray pixels (255 = paint) | resampled to `size_px` by default (`resample=False` keeps pixels) |
| `tips` (>1), `tip_order` | GIH cells, `sel0` | |
| `size_px` | tip pixel size; VBR `2·radius` | GIMP's default brush size is the tip size |
| `spacing` | `spacing` percent / 100 | |
| `hardness`, `roundness`, `angle` (computed tip) | VBR hardness, `1/aspect`, angle | GBR bakes the shape into pixels |
| `roundness`, `angle` (bitmap tip) | — | warned: set them in GIMP's tool options |
| dynamics, texture, scatter, dual brush, color dynamics | — | GIMP keeps these in tool presets and paint dynamics, not in the brush; warned |

## Limitations

* Color brushes (`bytes=4`, e.g. `pepper.gbr`) are read as a mask only; their color is dropped with
  a warning.
* Multi-dimensional pipes are flattened. `angular` selection becomes sequential.
* GIMP tool presets (`.gtp`) and paint dynamics (`.gdyn`) are not read or written, so pressure
  settings don't travel to or from GIMP.
* Not validated in a running GIMP (GIMP isn't installed on the dev machine). The byte-level
  re-encoding of real GIMP brushes is covered by tests.
