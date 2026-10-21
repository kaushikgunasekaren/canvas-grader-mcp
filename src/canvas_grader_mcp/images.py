"""Visual content of submissions (screenshots, photos, scanned pages) as images for the model."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from PIL import Image as PILImage

from .extract import IMAGE_EXTENSIONS, count_pdf_images, unzip
from .media import VIDEO_EXTENSIONS, effective_suffix, video_frames

MAX_SIDE = 1400
MIN_SIDE = 48  # skip icons, bullets and tracking pixels


def _encode(img: PILImage.Image, max_side: int = MAX_SIDE) -> bytes:
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _open(data: bytes) -> PILImage.Image | None:
    try:
        img = PILImage.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None  # EMF/WMF and other formats Pillow can't draw
    return img if min(img.size) >= MIN_SIDE else None


def file_images(path: Path, max_side: int = MAX_SIDE) -> list[tuple[str, PILImage.Image]]:
    """(label, image) pairs for one submitted file; PDF pages are rendered with max_side as the long edge."""
    ext = effective_suffix(path)
    out: list[tuple[str, PILImage.Image]] = []
    if ext == ".pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        for i, page in enumerate(pdf):
            text = page.get_textpage().get_text_range().strip()
            if count_pdf_images(page) or not text:  # screenshots/photos, or a scanned page
                scale = max_side / max(page.get_width(), page.get_height())
                out.append((f"{path.name} page {i + 1}", page.render(scale=scale).to_pil()))
    elif ext in (".docx", ".pptx", ".xlsx"):
        with zipfile.ZipFile(path) as zf:
            for name in sorted(n for n in zf.namelist() if "/media/" in n):
                if img := _open(zf.read(name)):
                    out.append((f"{path.name} {name.rsplit('/', 1)[-1]}", img))
    elif ext == ".zip":
        for member in unzip(path):
            if member.exists():
                out.extend(file_images(member, max_side))
    elif ext in IMAGE_EXTENSIONS or ext in (".heic", ".tif", ".tiff"):
        if img := _open(path.read_bytes()):
            out.append((path.name, img))
    return out


def collect(paths: list[Path], max_images: int, max_side: int = MAX_SIDE, frames_per_video: int = 0) -> tuple[list[tuple[str, bytes]], int]:
    """Encoded, de-duplicated images across files, capped at max_images. Returns (images, total_found)."""
    seen: set[str] = set()
    found: list[tuple[str, bytes]] = []
    for path in paths:
        is_video = effective_suffix(path) in VIDEO_EXTENSIONS
        if is_video and not frames_per_video:
            continue
        pictures = video_frames(path, frames_per_video, max_side) if is_video else file_images(path, max_side)
        for label, img in pictures:
            data = _encode(img, max_side)
            digest = hashlib.sha1(data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            found.append((label, data))
    return found[:max_images], len(found)


def qr_codes(paths: list[Path]) -> list[dict]:
    """Text of QR codes/barcodes in submitted images and PDF pages (students often link videos this way)."""
    import zxingcpp

    found: list[dict] = []
    for path in paths:
        for label, img in file_images(path, max_side=2400):
            for code in zxingcpp.read_barcodes(img.convert("RGB")):
                if code.text and not any(f["text"] == code.text for f in found):
                    found.append({"source": label, "text": code.text})
    return found
