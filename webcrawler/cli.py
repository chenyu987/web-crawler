"""Command line interface: ``webcrawler https://example.com -o out``."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from urllib.parse import urlsplit

from . import __version__
from .crawler import CrawlConfig, Crawler
from .utils import CATEGORIES

TYPE_ALIASES = {
    "image": "images", "img": "images", "video": "videos", "doc": "documents",
    "docs": "documents", "document": "documents", "css": "styles", "js": "scripts",
    "font": "fonts", "archive": "archives", "zip": "archives", "media": "videos,audio",
}


def parse_types(value: str):
    if value in ("all", "*"):
        return set(CATEGORIES)
    if value in ("none", ""):
        return set()
    out = set()
    for t in value.split(","):
        t = t.strip().lower()
        t = TYPE_ALIASES.get(t, t)
        for x in t.split(","):
            if x not in CATEGORIES:
                raise argparse.ArgumentTypeError(
                    f"unknown type '{x}'. choose from: {', '.join(CATEGORIES)}")
            out.add(x)
    return out


def parse_size(value: str) -> int:
    v = value.strip().upper()
    mult = 1
    for suf, m in (("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3), ("K", 1024),
                   ("M", 1024 ** 2), ("G", 1024 ** 3), ("B", 1)):
        if v.endswith(suf):
            v, mult = v[: -len(suf)], m
            break
    return int(float(v) * mult)


def parse_kv(items, sep):
    out = {}
    for it in items or []:
        if sep not in it:
            raise argparse.ArgumentTypeError(f"expected KEY{sep}VALUE, got {it!r}")
        k, v = it.split(sep, 1)
        out[k.strip()] = v.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="webcrawler",
        description="Download a website: HTML pages (browsable offline), clean text/markdown, "
                    "links, images, videos, audio, documents and more.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("urls", nargs="+", help="start URL(s)")
    p.add_argument("-o", "--output", help="output directory (default: downloads/<host>)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    g = p.add_argument_group("what to crawl")
    g.add_argument("-n", "--max-pages", type=int, default=500, help="max HTML pages (0 = unlimited)")
    g.add_argument("-d", "--max-depth", type=int, default=10, help="max link depth (-1 = unlimited)")
    g.add_argument("--scope", choices=["host", "domain", "prefix", "any"], default="domain",
                   help="host=same host; domain=incl. subdomains; prefix=under start path")
    g.add_argument("--include", action="append", default=[], metavar="REGEX",
                   help="only crawl pages whose URL matches (repeatable)")
    g.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                   help="skip URLs matching (repeatable)")
    g.add_argument("--no-sitemap", action="store_true", help="don't seed from sitemap.xml")
    g.add_argument("--single-page", action="store_true",
                   help="only this page and its files (same as -d 0)")

    g = p.add_argument_group("what to download")
    g.add_argument("-t", "--types", type=parse_types, default=set(CATEGORIES),
                   help=f"file types to save: all, none, or comma list of {','.join(CATEGORIES)}")
    g.add_argument("--max-file-size", type=parse_size, default=parse_size("2GB"),
                   help="skip files bigger than this (e.g. 50MB)")
    g.add_argument("--max-files", type=int, default=0, help="max number of files (0 = unlimited)")
    g.add_argument("--assets-same-domain", action="store_true",
                   help="only download files hosted on the site's domain (ignore CDNs)")
    g.add_argument("--no-streams", action="store_true",
                   help="don't use yt-dlp for YouTube/Bilibili/Vimeo/HLS videos")
    g.add_argument("--video-quality", type=int, metavar="HEIGHT",
                   help="max video height for yt-dlp, e.g. 720")
    g.add_argument("--no-mirror", action="store_true",
                   help="keep the original HTML links (don't rewrite for offline viewing)")
    g.add_argument("--redownload", action="store_true", help="re-download files that already exist")

    g = p.add_argument_group("how to fetch")
    g.add_argument("-w", "--workers", type=int, default=8, help="parallel downloads")
    g.add_argument("--delay", type=float, default=0.2, help="seconds between requests per host")
    g.add_argument("--timeout", type=float, default=30, help="request timeout (s)")
    g.add_argument("--render", action="store_true",
                   help="render pages in headless Chromium (for JavaScript sites; needs playwright)")
    g.add_argument("--render-wait", type=float, default=1.5, help="extra wait after page load (s)")
    g.add_argument("--browser-path", help="path to a Chromium binary for --render")
    g.add_argument("--user-agent", help="custom User-Agent")
    g.add_argument("-H", "--header", action="append", metavar="'Name: value'", help="extra header")
    g.add_argument("--cookie", action="append", metavar="name=value", help="cookie (repeatable)")
    g.add_argument("--cookies-file", help="Netscape cookies.txt (also passed to yt-dlp)")
    g.add_argument("--proxy", help="http(s)/socks proxy URL")
    g.add_argument("--ignore-robots", action="store_true", help="ignore robots.txt")
    g.add_argument("--insecure", action="store_true", help="don't verify TLS certificates")

    g = p.add_argument_group("output")
    g.add_argument("-v", "--verbose", action="count", default=0)
    g.add_argument("-q", "--quiet", action="store_true")
    g.add_argument("--json", action="store_true", help="print the summary as JSON")
    return p


def _load_cookies_file(path: str) -> dict:
    cookies = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
    return cookies


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING if args.quiet else (logging.DEBUG if args.verbose > 1 else logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    urls = [u if "://" in u else "https://" + u for u in args.urls]
    output = args.output or f"downloads/{urlsplit(urls[0]).hostname}"
    cookies = parse_kv(args.cookie, "=")
    if args.cookies_file:
        cookies = {**_load_cookies_file(args.cookies_file), **cookies}

    cfg = CrawlConfig(
        start_urls=urls,
        output_dir=output,
        max_pages=1 if args.single_page else args.max_pages,
        max_depth=0 if args.single_page else args.max_depth,
        scope=args.scope,
        include=args.include,
        exclude=args.exclude,
        types=args.types,
        workers=max(1, args.workers),
        delay=args.delay,
        timeout=args.timeout,
        max_file_size=args.max_file_size,
        max_assets=args.max_files,
        asset_scope="domain" if args.assets_same_domain else "any",
        respect_robots=not args.ignore_robots,
        headers=parse_kv(args.header, ":"),
        cookies=cookies,
        proxy=args.proxy,
        verify_ssl=not args.insecure,
        render=args.render,
        render_wait=args.render_wait,
        browser_path=args.browser_path,
        use_sitemap=not (args.no_sitemap or args.single_page),
        streaming_videos=not args.no_streams,
        video_max_height=args.video_quality,
        cookies_file=args.cookies_file,
        mirror=not args.no_mirror,
        skip_existing=not args.redownload,
    )
    if args.user_agent:
        cfg.user_agent = args.user_agent

    summary = Crawler(cfg).run()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif not args.quiet:
        files = sum(c["count"] for c in summary["assets"].values())
        print("\n✔ done in {elapsed_seconds}s: {pages} pages, {files} files, "
              "{unique_links} unique links, {errors} errors".format(files=files, **summary))
        for cat, c in sorted(summary["assets"].items()):
            print(f"   {cat:<10} {c['count']:>6}  ({c['bytes'] / 1024 / 1024:.1f} MB)")
        if summary["embedded_videos"]:
            print(f"   embedded videos found: {summary['embedded_videos']}")
        print(f"\n→ open {summary['output_dir']}/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
