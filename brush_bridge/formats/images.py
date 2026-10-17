"""Plain images (PNG/JPG/...) as brush tips, and PNG export of any brush's tip/texture."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image

from ..core.imaging import to_mask, trim
from ..core.model import Brush, BrushSource
from ..core.plugin import FileInfo, FormatCodec, register_codec
from ..core.system import safe_filename, unique_path


@register_codec
class ImagesCodec(FormatCodec):
    id = "images"
    name = "图片笔尖 (.png/.jpg)"
    extensions = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
    can_read = True
    can_write = True

    def sniff(self, path: Path) -> Optional[FileInfo]:
        path = Path(path)
        if path.suffix.lower() not in self.extensions:
            return None
        try:
            with Image.open(path) as im:
                size, mode = im.size, im.mode
        except Exception:
            return None
        info = FileInfo(path=path, format_id=self.id, format_name=self.name, version=mode, brush_count=1)
        info.details["尺寸"] = f"{size[0]}×{size[1]}"
        if max(size) > 5000:
            info.problems.append("图片超过 5000 像素，部分软件（如 Photoshop）不接受这么大的笔尖，转换时会自动缩小。")
        return info

    def read(self, path: Path) -> list[Brush]:
        path = Path(path)
        with Image.open(path) as im:
            im.load()
            tip = trim(to_mask(im))
        if max(tip.size) > 5000:
            tip.thumbnail((5000, 5000), Image.LANCZOS)
        b = Brush(name=path.stem, tips=[tip], size_px=float(max(tip.size)), spacing=0.15)
        b.source = BrushSource(app="", format=self.id, path=str(path))
        return [b]

    def write(self, brushes: list[Brush], out_path: Path, **options) -> list[Path]:
        """Each tip → black RGBA PNG (alpha = paint); textures → grayscale PNG."""
        out_path = Path(out_path)
        folder = out_path.parent if out_path.suffix else out_path
        folder.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        for b in brushes:
            tips = b.tips or [b.main_tip()]
            for i, tip in enumerate(tips):
                rgba = Image.new("RGBA", tip.size, (0, 0, 0, 0))
                rgba.putalpha(tip.convert("L"))
                suffix = f"_{i + 1}" if len(tips) > 1 else ""
                p = unique_path(folder / f"{safe_filename(b.name)}{suffix}.png")
                rgba.save(p)
                files.append(p)
            if b.texture is not None:
                p = unique_path(folder / f"{safe_filename(b.name)}_纹理.png")
                b.texture.image.convert("L").save(p)
                files.append(p)
        return files
