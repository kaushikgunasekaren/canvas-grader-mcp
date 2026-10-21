"""Read public web pages students submit for grading (LinkedIn posts, portfolios, blogs...).

Pages are fetched without any credentials, so only publicly visible content is
seen. Local and private-network addresses are refused so a submitted link can't
make the grader read services on the grader's own machine or network.
"""

from __future__ import annotations

import html as htmllib
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

from .extract import html_to_text, truncate

MAX_BYTES = 5 * 1024 * 1024
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


class PageError(RuntimeError):
    pass


def _check_public(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PageError(f"Only http(s) links can be opened: {url!r}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise PageError(f"Can't resolve {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise PageError(f"Refusing to open a local/private address ({parsed.hostname}).")


def _meta(page: str, prop: str) -> str | None:
    m = re.search(rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+content="([^"]*)"', page) or \
        re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(prop)}"', page)
    return htmllib.unescape(m.group(1)) if m else None


def fetch(url: str, http: httpx.Client | None = None) -> tuple[str, str, int]:
    """(final_url, html, status). Follows redirects, re-checking every hop."""
    own = http is None
    http = http or httpx.Client(timeout=30, headers={"User-Agent": UA})
    try:
        for _ in range(8):
            _check_public(url)
            with http.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers["location"])
                    continue
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise PageError("Page is larger than 5 MB.")
                return str(resp.url), body.decode(resp.encoding or "utf-8", errors="replace"), resp.status_code
        raise PageError("Too many redirects.")
    finally:
        if own:
            http.close()


def _linkedin(page: str) -> dict:
    """Post text, photos and video from a public LinkedIn post page (text, photo and video posts differ)."""
    info: dict = {}
    m = re.search(r'<p[^>]*class="attributed-text-segment-list__content[^"]*"[^>]*>(.*?)</p>', page, re.S)
    body = re.search(r'"articleBody":"((?:[^"\\]|\\.)*)"', page)
    if m:
        text = re.sub(r"<br\s*/?>", "\n", m.group(1))
        info["text"] = htmllib.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    elif body:
        info["text"] = json.loads(f'"{body.group(1)}"')
    elif desc := _meta(page, "og:description"):
        info["text"] = desc
    author = re.search(r'"author":\{[^}]*"name":"([^"]+)"', page)
    title = _meta(page, "og:title") or ""
    info["author"] = author.group(1) if author else (title.split("|")[-1].strip() if "|" in title else None)
    if date := re.search(r'"datePublished":"([^"]+)"', page):
        info["published"] = date.group(1)
    unescaped = page.replace('\\"', '"').replace("\\u0026", "&")
    images = []
    for rendition in re.finditer(r'"(https://media\.licdn\.com/dms/image/v2/[A-Za-z0-9_-]+/feedshare-)","imageRenditions":\[(.*?)\]', unescaped):
        sizes = re.findall(r'"width":(\d+),"height":\d+,"suffixUrl":"([^"]+)"', rendition.group(2))
        if sizes:
            width, suffix = max(sizes, key=lambda s: int(s[0]) if int(s[0]) <= 1280 else 0)
            images.append(rendition.group(1) + suffix)
    images += [htmllib.unescape(u) for u in re.findall(r'data-delayed-url="(https://media\.licdn\.com/dms/image/[^"]*feedshare[^"]*)"', page)]
    seen, unique = set(), []
    for u in images:
        key = u.split("/feedshare")[0]
        if key not in seen:
            seen.add(key)
            unique.append(u)
    info["images"] = unique
    info["has_video"] = _meta(page, "og:type") == "video.other" or bool(re.search(r"<video\b|video-js|data-sources=", page))
    return info


def read(url: str, http: httpx.Client | None = None) -> dict:
    """Summary of a public page: title, author/date where known, main text, image URLs and whether it has video."""
    final, page, status = fetch(url, http)
    result: dict = {"url": url, "final_url": final, "status": status}
    host = urlparse(final).hostname or ""
    if "linkedin.com/login" in final or "authwall" in final:
        result["note"] = "LinkedIn wants a sign-in for this post: it isn't public. Ask the student to set the post to public."
        return result
    if status == 999 and host.endswith("linkedin.com"):
        result["note"] = "LinkedIn temporarily refused automated access (HTTP 999); try again in a few minutes or open it in a browser."
        return result
    if status >= 400:
        result["note"] = f"The page returned HTTP {status}; it may be private, deleted or need a sign-in."
        return result
    result["title"] = _meta(page, "og:title") or (re.search(r"<title[^>]*>(.*?)</title>", page, re.S) or [None, None])[1]
    result["type"] = _meta(page, "og:type")
    if host.endswith("linkedin.com"):
        result["site"] = "linkedin"
        result.update(_linkedin(page))
    else:
        main = re.search(r"<(article|main)\b.*?</\1>", page, re.S | re.I)
        result["text"] = html_to_text(main.group(0) if main else page)
        imgs = [_meta(page, "og:image")] + re.findall(r'<img[^>]+src="(https?://[^"]+)"', main.group(0) if main else page)
        result["images"] = list(dict.fromkeys(i for i in imgs if i))[:20]
        result["has_video"] = bool(re.search(r"<video\b|og:video|youtube\.com/embed|player\.vimeo\.com", page))
    result["text"] = truncate(result.get("text") or "", 20000)
    result["hashtags"] = sorted(set(re.findall(r"#\w+", result["text"])))
    if not result["text"]:
        result["note"] = "No readable text found (the page may be built by JavaScript or need a sign-in)."
    return result


def download_images(urls: list[str], limit: int, http: httpx.Client | None = None) -> list[tuple[str, bytes]]:
    own = http is None
    http = http or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": UA})
    out = []
    try:
        for url in urls[:limit]:
            try:
                _check_public(url)
                resp = http.get(url)
            except (PageError, httpx.HTTPError):
                continue
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/") and len(resp.content) <= 15 * 1024 * 1024:
                out.append((url, resp.content))
    finally:
        if own:
            http.close()
    return out
