# Third-party notices

## Procreate support (`brush_bridge/formats/procreate.py`, `brush_bridge/apps/procreate.py`)

No third-party code was copied into the Procreate codec or app adapter. The
NSKeyedArchiver reader/writer, the `SilicaBrush` key template and the parameter
mapping were written from scratch. They are based on our own analysis of public
sample brushes and on format knowledge from these projects:

| Project | License | Used for |
|---|---|---|
| [aumlette-lab/procreate-brush-decoder](https://github.com/aumlette-lab/procreate-brush-decoder) | MIT | Key names ↔ Brush Studio settings, value ranges (knowledge only) |
| [Leon-Schoenbrunn/CSP2PC](https://github.com/Leon-Schoenbrunn/CSP2PC) | MIT | Confirmed that a plistlib-written archive is accepted by Procreate; seed brush layout (knowledge only) |
| [barakbl/procreate_brush_claude_tools](https://github.com/barakbl/procreate_brush_claude_tools) | MIT | Minimal archive / zip layout notes (knowledge only) |
| [javadhamidi/brush-converters](https://github.com/javadhamidi/brush-converters) | MIT | Format anatomy notes (knowledge only) |
| MorrowShore/ProcreateBrushExtract, tohsakarat/Brush-Converter (CC BY-NC), prospect and other GPL/AGPL/unlicensed projects | non-permissive | Read for format knowledge only. No code was copied. |

### Test samples (downloaded at test time, never committed)

`scripts/fetch_samples.py` (procreate section) downloads the public `.brush` files
below into `tests/fixtures/downloaded/procreate/`, which is git-ignored. They are
used only by `tests/test_procreate.py`. Each download is pinned to a commit.

| File | Source | License / terms |
|---|---|---|
| Monopixel.brush | github.com/vtf6259/procreate-brushes | 0BSD |
| CSP2PC_Seed.brush | github.com/Leon-Schoenbrunn/CSP2PC | MIT |
| barakbl_marker.brush | github.com/barakbl/procreate_brush_claude_tools | MIT |
| Hard_Oval_Brush, Lineart_v3, Sketchy_Liner, Painterly_Brush_v2 | github.com/jixuqianxing/CustomProcreateBrushes | free to use (per README) |
| Canvas, Snake_Cross | github.com/aptcraft/FreeProcreateBrushes | free brushes (per README) |
| SuperBrush.brush | github.com/finngreiter/procreate-brushes | public repository, no explicit license |
| Wiggle_Ink.brush | github.com/worrydoll/Procreate_Brushes | public repository, no explicit license |

Procreate is a trademark of Savage Interactive Pty Ltd. brush-bridge is not
affiliated with Savage Interactive. Procreate's built-in shapes and grains are not
distributed. Brushes that reference them use images the user places in their own
`procreate_library` folder; otherwise they fall back to procedurally generated look-alikes
(`brush_bridge/formats/procreate_builtin.py`) or a computed round tip.

## Clip Studio Paint support (`brush_bridge/formats/csp*.py`, `brush_bridge/apps/csp.py`)

No third-party code was copied. The `.sut` / `EditImageTool.todb` schema, the effector and
material-reference blobs, and the embedded-material archive layout were all reverse-engineered
from our own exports and from a local CSP 5.1.2 install, using read-only copies. The Variant
column defaults in `csp_schema.py` are plain parameter values computed from those copies;
no CELSYS file is redistributed. The projects below were read for format knowledge only:

| Project | License | Used for |
|---|---|---|
| [javadhamidi/brush-converters](https://github.com/javadhamidi/brush-converters) | MIT | Overall .sut table layout (knowledge only; its writer does not embed materials) |
| [Leon-Schoenbrunn/CSP2PC](https://github.com/Leon-Schoenbrunn/CSP2PC) | MIT | Variant column meanings and MaterialFile handling (knowledge only) |
| [Loperamide/CSPExtract](https://github.com/Loperamide/CSPExtract) | see repository | Material extraction notes (knowledge only) |
| KennethEvans' CSP tools, tohsakarat/Brush-Converter (CC BY-NC) | unlicensed / non-commercial | Read for format knowledge only. No code was copied. |

Clip Studio Paint is a trademark of CELSYS, Inc. brush-bridge is not affiliated with CELSYS.
CSP's bundled materials are not distributed. Exported `.sut` files only reference them by
UUID, so they resolve against the materials already installed with the user's own copy of CSP.

## PaintTool SAI Ver.2 support (`brush_bridge/formats/sai2.py`, `brush_bridge/apps/sai2.py`)

No third-party code is used. We worked out the `.saitdat`/`.saitgrp`/`.saitset` text format, the
shape INI keys and the folder layout ourselves, from SAI2's own files on a local install: the
`init` folders, `history.txt` and the strings in `sai2.exe`. Public SAI2 brush-installation
tutorials helped us find the folder conventions. The INI comment style written by the codec follows
the layout of the files that ship with SAI2; the comment text is our own Chinese wording. No SAI2
asset (shapes, textures, default tools) is distributed with brush-bridge, and the tests generate
their own sample files.

PaintTool SAI is a trademark of SYSTEMAX Software Development. brush-bridge is not affiliated with
SYSTEMAX.

## Krita and GIMP support (`brush_bridge/formats/krita.py`, `brush_bridge/formats/gimp.py`, `brush_bridge/apps/krita.py`, `brush_bridge/apps/gimp.py`)

No third-party code was copied. We wrote the `.kpp`/`.bundle` and `.gbr`/`.gih`/`.vbr`/`.pat`
readers and writers ourselves from the file layouts described below. We confirmed those layouts by
reading the official GPL sources and real files. No source was copied or translated line by line.

| Project | License | Used for |
|---|---|---|
| [KDE/krita](https://invent.kde.org/graphics/krita) (GitHub mirror `KDE/krita`) | GPL-3.0-or-later | Read for format knowledge only: preset XML, sensor/curve option keys, `KoResourceBundle` manifest/meta, `KisResourceLocator` storage scanning, PNG/GBR brush-tip conventions and auto-brush fade semantics. No code was copied. |
| [GNOME/gimp](https://gitlab.gnome.org/GNOME/gimp) (`app/core/gimpbrush-load.c`, `gimpbrushpipe-load.c`, `gimpbrushgenerated-load.c` …) | GPL-3.0-or-later | Read for format knowledge only: GBR v1/v2/CinePaint v3 headers, GIH pipe header, VBR 1.0/1.5 text format. No code was copied. |
| [Pawel-9215/abr-to-krita](https://github.com/Pawel-9215/abr-to-krita) | MIT | Ideas for mapping ABR settings to Krita presets (knowledge only). We do not follow its `fade = 1 − hardness` mapping, because Krita's mask generator treats fade ≈ hardness. |

### Test samples (downloaded at test time, never committed)

The krita/gimp section of `scripts/fetch_samples.py` downloads these files into
`tests/fixtures/downloaded/krita/` and `tests/fixtures/downloaded/gimp/`, which are git-ignored. Each
download is pinned to a commit. The files are used only as local test inputs by
`tests/test_krita_gimp.py`.

| Files | Source | License |
|---|---|---|
| `Krita_4_Default_Resources.bundle`, `RGBA_brushes.bundle`, three `paintoppresets/*.kpp`, unit-test presets/bundle | github.com/KDE/krita | GPL-3.0-or-later |
| `Hardness-075.vbr`, `Star.vbr`, `pixel.gbr`, `pepper.gbr`, `Bristles-01.gbr`, `Smoke.gbr`, `confetti.gih`, `Pencil-03.gih` | github.com/GNOME/gimp-data | GPL-3.0-or-later (GIMP data) |

Krita is a trademark of the KDE e.V. / Krita Foundation. GIMP is developed by the GIMP team. brush-bridge
is affiliated with neither. No Krita or GIMP resources are distributed with brush-bridge.
