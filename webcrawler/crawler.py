"""The crawl engine: walks a site, saves pages, text and assets, builds a mirror."""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import Deque, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

from . import __version__
from .extractor import PageData, css_urls, extract, make_soup, parse_srcset, CSS_URL_RE, CSS_IMPORT_RE
from .fetcher import DEFAULT_UA, RateLimiter, Renderer, RobotsCache, build_session
from .media import VideoDownloader, yt_dlp_available
from .utils import (
    CATEGORIES,
    category_from_extension,
    category_from_mime,
    in_scope,
    is_html_mime,
    looks_like_page,
    mime_default_ext,
    normalize_url,
    relative_link,
    url_to_relpath,
)

log = logging.getLogger("webcrawler")


@dataclass
class CrawlConfig:
    start_urls: List[str]
    output_dir: str = "output"
    max_pages: int = 500
    max_depth: int = 10                 # -1 = unlimited
    scope: str = "domain"               # host | domain | prefix | any
    include: List[str] = field(default_factory=list)   # regexes pages must match
    exclude: List[str] = field(default_factory=list)   # regexes to skip
    types: Set[str] = field(default_factory=lambda: set(CATEGORIES))
    workers: int = 8
    delay: float = 0.2                  # seconds between requests per host
    timeout: float = 30
    max_file_size: int = 2 * 1024 ** 3  # bytes, per asset
    max_assets: int = 0                 # 0 = unlimited
    asset_scope: str = "any"            # any | domain  (where assets may live)
    respect_robots: bool = True
    user_agent: str = DEFAULT_UA
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    proxy: Optional[str] = None
    verify_ssl: bool = True
    render: bool = False                # use headless Chromium
    render_wait: float = 1.5
    browser_path: Optional[str] = None
    use_sitemap: bool = True
    streaming_videos: bool = True       # use yt-dlp for youtube/bilibili/m3u8...
    video_max_height: Optional[int] = None
    cookies_file: Optional[str] = None  # Netscape cookies.txt for yt-dlp
    mirror: bool = True                 # rewrite links so pages work offline
    skip_existing: bool = True          # don't re-download existing asset files


@dataclass
class AssetRecord:
    url: str
    category: str
    local_path: str
    size: int
    content_type: str
    found_on: str
    status: str = "ok"


