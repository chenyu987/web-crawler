"""Parse HTML: pull out text, metadata, links and every downloadable asset."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from bs4 import BeautifulSoup, NavigableString, Tag

from .utils import (
    category_from_extension,
    is_streaming_url,
    looks_like_page,
    normalize_url,
)

CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+?)\1\s*\)""", re.I)
CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\()?\s*['"]([^'"]+)['"]""", re.I)
# Raw media URLs that sit inside inline <script> blocks / JSON (players, galleries)
SCRIPT_MEDIA_RE = re.compile(
    r"""["'](https?:)?(//[^"'\s<>]+?\.(?:mp4|webm|m3u8|mpd|mov|mp3|m4a|pdf|jpe?g|png|webp|gif)(?:\?[^"'\s<>]*)?)["']""",
    re.I,
)


@dataclass
class AssetRef:
    url: str
    category: Optional[str]  # guessed from extension / tag; may be refined later
    tag: str                 # where it was found (img, video, a, css, meta ...)


@dataclass
class PageData:
    url: str
    title: str = ""
    description: str = ""
    language: str = ""
    meta: Dict[str, str] = field(default_factory=dict)
    headings: List[Dict[str, str]] = field(default_factory=list)
    text: str = ""            # plain text
    markdown: str = ""        # readable markdown version
    links: List[Dict[str, str]] = field(default_factory=list)   # {url, text}
    page_links: List[str] = field(default_factory=list)          # to crawl
    assets: List[AssetRef] = field(default_factory=list)
    embeds: List[str] = field(default_factory=list)              # youtube etc.
    json_ld: List[object] = field(default_factory=list)
    base_url: str = ""


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - lxml missing
        return BeautifulSoup(html, "html.parser")


def parse_srcset(value: str) -> List[str]:
    out = []
    for part in (value or "").split(","):
        part = part.strip()
        if part:
            out.append(part.split()[0])
    return out


def css_urls(css: str) -> List[str]:
    urls = [m.group(2).strip() for m in CSS_URL_RE.finditer(css or "")]
    urls += [m.group(1).strip() for m in CSS_IMPORT_RE.finditer(css or "")]
    return [u for u in urls if not u.startswith(("data:", "#"))]


# Tag/attribute pairs that reference resources
_RESOURCE_ATTRS = [
    ("img", "src", "images"),
    ("img", "data-src", "images"),
    ("img", "data-original", "images"),
    ("img", "data-lazy-src", "images"),
    ("image", "href", "images"),
    ("image", "xlink:href", "images"),
    ("input", "src", "images"),
    ("video", "src", "videos"),
    ("video", "poster", "images"),
    ("video", "data-src", "videos"),
    ("audio", "src", "audio"),
    ("track", "src", "documents"),
    ("embed", "src", None),
    ("object", "data", None),
    ("script", "src", "scripts"),
]


