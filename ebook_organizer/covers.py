from __future__ import annotations

import hashlib
import io
import warnings
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

from .safeio import atomic_bytes


def make_cover(data: bytes, root: Path, book_id: str) -> tuple[str, str]:
    if len(data) > 20 * 1024**2:
        raise ValueError("封面文件超过20MB")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 50_000_000:
                raise ValueError("封面像素超过上限")
            picture = ImageOps.exif_transpose(original)
            if profile := original.info.get("icc_profile"):
                try:
                    picture = ImageCms.profileToProfile(picture, ImageCms.ImageCmsProfile(io.BytesIO(profile)), ImageCms.createProfile("sRGB"), outputMode="RGB")
                except Exception:
                    picture = picture.convert("RGB")
            picture = picture.convert("RGBA")
            picture.thumbnail((600, 900), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (600, 900), "white")
            canvas.paste(picture, ((600 - picture.width) // 2, (900 - picture.height) // 2), picture)
            output = io.BytesIO()
            canvas.save(output, "WEBP", quality=80, method=4)
            binary = output.getvalue()
    with Image.open(io.BytesIO(binary)) as verify:
        verify.load()
    version = hashlib.sha256(binary).hexdigest()
    path = root / "covers" / book_id / version / "cover.webp"
    if not path.exists():
        atomic_bytes(path, binary)
        thumbnail = canvas.resize((240, 360), Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        thumbnail.save(stream, "WEBP", quality=75)
        atomic_bytes(path.with_name("thumb.webp"), stream.getvalue())
    raw_path = root / "originals" / book_id / (hashlib.sha256(data).hexdigest() + ".image")
    if not raw_path.exists():
        atomic_bytes(raw_path, data)
    return str(path.relative_to(root).as_posix()), version

