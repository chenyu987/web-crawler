"""URL helpers, asset classification and URL -> local path mapping."""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
from typing import Optional
from urllib.parse import unquote, urldefrag, urljoin, urlsplit, urlunsplit

# --------------------------------------------------------------------------- #
# Asset categories
# --------------------------------------------------------------------------- #

CATEGORIES = (
    "images", "videos", "audio", "documents", "archives",
    "styles", "scripts", "fonts", "other",
)

EXTENSIONS = {
    "images": {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico",
        ".tif", ".tiff", ".avif", ".heic", ".heif", ".jfif",
    },
    "videos": {
        ".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".m4v",
        ".mpg", ".mpeg", ".3gp", ".ogv", ".ts", ".m3u8", ".mpd",
    },
    "audio": {
        ".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus",
        ".wma", ".mid", ".midi",
    },
    "documents": {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt",
        ".ods", ".odp", ".rtf", ".txt", ".csv", ".tsv", ".md", ".epub",
        ".mobi", ".json", ".xml", ".pages", ".numbers", ".key", ".tex",
    },
    "archives": {
        ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".dmg",
        ".iso", ".apk", ".exe", ".msi", ".deb", ".rpm",
    },
    "styles": {".css"},
    "scripts": {".js", ".mjs"},
    "fonts": {".woff", ".woff2", ".ttf", ".otf", ".eot"},
}

_EXT_TO_CAT = {ext: cat for cat, exts in EXTENSIONS.items() for ext in exts}

# Extensions that are (probably) HTML pages and should be crawled, not downloaded
PAGE_EXTENSIONS = {
    "", ".html", ".htm", ".xhtml", ".shtml", ".php", ".asp", ".aspx",
    ".jsp", ".jspx", ".cfm", ".cgi", ".pl",
}

_MIME_PREFIX_TO_CAT = [
    ("image/", "images"),
    ("video/", "videos"),
    ("audio/", "audio"),
    ("font/", "fonts"),
    ("text/css", "styles"),
    ("application/javascript", "scripts"),
    ("text/javascript", "scripts"),
    ("application/x-javascript", "scripts"),
    ("application/pdf", "documents"),
    ("application/msword", "documents"),
    ("application/vnd.openxmlformats", "documents"),
    ("application/vnd.ms-", "documents"),
    ("application/vnd.oasis.opendocument", "documents"),
    ("application/rtf", "documents"),
    ("application/epub", "documents"),
    ("text/plain", "documents"),
    ("text/csv", "documents"),
    ("application/json", "documents"),
    ("application/zip", "archives"),
    ("application/x-rar", "archives"),
    ("application/x-7z", "archives"),
    ("application/gzip", "archives"),
    ("application/x-tar", "archives"),
    ("application/vnd.apple.mpegurl", "videos"),
    ("application/x-mpegurl", "videos"),
    ("application/dash+xml", "videos"),
    ("application/font", "fonts"),
    ("application/x-font", "fonts"),
]

# Hosts whose embedded players need yt-dlp to download
STREAMING_HOSTS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com", "vimeo.com",
    "player.vimeo.com", "bilibili.com", "b23.tv", "dailymotion.com",
    "twitch.tv", "tiktok.com", "facebook.com", "fb.watch", "instagram.com",
    "twitter.com", "x.com", "v.qq.com", "youku.com", "ixigua.com",
    "douyin.com", "wistia.com", "wistia.net", "loom.com", "ted.com",
    "soundcloud.com", "streamable.com", "rumble.com",
)


def url_extension(url: str) -> str:
    path = urlsplit(url).path
    ext = posixpath.splitext(unquote(path))[1].lower()
    # ignore weird "extensions" like ".com/whatever" or very long tails
    if len(ext) > 6 or not re.fullmatch(r"\.[a-z0-9]+", ext or ".x"):
        return ""
    return ext


def category_from_extension(url: str) -> Optional[str]:
    return _EXT_TO_CAT.get(url_extension(url))


def category_from_mime(content_type: Optional[str]) -> Optional[str]:
    if not content_type:
        return None
    ct = content_type.split(";")[0].strip().lower()
    for prefix, cat in _MIME_PREFIX_TO_CAT:
        if ct.startswith(prefix):
            return cat
    return None


def is_html_mime(content_type: Optional[str]) -> bool:
    if not content_type:
        return False
    ct = content_type.split(";")[0].strip().lower()
    return ct in ("text/html", "application/xhtml+xml")


def looks_like_page(url: str) -> bool:
    return url_extension(url) in PAGE_EXTENSIONS


def is_streaming_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in STREAMING_HOSTS)


# --------------------------------------------------------------------------- #
# URL normalisation and scope
# --------------------------------------------------------------------------- #

_TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|_ga|spm|from)$", re.I
)


