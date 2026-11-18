"""HTTP fetching: polite session, robots.txt, rate limiting, optional JS rendering."""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional
from urllib import robotparser
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("webcrawler")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 webcrawler/0.1"
)


def build_session(user_agent: str, headers: Optional[Dict[str, str]] = None,
                  cookies: Optional[Dict[str, str]] = None, retries: int = 3,
                  pool_size: int = 32, proxy: Optional[str] = None,
                  verify: bool = True) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries, backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"), respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_size, pool_maxsize=pool_size)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    if headers:
        s.headers.update(headers)
    if cookies:
        s.cookies.update(cookies)
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    s.verify = verify
    return s


class RateLimiter:
    """Minimum delay between requests to the same host (thread-safe)."""

    def __init__(self, delay: float):
        self.delay = max(0.0, delay)
        self._next: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        if self.delay <= 0:
            return
        host = urlsplit(url).netloc
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + self.delay
        sleep = slot - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)


class RobotsCache:
    def __init__(self, session: requests.Session, user_agent: str, enabled: bool = True):
        self.session = session
        self.user_agent = user_agent
        self.enabled = enabled
        self._cache: Dict[str, Optional[robotparser.RobotFileParser]] = {}
        self._lock = threading.Lock()

    def _get(self, url: str) -> Optional[robotparser.RobotFileParser]:
        p = urlsplit(url)
        root = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if root in self._cache:
                return self._cache[root]
        rp: Optional[robotparser.RobotFileParser] = robotparser.RobotFileParser()
        try:
            r = self.session.get(root + "/robots.txt", timeout=15)
            if r.status_code >= 400:
                rp = None  # no robots.txt -> everything allowed
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            rp = None
        with self._lock:
            self._cache[root] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        rp = self._get(url)
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def sitemaps(self, url: str):
        rp = self._get(url)
        return list(rp.site_maps() or []) if rp else []


class Renderer:
    """Render pages with headless Chromium (Playwright) so JS content appears.

    One browser per worker thread because Playwright's sync API is not
    thread-safe.
    """

    def __init__(self, user_agent: str, wait: float = 1.5, executable_path: Optional[str] = None):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise SystemExit(
                "--render needs Playwright: pip install playwright && playwright install chromium"
            ) from e
        self.user_agent = user_agent
        self.wait = wait
        self.executable_path = executable_path
        self._local = threading.local()
        self._all = []
        self._lock = threading.Lock()

    def _page(self):
        if getattr(self._local, "ctx", None) is None:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            kw = {"headless": True}
            if self.executable_path:
                kw["executable_path"] = self.executable_path
            browser = pw.chromium.launch(**kw)
            ctx = browser.new_context(user_agent=self.user_agent)
            self._local.pw, self._local.browser, self._local.ctx = pw, browser, ctx
            with self._lock:
                self._all.append((pw, browser))
        return self._local.ctx.new_page()

    def render(self, url: str, timeout: float = 30) -> str:
        page = self._page()
        try:
            page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            # scroll to trigger lazy loading
            page.evaluate(
                """async () => { for (let y = 0; y < document.body.scrollHeight; y += 800) {
                       window.scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); } }"""
            )
            if self.wait:
                page.wait_for_timeout(self.wait * 1000)
            return page.content()
        finally:
            page.close()

    def close(self) -> None:
        # Each browser belongs to the thread that created it; closing from
        # another thread can raise, so be forgiving.
        for pw, browser in self._all:
            try:
                browser.close()
                pw.stop()
            except Exception:
                pass
