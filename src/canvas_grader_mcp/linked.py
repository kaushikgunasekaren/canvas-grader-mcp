"""Work submitted as a link instead of a file (public Google Docs/Slides/Sheets)."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

GOOGLE_DOC = re.compile(r"https://docs\.google\.com/(document|presentation|spreadsheets)/d/([A-Za-z0-9_-]{20,})")
EXPORT = {"document": "document/d/{id}/export?format=pdf", "presentation": "presentation/d/{id}/export/pdf",
          "spreadsheets": "spreadsheets/d/{id}/export?format=pdf"}
MAX_BYTES = 50 * 1024 * 1024


def google_links(text: str) -> list[tuple[str, str, str]]:
    """(kind, doc_id, url) for each distinct Google Docs/Slides/Sheets link in text."""
    seen, out = set(), []
    for m in GOOGLE_DOC.finditer(text or ""):
        if m.group(2) not in seen:
            seen.add(m.group(2))
            out.append((m.group(1), m.group(2), m.group(0)))
    return out


def fetch(kind: str, doc_id: str, dest: Path) -> tuple[Path | None, str | None]:
    """Download a public Google file as PDF. Returns (path, None) or (None, reason).

    Uses a fresh client with no credentials: only files shared as 'anyone with the link' work.
    """
    if dest.exists():
        return dest, None
    url = "https://docs.google.com/" + EXPORT[kind].format(id=doc_id)
    try:
        with httpx.Client(follow_redirects=True, timeout=60) as http:
            resp = http.get(url)
    except httpx.HTTPError as exc:
        return None, f"could not download: {exc}"
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("application/pdf"):
        return None, "not publicly shared (needs a Google sign-in); ask the student to share it or upload a file"
    if len(resp.content) > MAX_BYTES:
        return None, "too large to download"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return dest, None
