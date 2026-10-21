"""File-type sniffing, video frames, and what a submitted link actually points to."""

from __future__ import annotations

import re
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

KNOWN_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".zip", ".sb3", ".ipynb", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".heic", ".tif", ".tiff", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mp3", ".m4a", ".wav",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def sniff(path: Path) -> str | None:
    """Guess a file's real extension from its first bytes (for uploads with no or wrong extension)."""
    try:
        head = path.read_bytes()[:64]
    except OSError:
        return None
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand.startswith((b"heic", b"heix", b"mif1")):
            return ".heic"
        return ".mov" if brand == b"qt  " else ".mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".mkv"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = zipfile.ZipFile(path).namelist()
        except zipfile.BadZipFile:
            return None
        if "project.json" in names:
            return ".sb3"
        for prefix, ext in (("word/", ".docx"), ("ppt/", ".pptx"), ("xl/", ".xlsx")):
            if any(n.startswith(prefix) for n in names):
                return ext
        return ".zip"
    return None


def effective_suffix(path: Path) -> str:
    """The file's extension, or its sniffed type when the extension is missing or unknown (e.g. 'Lab 1.6')."""
    ext = path.suffix.lower()
    if ext in KNOWN_EXTENSIONS:
        return ext
    return (sniff(path) if path.exists() else None) or ext


# --- video frames -------------------------------------------------------------


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        import shutil

        return shutil.which("ffmpeg")


def video_duration(path: Path) -> float | None:
    exe = ffmpeg_exe()
    if not exe:
        return None
    proc = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=60)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", proc.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None


def video_frames(path: Path, count: int, max_side: int = 1400) -> list[tuple[str, "object"]]:
    """(label, PIL image) for `count` frames spread evenly through a video, e.g. a recording of the student's code."""
    import io

    from PIL import Image

    exe, duration = ffmpeg_exe(), video_duration(path)
    if not exe or not duration:
        return []
    out = []
    for i in range(count):
        t = duration * (i + 0.5) / count
        proc = subprocess.run(
            [exe, "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
             "-vf", f"scale='min({max_side},iw)':-2", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=120,
        )
        if proc.returncode == 0 and proc.stdout:
            out.append((f"{path.name} at {t:.1f}s of {duration:.0f}s", Image.open(io.BytesIO(proc.stdout))))
    return out


# --- links ---------------------------------------------------------------------

URL = re.compile(r"https?://[^\s<>\"')\]]+")
SHORTENERS = {"photos.app.goo.gl", "goo.gl", "bit.ly", "tinyurl.com", "canvaqr.com", "www.canvaqr.com",
              "qr.generatorqr.com", "forms.gle"}


def find_urls(*texts: str) -> list[str]:
    seen: list[str] = []
    for text in texts:
        for url in URL.findall(text or ""):
            url = url.rstrip(".,;:")
            if url not in seen:
                seen.append(url)
    return seen


def _page_kind(html: str) -> tuple[str | None, str | None]:
    title = re.search(r'og:title" content="([^"]*)"', html)
    for mime, kind in (("video/", "video"), ("image/", "photo"), ("application/pdf", "document")):
        if f'"{mime}' in html:
            return kind, title.group(1) if title else None
    return None, title.group(1) if title else None


def classify_link(url: str, http: httpx.Client | None = None) -> dict:
    """What a link points to: video, photo, document, webpage, not_public or unknown.

    Only well-known hosts are fetched (Google Drive/Photos, QR and URL shorteners), with no credentials.
    """
    own = http is None
    http = http or httpx.Client(follow_redirects=True, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    try:
        host = urlparse(url).hostname or ""
        if host in SHORTENERS:
            resp = http.get(url)
            final = str(resp.url)
            if urlparse(final).hostname in SHORTENERS:
                return {"url": url, "kind": "unknown"}
            result = classify_link(final, http) if final != url else {"url": url, "kind": "unknown"}
            return {**result, "url": url, "resolves_to": final}
        if host.endswith(("youtube.com", "youtu.be")):
            return {"url": url, "kind": "video", "title": "YouTube"}
        if host == "docs.google.com" and "/videos/" in url:
            return {"url": url, "kind": "video", "title": "Google Vids"}
        if host == "docs.google.com":
            return {"url": url, "kind": "document"}
        if host in ("drive.google.com", "photos.google.com"):
            resp = http.get(url)
            if resp.status_code != 200 or (urlparse(str(resp.url)).hostname or "").startswith("accounts."):
                return {"url": url, "kind": "not_public"}
            kind, title = _page_kind(resp.text)
            if host == "photos.google.com" and kind is None:
                kind = "photo"
            return {"url": url, "kind": kind or "unknown", "title": title}
        return {"url": url, "kind": "webpage"}
    except httpx.HTTPError as exc:
        return {"url": url, "kind": "unknown", "error": str(exc)[:120]}
    finally:
        if own:
            http.close()