class Crawler:
    def __init__(self, config: CrawlConfig):
        self.cfg = config
        self.out = os.path.abspath(config.output_dir)
        self.session = build_session(
            config.user_agent, config.headers, config.cookies,
            pool_size=max(16, config.workers * 4), proxy=config.proxy,
            verify=config.verify_ssl,
        )
        self.rate = RateLimiter(config.delay)
        self.robots = RobotsCache(self.session, config.user_agent, config.respect_robots)
        self.renderer: Optional[Renderer] = None
        if config.render:
            self.renderer = Renderer(config.user_agent, config.render_wait, config.browser_path)
        self.video: Optional[VideoDownloader] = None
        if config.streaming_videos and "videos" in config.types:
            if yt_dlp_available():
                self.video = VideoDownloader(
                    os.path.join(self.out, "assets", "videos", "_streams"),
                    max_height=config.video_max_height,
                    cookies_file=config.cookies_file, proxy=config.proxy,
                )
            else:
                log.warning("yt-dlp not installed: embedded/streaming videos will only be listed "
                            "(pip install yt-dlp to download them)")

        self.start_urls = [u for u in (normalize_url(u) for u in config.start_urls) if u]
        if not self.start_urls:
            raise ValueError("no valid start URL")
        self._include = [re.compile(p) for p in config.include]
        self._exclude = [re.compile(p) for p in config.exclude]

        # state (only touched from the main thread unless noted)
        self.frontier: Deque[Tuple[str, int]] = deque()
        self.seen_pages: Set[str] = set()
        self.seen_page_paths: Set[str] = set()
        self.page_path_map: Dict[str, str] = {}  # url_to_relpath(page) -> local html
        self.seen_assets: Set[str] = set()
        self.url_map: Dict[str, str] = {}       # url -> path relative to self.out
        self.pages: List[dict] = []
        self.assets: List[AssetRecord] = []
        self.links: List[Tuple[str, str, str, bool]] = []
        self.embeds: Dict[str, str] = {}        # url -> found_on
        self.errors: List[Tuple[str, str]] = []
        self.page_files: List[Tuple[str, str]] = []   # (page url, local html path)
        self.css_files: List[Tuple[str, str]] = []
        self._lock = threading.Lock()
        self._stop = False

    # ------------------------------------------------------------------ #
    # scope helpers
    # ------------------------------------------------------------------ #

    def _page_in_scope(self, url: str) -> bool:
        if not any(in_scope(url, s, self.cfg.scope) for s in self.start_urls):
            return False
        if self._include and not any(p.search(url) for p in self._include):
            return False
        if any(p.search(url) for p in self._exclude):
            return False
        return True

    def _asset_allowed(self, url: str) -> bool:
        if self.cfg.asset_scope == "domain":
            if not any(in_scope(url, s, "domain") for s in self.start_urls):
                return False
        return not any(p.search(url) for p in self._exclude)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def run(self) -> dict:
        os.makedirs(self.out, exist_ok=True)
        t0 = time.time()
        for u in self.start_urls:
            self._enqueue_page(u, 0)
        if self.cfg.use_sitemap:
            for u in self._sitemap_urls():
                self._enqueue_page(u, 1)

        page_pool = ThreadPoolExecutor(self.cfg.workers, thread_name_prefix="page")
        asset_pool = ThreadPoolExecutor(self.cfg.workers, thread_name_prefix="asset")
        pending: Dict[Future, str] = {}
        try:
            while (self.frontier or pending) and not self._stop:
                while (self.frontier and len(pending) < self.cfg.workers * 2
                       and self._pages_budget_left(pending)):
                    url, depth = self.frontier.popleft()
                    fut = page_pool.submit(self._fetch_page, url, depth)
                    pending[fut] = "page"
                if not pending:
                    break
                done, _ = wait(list(pending), timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    kind = pending.pop(fut)
                    try:
                        result = fut.result()
                    except Exception as e:  # pragma: no cover - defensive
                        log.exception("worker crashed: %s", e)
                        continue
                    if kind == "page":
                        self._handle_page_result(result, asset_pool, pending)
                    else:
                        self._handle_asset_result(result, asset_pool, pending)
                if not self._pages_budget_left(pending):
                    self.frontier.clear()
        except KeyboardInterrupt:
            log.warning("interrupted - finishing up and writing what we have")
            self._stop = True
        finally:
            page_pool.shutdown(wait=True, cancel_futures=True)
            # drain remaining asset downloads
            try:
                while pending and not self._stop:
                    done, _ = wait(list(pending), timeout=1.0, return_when=FIRST_COMPLETED)
                    for fut in done:
                        kind = pending.pop(fut)
                        if kind == "asset":
                            self._handle_asset_result(fut.result(), asset_pool, pending)
                        else:
                            self._handle_page_result(fut.result(), asset_pool, pending)
            except KeyboardInterrupt:
                self._stop = True
            asset_pool.shutdown(wait=not self._stop, cancel_futures=True)

        if self.video and self.embeds and not self._stop:
            self._download_streams()
        if self.renderer:
            self.renderer.close()
        if self.cfg.mirror:
            self._rewrite_mirror()
        summary = self._write_reports(time.time() - t0)
        return summary

    # ------------------------------------------------------------------ #
    # frontier
    # ------------------------------------------------------------------ #

    def _pages_budget_left(self, pending) -> bool:
        if self.cfg.max_pages <= 0:
            return True
        in_flight = sum(1 for k in pending.values() if k == "page")
        return len(self.pages) + in_flight < self.cfg.max_pages

    def _enqueue_page(self, url: str, depth: int) -> None:
        if url in self.seen_pages:
            return
        if self.cfg.max_depth >= 0 and depth > self.cfg.max_depth:
            return
        if not self._page_in_scope(url):
            return
        self.seen_pages.add(url)
        # "/", "/index.html" etc. map to the same file -> fetch only once
        local = url_to_relpath(url, page=True)
        if local in self.seen_page_paths:
            return
        self.seen_page_paths.add(local)
        self.frontier.append((url, depth))

    def _enqueue_asset(self, url: str, category: Optional[str], found_on: str,
                       pool: ThreadPoolExecutor, pending: Dict[Future, str]) -> None:
        if url in self.seen_assets or url in self.url_map:
            return
        if category and category not in self.cfg.types:
            return
        if not self._asset_allowed(url):
            return
        if self.cfg.max_assets and len(self.seen_assets) >= self.cfg.max_assets:
            return
        self.seen_assets.add(url)
        if self.video and url.lower().split("?")[0].endswith((".m3u8", ".mpd")):
            self.embeds.setdefault(url, found_on)
            return
        fut = pool.submit(self._download_asset, url, category, found_on)
        pending[fut] = "asset"

    def _sitemap_urls(self) -> List[str]:
        found: List[str] = []
        roots = {f"{u.split('/')[0]}//{u.split('/')[2]}" for u in self.start_urls}
        candidates: List[str] = []
        for root in roots:
            candidates += self.robots.sitemaps(root + "/") or []
            candidates.append(root + "/sitemap.xml")
        seen: Set[str] = set()
        while candidates and len(seen) < 50:
            sm = candidates.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            try:
                self.rate.wait(sm)
                r = self.session.get(sm, timeout=self.cfg.timeout)
                if r.status_code != 200 or b"<" not in r.content[:200]:
                    continue
                soup = BeautifulSoup(r.content, "xml")
            except Exception:
                continue
            for loc in soup.find_all("loc"):
                u = normalize_url(loc.get_text(strip=True))
                if not u:
                    continue
                if loc.parent and loc.parent.name == "sitemap":
                    candidates.append(u)
                else:
                    found.append(u)
        if found:
            log.info("sitemap: %d URLs", len(found))
        return found

    # ------------------------------------------------------------------ #
    # page handling (worker thread)
    # ------------------------------------------------------------------ #

    def _get(self, url: str, stream: bool = False) -> requests.Response:
        self.rate.wait(url)
        return self.session.get(url, timeout=self.cfg.timeout, stream=stream, allow_redirects=True)

    def _fetch_page(self, url: str, depth: int) -> dict:
        res = {"url": url, "depth": depth, "ok": False}
        if not self.robots.allowed(url):
            res["error"] = "blocked by robots.txt"
            return res
        try:
            r = self._get(url, stream=True)
        except requests.RequestException as e:
            res["error"] = f"{type(e).__name__}: {e}"
            return res
        final = normalize_url(r.url) or url
        res.update(final_url=final, status=r.status_code,
                   content_type=r.headers.get("Content-Type", ""))
        if r.status_code >= 400:
            r.close()
            res["error"] = f"HTTP {r.status_code}"
            return res
        if not is_html_mime(res["content_type"]):
            # It was a file, not a page: hand over to the asset logic.
            res["asset"] = self._save_response(r, url, None, found_on="(linked)")
            return res
        try:
            html_bytes = r.content
        finally:
            r.close()
        html = _decode(html_bytes, r)
        if self.renderer:
            try:
                html = self.renderer.render(final, self.cfg.timeout)
            except Exception as e:
                log.warning("render failed for %s (%s); using raw HTML", final, e)
        data = extract(html, final)
        rel = os.path.join("pages", url_to_relpath(final, page=True))
        self._write_file(rel, html.encode("utf-8"))
        text_rel = os.path.join("text", os.path.splitext(url_to_relpath(final, page=True))[0] + ".md")
        header = f"# {data.title}\n\nSource: {final}\n\n" if data.title else f"Source: {final}\n\n"
        self._write_file(text_rel, (header + data.markdown).encode("utf-8"))
        res.update(ok=True, data=data, local_html=rel, local_text=text_rel)
        return res

    def _handle_page_result(self, res: dict, pool, pending) -> None:
        url = res["url"]
        if res.get("asset"):
            self._handle_asset_result(res["asset"], pool, pending)
            return
        if not res.get("ok"):
            self.errors.append((url, res.get("error", "unknown")))
            log.info("  ✗ %s (%s)", url, res.get("error"))
            return
        data: PageData = res["data"]
        final = res["final_url"]
        self.url_map[url] = res["local_html"]
        self.url_map[final] = res["local_html"]
        self.seen_pages.add(final)
        self.seen_page_paths.add(url_to_relpath(final, page=True))
        self.page_files.append((final, res["local_html"]))
        self.page_path_map[url_to_relpath(final, page=True)] = res["local_html"]
        self.pages.append({
            "url": url, "final_url": final, "depth": res["depth"], "status": res["status"],
            "title": data.title, "description": data.description, "language": data.language,
            "meta": data.meta, "headings": data.headings, "json_ld": data.json_ld,
            "text": data.text, "local_html": res["local_html"], "local_text": res["local_text"],
            "n_links": len(data.links), "n_assets": len(data.assets), "embeds": data.embeds,
            "images": data.images,
        })
        log.info("[%d] %s  — %s", len(self.pages), final, (data.title or "")[:60])
        for link in data.links:
            self.links.append((final, link["url"], link["text"], self._page_in_scope(link["url"])))
        for u in data.page_links:
            self._enqueue_page(u, res["depth"] + 1)
        for a in data.assets:
            # a "page-looking" anchor target that's in scope gets crawled as a page instead
            if a.tag == "a" and a.category is None and looks_like_page(a.url) and self._page_in_scope(a.url):
                self._enqueue_page(a.url, res["depth"] + 1)
                continue
            self._enqueue_asset(a.url, a.category, final, pool, pending)
        for e in data.embeds:
            self.embeds.setdefault(e, final)

    # ------------------------------------------------------------------ #
    # assets (worker thread)
    # ------------------------------------------------------------------ #

    def _download_asset(self, url: str, category: Optional[str], found_on: str) -> dict:
        # Fast path: file already on disk from a previous run
        if self.cfg.skip_existing and category:
            rel = os.path.join("assets", category, url_to_relpath(url, page=False))
            full = os.path.join(self.out, rel)
            if os.path.isfile(full) and os.path.getsize(full) > 0:
                return {"record": AssetRecord(url, category, rel, os.path.getsize(full), "",
                                              found_on, "cached")}
        if not self.robots.allowed(url):
            return {"url": url, "error": "blocked by robots.txt"}
        try:
            r = self._get(url, stream=True)
        except requests.RequestException as e:
            return {"url": url, "error": f"{type(e).__name__}: {e}"}
        if r.status_code >= 400:
            r.close()
            return {"url": url, "error": f"HTTP {r.status_code}"}
        return self._save_response(r, url, category, found_on)

    def _save_response(self, r: requests.Response, url: str, category: Optional[str],
                       found_on: str) -> dict:
        ctype = r.headers.get("Content-Type", "")
        try:
            if is_html_mime(ctype) and category not in ("documents",):
                return {"url": url, "error": "is an HTML page, not a file", "html": True}
            cat = category_from_extension(url) or category_from_mime(ctype) or category or "other"
            if cat not in self.cfg.types:
                return {"url": url, "skipped": True}
            length = int(r.headers.get("Content-Length") or 0)
            if self.cfg.max_file_size and length > self.cfg.max_file_size:
                return {"url": url, "error": f"too large ({length} bytes)"}
            default_ext = mime_default_ext(ctype)
            rel = os.path.join("assets", cat, url_to_relpath(url, page=False, default_ext=default_ext))
            full = os.path.join(self.out, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            size = 0
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(full), prefix=".part-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if self.cfg.max_file_size and size > self.cfg.max_file_size:
                            raise _TooLarge()
                        fh.write(chunk)
                if os.path.isdir(full):  # name clash with a directory
                    full = full.rstrip("/\\") + "_file"
                    rel = os.path.relpath(full, self.out)
                os.replace(tmp, full)
            except _TooLarge:
                os.unlink(tmp)
                return {"url": url, "error": "too large"}
            except Exception as e:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                return {"url": url, "error": f"{type(e).__name__}: {e}"}
            final = normalize_url(r.url) or url
            return {"record": AssetRecord(url, cat, rel, size, ctype.split(";")[0], found_on),
                    "final_url": final}
        finally:
            r.close()

    def _handle_asset_result(self, res: dict, pool, pending) -> None:
        if res.get("skipped"):
            return
        if "record" not in res:
            if not res.get("html"):
                self.errors.append((res.get("url", "?"), res.get("error", "unknown")))
            return
        rec: AssetRecord = res["record"]
        self.assets.append(rec)
        self.url_map[rec.url] = rec.local_path
        if res.get("final_url"):
            self.url_map.setdefault(res["final_url"], rec.local_path)
        log.debug("  ↓ %s %s (%s)", rec.category, rec.url, _human(rec.size))
        if rec.category == "styles":
            self.css_files.append((res.get("final_url") or rec.url, rec.local_path))
            try:
                with open(os.path.join(self.out, rec.local_path), "r", encoding="utf-8", errors="replace") as fh:
                    css = fh.read()
            except OSError:
                return
            base = res.get("final_url") or rec.url
            for u in css_urls(css):
                nu = normalize_url(u, base)
                if nu:
                    self._enqueue_asset(nu, category_from_extension(nu), rec.url, pool, pending)

    def _download_streams(self) -> None:
        log.info("downloading %d embedded/streaming videos with yt-dlp ...", len(self.embeds))
        for url, found_on in list(self.embeds.items()):
            if self._stop:
                break
            try:
                files = self.video.download(url)
            except KeyboardInterrupt:
                self._stop = True
                break
            except Exception as e:
                self.errors.append((url, f"yt-dlp: {str(e)[:200]}"))
                log.info("  ✗ video %s (%s)", url, str(e)[:120])
                continue
            for i, f in enumerate(files):
                rel = os.path.relpath(f, self.out)
                self.assets.append(AssetRecord(url, "videos", rel, os.path.getsize(f),
                                               "video/stream", found_on))
                if i == 0:
                    self.url_map.setdefault(url, rel)
                log.info("  ▶ %s -> %s", url, rel)

    # ------------------------------------------------------------------ #
    # offline mirror
    # ------------------------------------------------------------------ #

    def _local_target(self, raw: str, base: str, from_rel: str) -> Optional[str]:
        nu = normalize_url(raw, base)
        if not nu:
            return None
        target = self.url_map.get(nu) or self.page_path_map.get(url_to_relpath(nu, page=True))
        if target:
            frag = raw.split("#", 1)[1] if "#" in raw else ""
            link = relative_link(from_rel, target)
            return link + ("#" + frag if frag else "")
        return nu  # make it absolute so it still works from disk

    def _rewrite_css(self, css: str, base: str, from_rel: str) -> str:
        def sub_url(m):
            raw = m.group(2).strip()
            if raw.startswith(("data:", "#")):
                return m.group(0)
            new = self._local_target(raw, base, from_rel) or raw
            return f'url("{new}")'

        css = CSS_URL_RE.sub(sub_url, css)

        def sub_import(m):
            new = self._local_target(m.group(1), base, from_rel) or m.group(1)
            return m.group(0).replace(m.group(1), new)

        return CSS_IMPORT_RE.sub(sub_import, css)

    def _rewrite_mirror(self) -> None:
        log.info("rewriting links for offline browsing ...")
        url_attrs = ("href", "src", "poster", "data-src", "data-original", "data-lazy-src", "action", "data")
        for page_url, rel in self.page_files:
            path = os.path.join(self.out, rel)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    soup = make_soup(fh.read())
            except OSError:
                continue
            base = page_url
            bt = soup.find("base", href=True)
            if bt:
                base = normalize_url(bt["href"], page_url) or page_url
                bt.decompose()
            for tag in soup.find_all(True):
                for attr in url_attrs:
                    v = tag.get(attr)
                    if not v or not isinstance(v, str):
                        continue
                    if v.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                        continue
                    new = self._local_target(v, base, rel)
                    if new:
                        tag[attr] = new
                for attr in ("srcset", "data-srcset"):
                    v = tag.get(attr)
                    if v:
                        parts = []
                        for item in v.split(","):
                            bits = item.strip().split()
                            if not bits:
                                continue
                            bits[0] = self._local_target(bits[0], base, rel) or bits[0]
                            parts.append(" ".join(bits))
                        tag[attr] = ", ".join(parts)
                if tag.get("style"):
                    tag["style"] = self._rewrite_css(tag["style"], base, rel)
                # integrity hashes break once files are served from disk
                if tag.name in ("script", "link") and tag.get("integrity"):
                    del tag["integrity"]
            for st in soup.find_all("style"):
                if st.string:
                    st.string = self._rewrite_css(st.string, base, rel)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(soup))
        md_link = re.compile(r"(\]\()([^)\s]+)(\))")
        for p in self.pages:
            path = os.path.join(self.out, p["local_text"])
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    md = fh.read()
            except OSError:
                continue

            def sub_md(m, rel=p["local_text"]):
                target = self.url_map.get(m.group(2))
                if not target:
                    return m.group(0)
                return m.group(1) + relative_link(rel, target) + m.group(3)

            with open(path, "w", encoding="utf-8") as fh:
                fh.write(md_link.sub(sub_md, md))
        for css_url, rel in self.css_files:
            path = os.path.join(self.out, rel)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    css = fh.read()
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self._rewrite_css(css, css_url, rel))
            except OSError:
                continue

    # ------------------------------------------------------------------ #
    # output
    # ------------------------------------------------------------------ #

    def _write_file(self, rel: str, content: bytes) -> None:
        full = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if os.path.isdir(full):
            full = full + "_"
        with open(full, "wb") as fh:
            fh.write(content)

    def _write_reports(self, elapsed: float) -> dict:
        data_dir = os.path.join(self.out, "data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "pages.jsonl"), "w", encoding="utf-8") as fh:
            for p in self.pages:
                fh.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
        _write_csv(os.path.join(data_dir, "links.csv"),
                   ["from_page", "url", "text", "internal"], self.links)
        _write_csv(os.path.join(data_dir, "assets.csv"),
                   ["url", "category", "local_path", "size", "content_type", "found_on", "status"],
                   [tuple(asdict(a).values()) for a in self.assets])
        img_rows = []
        for p in self.pages:
            for im in p.get("images", []):
                img_rows.append({
                    "local_path": self.url_map.get(im["url"], ""), **im,
                    "page_title": p["title"], "page_url": p["final_url"],
                    "page_description": p["description"],
                })
        _write_csv(os.path.join(data_dir, "images.csv"),
                   ["local_path", "url", "alt", "title", "caption", "heading", "context",
                    "page_title", "page_url", "page_description"],
                   [tuple(r[k] for k in ("local_path", "url", "alt", "title", "caption", "heading",
                                         "context", "page_title", "page_url", "page_description"))
                    for r in img_rows])
        with open(os.path.join(data_dir, "images.jsonl"), "w", encoding="utf-8") as fh:
            for r in img_rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        _write_csv(os.path.join(data_dir, "embeds.csv"), ["url", "found_on", "local_path"],
                   [(u, f, self.url_map.get(u, "")) for u, f in self.embeds.items()])
        _write_csv(os.path.join(data_dir, "errors.csv"), ["url", "error"], self.errors)

        by_cat: Dict[str, Dict[str, int]] = {}
        for a in self.assets:
            c = by_cat.setdefault(a.category, {"count": 0, "bytes": 0})
            c["count"] += 1
            c["bytes"] += a.size
        summary = {
            "tool": f"webcrawler {__version__}",
            "start_urls": self.start_urls,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(elapsed, 1),
            "pages": len(self.pages),
            "links": len(self.links),
            "unique_links": len({l[1] for l in self.links}),
            "assets": by_cat,
            "embedded_videos": len(self.embeds),
            "errors": len(self.errors),
            "output_dir": self.out,
        }
        with open(os.path.join(self.out, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        self._write_index(summary)
        return summary

    def _write_index(self, summary: dict) -> None:
        import html as h

        rows = []
        for p in self.pages:
            rows.append(
                f'<tr><td><a href="{relative_link("index.html", p["local_html"])}">'
                f'{h.escape(p["title"] or p["final_url"])}</a></td>'
                f'<td><a href="{relative_link("index.html", p["local_text"])}">text</a></td>'
                f'<td class="u"><a href="{h.escape(p["final_url"])}">{h.escape(p["final_url"])}</a></td></tr>'
            )
        sections = []
        for cat in CATEGORIES:
            items = [a for a in self.assets if a.category == cat]
            if not items:
                continue
            lis = "".join(
                f'<li><a href="{relative_link("index.html", a.local_path)}">'
                f'{h.escape(os.path.basename(a.local_path))}</a> <small>{_human(a.size)}</small></li>'
                for a in items[:2000]
            )
            more = f"<p>… and {len(items) - 2000} more</p>" if len(items) > 2000 else ""
            sections.append(f"<details><summary>{cat} ({len(items)})</summary><ul>{lis}</ul>{more}</details>")
        embeds = "".join(
            f'<li><a href="{h.escape(u)}">{h.escape(u)}</a>'
            + (f' → <a href="{relative_link("index.html", self.url_map[u])}">local</a>' if u in self.url_map else "")
            + "</li>"
            for u in self.embeds
        )
        doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crawl: {h.escape(", ".join(self.start_urls))}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 16px}}
table{{border-collapse:collapse;width:100%}}td{{border-bottom:1px solid #ddd;padding:4px 8px;vertical-align:top}}
.u{{word-break:break-all;color:#666;font-size:12px}}summary{{cursor:pointer;font-weight:600;margin:.5em 0}}
code{{background:#f3f3f3;padding:1px 4px}}</style></head><body>
<h1>Crawl report</h1>
<p>Start: {", ".join(f'<a href="{h.escape(u)}">{h.escape(u)}</a>' for u in self.start_urls)}<br>
Finished {summary["finished_at"]} in {summary["elapsed_seconds"]}s ·
{summary["pages"]} pages · {sum(c["count"] for c in summary["assets"].values())} files ·
{summary["unique_links"]} unique links · {summary["errors"]} errors</p>
<p>Data: <code>data/pages.jsonl</code> · <code>data/links.csv</code> · <code>data/assets.csv</code> ·
<code>data/embeds.csv</code> · <code>data/errors.csv</code> · <code>manifest.json</code></p>
<h2>Files</h2>{"".join(sections) or "<p>none</p>"}
<h2>Embedded / streaming videos ({len(self.embeds)})</h2><ul>{embeds}</ul>
<h2>Pages ({len(self.pages)})</h2><table>{"".join(rows)}</table>
</body></html>"""
        with open(os.path.join(self.out, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(doc)


class _TooLarge(Exception):
    pass


def _decode(content: bytes, r: requests.Response) -> str:
    enc = None
    ct = r.headers.get("Content-Type", "")
    if "charset=" in ct.lower():
        enc = ct.lower().split("charset=")[-1].split(";")[0].strip()
    if not enc:
        m = re.search(rb'<meta[^>]+charset=["\']?([a-zA-Z0-9_\-]+)', content[:4096], re.I)
        if m:
            enc = m.group(1).decode("ascii", "ignore")
    if not enc:
        enc = r.apparent_encoding or "utf-8"
    try:
        return content.decode(enc, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def _write_csv(path: str, header: List[str], rows) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