def normalize_url(url: str, base: Optional[str] = None) -> Optional[str]:
    """Resolve ``url`` against ``base`` and canonicalise it.

    Returns ``None`` for non-http(s) URLs (mailto:, javascript:, data: ...).
    """
    if url is None:
        return None
    url = url.strip()
    if not url or url.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "blob:")):
        return None
    if base:
        url = urljoin(base, url)
    url, _ = urldefrag(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    port = parts.port if parts.port else None
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    query = "&".join(
        p for p in parts.query.split("&")
        if p and not _TRACKING_PARAMS.match(p.split("=", 1)[0])
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def in_scope(url: str, start_url: str, scope: str) -> bool:
    """Whether ``url`` belongs to the crawl defined by ``start_url``.

    scope:
      * ``host``   - exactly the same host
      * ``domain`` - same host or any subdomain (www. is ignored)
      * ``prefix`` - same host and path starts with the start URL's directory
      * ``any``    - everything (be careful!)
    """
    if scope == "any":
        return True
    u, s = urlsplit(url), urlsplit(start_url)
    uh, sh = (u.hostname or "").lower(), (s.hostname or "").lower()
    if scope == "host":
        return uh == sh
    if scope == "domain":
        base = strip_www(sh)
        return uh == base or uh.endswith("." + base)
    if scope == "prefix":
        if uh != sh:
            return False
        prefix = s.path if s.path.endswith("/") else posixpath.dirname(s.path) + "/"
        return (u.path or "/").startswith(prefix)
    raise ValueError(f"unknown scope: {scope}")


# --------------------------------------------------------------------------- #
# Local paths
# --------------------------------------------------------------------------- #

_UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')
MAX_SEGMENT = 120


_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}
MAX_RELPATH = 160  # keep full paths under Windows' 260-char limit


def _safe_segment(seg: str) -> str:
    seg = _UNSAFE.sub("_", unquote(seg)).replace("/", "_").strip()
    seg = seg.rstrip(". ")  # Windows silently drops trailing dots/spaces
    if seg in ("", ".", ".."):
        seg = "_"
    if seg.split(".")[0].lower() in _WIN_RESERVED:
        seg = "_" + seg
    if len(seg.encode("utf-8")) > MAX_SEGMENT:
        root, ext = os.path.splitext(seg)
        digest = hashlib.sha1(seg.encode("utf-8")).hexdigest()[:10]
        root = root.encode("utf-8")[: MAX_SEGMENT - len(ext) - 12].decode("utf-8", "ignore")
        seg = f"{root}_{digest}{ext}"
    return seg


def short_hash(text: str, n: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def url_to_relpath(url: str, *, page: bool, default_ext: str = "") -> str:
    """Map a URL to a relative filesystem path like ``host/dir/file.ext``.

    Pages always end in ``.html`` (``/`` -> ``index.html``). The query string,
    when present, is folded into the file name as a short hash so different
    query variants do not overwrite each other.
    """
    parts = urlsplit(url)
    host = parts.netloc.replace(":", "_")
    path = parts.path or "/"
    segs = [s for s in path.split("/") if s]
    if path.endswith("/") or not segs:
        segs.append("index.html" if page else "index" + (default_ext or ""))
    segs = [_safe_segment(s) for s in segs]
    name = segs[-1]
    root, ext = os.path.splitext(name)
    if page:
        if ext.lower() not in (".html", ".htm"):
            root, ext = name, ".html"
    elif not ext and default_ext:
        ext = default_ext
    if parts.query:
        root = f"{root}_{short_hash(parts.query)}"
    segs[-1] = _safe_segment(root + ext)
    rel = posixpath.join(host, *segs)
    if len(rel) > MAX_RELPATH:
        # collapse the middle directories into a hash so the path stays short
        tail = segs[-1]
        rel = posixpath.join(host, "_long", short_hash(rel, 12), tail)
    return rel


def relative_link(from_file: str, to_file: str) -> str:
    """Relative POSIX URL path from one local file to another."""
    start = posixpath.dirname(from_file.replace(os.sep, "/")) or "."
    rel = posixpath.relpath(to_file.replace(os.sep, "/"), start)
    # percent-encode characters that would break an href
    return rel.replace("%", "%25").replace(" ", "%20").replace("#", "%23").replace("?", "%3F")


def mime_default_ext(content_type: Optional[str]) -> str:
    import mimetypes

    if not content_type:
        return ""
    ct = content_type.split(";")[0].strip().lower()
    fixed = {
        "image/jpeg": ".jpg", "image/svg+xml": ".svg", "image/webp": ".webp",
        "text/plain": ".txt", "application/javascript": ".js",
        "text/javascript": ".js", "video/mp4": ".mp4", "audio/mpeg": ".mp3",
        "application/vnd.apple.mpegurl": ".m3u8", "font/woff2": ".woff2",
    }
    return fixed.get(ct) or mimetypes.guess_extension(ct) or ""