def extract(html: str, url: str) -> PageData:
    soup = make_soup(html)
    data = PageData(url=url)

    base = url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = normalize_url(base_tag["href"], url) or url
    data.base_url = base

    def norm(u: Optional[str]) -> Optional[str]:
        return normalize_url(u, base) if u else None

    seen_assets = set()

    def add_asset(u: Optional[str], category: Optional[str], tag: str) -> None:
        nu = norm(u)
        if not nu:
            return
        if is_streaming_url(nu) and category in (None, "videos", "audio"):
            if nu not in data.embeds:
                data.embeds.append(nu)
            return
        cat = category_from_extension(nu) or category
        key = (nu, cat)
        if key in seen_assets:
            return
        seen_assets.add(key)
        data.assets.append(AssetRef(nu, cat, tag))

    # ---- metadata ----------------------------------------------------------
    if soup.title and soup.title.string:
        data.title = soup.title.string.strip()
    html_tag = soup.find("html")
    if isinstance(html_tag, Tag):
        data.language = (html_tag.get("lang") or "").strip()
    for m in soup.find_all("meta"):
        key = m.get("name") or m.get("property") or m.get("itemprop")
        val = m.get("content")
        if key and val:
            key = key.strip().lower()
            data.meta[key] = val.strip()
            if key in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image"):
                add_asset(val, "images", "meta")
            elif key in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
                add_asset(val, "videos", "meta")
            elif key in ("og:audio", "og:audio:url"):
                add_asset(val, "audio", "meta")
    data.description = data.meta.get("description") or data.meta.get("og:description", "")
    if not data.title:
        data.title = data.meta.get("og:title", "")

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data.json_ld.append(json.loads(s.string or ""))
        except (ValueError, TypeError):
            pass

    # ---- <link> tags (css, icons, preload) --------------------------------
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        href = link["href"]
        if "stylesheet" in rel:
            add_asset(href, "styles", "link")
        elif "icon" in rel or "apple-touch" in rel:
            add_asset(href, "images", "link")
        elif "preload" in rel or "prefetch" in rel:
            as_ = (link.get("as") or "").lower()
            cat = {"font": "fonts", "image": "images", "style": "styles",
                   "script": "scripts", "video": "videos", "audio": "audio"}.get(as_)
            add_asset(href, cat, "link")
        elif "alternate" in rel and "rss" in (link.get("type") or ""):
            add_asset(href, "documents", "link")

    # ---- tag resources ----------------------------------------------------
    for tag_name, attr, cat in _RESOURCE_ATTRS:
        for t in soup.find_all(tag_name):
            v = t.get(attr)
            if v:
                add_asset(v, cat, tag_name)
    for t in soup.find_all(["img", "source"]):
        for attr in ("srcset", "data-srcset"):
            for u in parse_srcset(t.get(attr, "")):
                add_asset(u, "images", t.name)
    for t in soup.find_all("source"):
        parent = t.find_parent(["video", "audio", "picture"])
        cat = {"video": "videos", "audio": "audio", "picture": "images"}.get(
            parent.name if parent else "", None)
        src = t.get("src") or t.get("data-src")
        if src:
            add_asset(src, cat, "source")
    for t in soup.find_all("iframe"):
        src = t.get("src") or t.get("data-src")
        nu = norm(src)
        if nu and is_streaming_url(nu):
            if nu not in data.embeds:
                data.embeds.append(nu)
        elif nu:
            data.page_links.append(nu)
    for t in soup.find_all(style=True):
        for u in css_urls(t["style"]):
            add_asset(u, "images", "style")
    for t in soup.find_all("style"):
        for u in css_urls(t.string or ""):
            add_asset(u, None, "style")

    # Media URLs buried in inline scripts / JSON player configs
    for s in soup.find_all("script"):
        if s.get("src"):
            continue
        for m in SCRIPT_MEDIA_RE.finditer(s.string or ""):
            raw = (m.group(1) or "https:") + m.group(2)
            raw = raw.replace("\\/", "/")
            cat = category_from_extension(raw)
            if cat in ("videos", "audio", "documents"):
                add_asset(raw, cat, "script")

    # ---- anchors ------------------------------------------------------------
    seen_links = set()
    for a in soup.find_all(["a", "area"], href=True):
        nu = norm(a["href"])
        if not nu:
            continue
        text = a.get_text(" ", strip=True)[:300]
        if nu not in seen_links:
            seen_links.add(nu)
            data.links.append({"url": nu, "text": text})
        if is_streaming_url(nu) and re.search(r"(watch|video|/v/|embed|player|/BV)", nu, re.I):
            if nu not in data.embeds:
                data.embeds.append(nu)
            continue
        cat = category_from_extension(nu)
        if cat and cat != "other":
            add_asset(nu, cat, "a")
        elif a.has_attr("download"):
            add_asset(nu, None, "a")
        elif looks_like_page(nu):
            data.page_links.append(nu)

    # ---- text ---------------------------------------------------------------
    data.headings = [
        {"level": h.name, "text": h.get_text(" ", strip=True)}
        for h in soup.find_all(re.compile(r"^h[1-6]$"))
        if h.get_text(strip=True)
    ]
    content = _main_content(soup)
    data.markdown = html_to_markdown(content, base)
    data.text = _plain_text(content)
    return data


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

