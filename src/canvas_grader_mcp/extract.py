"""Turn assignment descriptions and submitted files into plain text for grading."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

MAX_CHARS = 60_000

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".py", ".java", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
    ".rb", ".php", ".swift", ".kt", ".kts", ".scala", ".m", ".r", ".sql", ".sh", ".bash", ".ps1",
    ".tex", ".bib", ".css", ".scss", ".asm", ".s", ".v", ".sv", ".vhd", ".hs", ".ml", ".lua", ".pl", ".dart",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class _HTMLToText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")
            elif tag.startswith("h") and len(tag) == 2:
                self.parts.append("#" * int(tag[1]) + " ")
        elif tag == "a":
            self._href = dict(attrs).get("href")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            self.parts.append(f"[image{': ' + alt if alt else ''}]")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "a" and self._href:
            self.parts.append(f" ({self._href})")
            self._href = None

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    parser = _HTMLToText()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"^([ \t]*-)[ \t]*\n+[ \t]*", r"\1 ", text, flags=re.M)  # <li><p>..</p></li>
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def truncate(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated: {len(text) - limit} more characters not shown ...]"


def extract_file(path: Path) -> dict:
    """Return {'kind', 'text'?, 'note'?} for a downloaded submission file."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(str(path))
            pages, image_pages = [], []
            for i, page in enumerate(pdf):
                text = page.get_textpage().get_text_range().replace("\r\n", "\n").strip()
                images = count_pdf_images(page)
                marker = f" [{images} image(s) - see get_submission_images]" if images else ""
                if images:
                    image_pages.append(i + 1)
                pages.append(f"--- page {i + 1}{marker} ---\n{text}")
            text = "\n\n".join(pages)
            has_text = any(p.split("---\n", 1)[-1].strip() for p in pages)
            note = None if has_text else "No extractable text (scanned PDF?); use get_submission_images."
            return {"kind": "pdf", "pages": len(pdf), "pages_with_images": image_pages, "text": truncate(text), "note": note}
        if ext == ".docx":
            import docx

            doc = docx.Document(str(path))
            images = sum(1 for rel in doc.part.rels.values() if "image" in rel.reltype)
            lines = [p.text for p in doc.paragraphs]
            for table in doc.tables:
                for row in table.rows:
                    lines.append(" | ".join(cell.text for cell in row.cells))
            note = f"Contains {images} embedded image(s); see get_submission_images." if images else None
            return {"kind": "docx", "images": images, "text": truncate("\n".join(lines)), "note": note}
        if ext == ".ipynb":
            nb = json.loads(path.read_text(errors="replace"))
            cells = []
            for i, cell in enumerate(nb.get("cells", [])):
                src = "".join(cell.get("source", []))
                cells.append(f"--- cell {i + 1} ({cell.get('cell_type')}) ---\n{src}")
                for out in cell.get("outputs", []):
                    out_text = "".join(out.get("text", [])) or "".join(out.get("data", {}).get("text/plain", []))
                    if out_text:
                        cells.append(f"[output]\n{out_text}")
            return {"kind": "notebook", "text": truncate("\n\n".join(cells))}
        if ext in (".html", ".htm"):
            return {"kind": "html", "text": truncate(html_to_text(path.read_text(errors="replace")))}
        if ext in TEXT_EXTENSIONS or _looks_like_text(path):
            return {"kind": "text", "text": truncate(path.read_text(errors="replace"))}
        if ext in IMAGE_EXTENSIONS:
            return {"kind": "image", "note": "Image file; see get_submission_images."}
    except Exception as exc:  # a corrupt upload should not break grading the rest
        return {"kind": "error", "note": f"Could not read {path.name}: {exc}"}
    return {"kind": "binary", "note": f"Unsupported file type {ext or '(none)'}; review it in SpeedGrader."}


def count_pdf_images(page) -> int:
    import pypdfium2 as pdfium

    return sum(1 for obj in page.get_objects(max_depth=2) if obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE)


def _looks_like_text(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