_DROP_TAGS = ("script", "style", "noscript", "template", "svg", "canvas", "iframe", "form")


def _main_content(soup: BeautifulSoup) -> Tag:
    clone = make_soup(str(soup))
    for t in clone.find_all(_DROP_TAGS):
        t.decompose()
    for sel in ("main", "article", "[role=main]", "#content", ".content", "body"):
        node = clone.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            return node
    return clone.body or clone


def _plain_text(node: Tag) -> str:
    text = node.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def html_to_markdown(node: Tag, base: str = "") -> str:
    """Small, dependency-free HTML -> Markdown converter."""
    out: List[str] = []

    def inline(n) -> str:
        if isinstance(n, NavigableString):
            return re.sub(r"\s+", " ", str(n))
        if not isinstance(n, Tag):
            return ""
        name = n.name
        inner = "".join(inline(c) for c in n.children)
        if name in ("strong", "b"):
            return f"**{inner.strip()}**" if inner.strip() else ""
        if name in ("em", "i"):
            return f"*{inner.strip()}*" if inner.strip() else ""
        if name == "code":
            return f"`{inner.strip()}`"
        if name == "br":
            return "  \n"
        if name == "a":
            href = normalize_url(n.get("href", ""), base) or n.get("href", "")
            label = inner.strip() or href
            return f"[{label}]({href})" if href else label
        if name == "img":
            src = normalize_url(n.get("src", ""), base) or ""
            return f"![{n.get('alt', '').strip()}]({src})" if src else ""
        return inner

    def block(n, depth: int = 0) -> None:
        if isinstance(n, NavigableString):
            t = re.sub(r"\s+", " ", str(n)).strip()
            if t:
                out.append(t + "\n\n")
            return
        if not isinstance(n, Tag):
            return
        name = n.name
        if re.fullmatch(r"h[1-6]", name):
            t = inline(n).strip()
            if t:
                out.append("#" * int(name[1]) + " " + t + "\n\n")
        elif name == "p":
            t = inline(n).strip()
            if t:
                out.append(t + "\n\n")
        elif name in ("ul", "ol"):
            for i, li in enumerate(n.find_all("li", recursive=False), 1):
                bullet = f"{i}." if name == "ol" else "-"
                t = "".join(inline(c) for c in li.children if not (isinstance(c, Tag) and c.name in ("ul", "ol"))).strip()
                out.append("  " * depth + f"{bullet} {t}\n")
                for sub in li.find_all(["ul", "ol"], recursive=False):
                    block(sub, depth + 1)
            if depth == 0:
                out.append("\n")
        elif name == "pre":
            out.append("```\n" + n.get_text().rstrip() + "\n```\n\n")
        elif name == "blockquote":
            t = inline(n).strip()
            if t:
                out.append("> " + t.replace("\n", "\n> ") + "\n\n")
        elif name == "table":
            rows = n.find_all("tr")
            for i, tr in enumerate(rows):
                cells = [inline(c).strip().replace("|", "\\|") for c in tr.find_all(["td", "th"])]
                out.append("| " + " | ".join(cells) + " |\n")
                if i == 0:
                    out.append("|" + "---|" * len(cells) + "\n")
            out.append("\n")
        elif name in ("img",):
            t = inline(n)
            if t:
                out.append(t + "\n\n")
        elif name == "hr":
            out.append("---\n\n")
        else:
            has_block = any(
                isinstance(c, Tag) and c.name in _BLOCK_TAGS for c in n.children
            )
            if has_block:
                for c in n.children:
                    block(c, depth)
            else:
                t = inline(n).strip()
                if t:
                    out.append(t + "\n\n")

    block(node)
    md = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "header", "footer", "nav",
    "aside", "ul", "ol", "li", "table", "pre", "blockquote", "figure",
    "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "dl", "dt", "dd",
    "body", "html", "img", "details", "summary", "address",
}
