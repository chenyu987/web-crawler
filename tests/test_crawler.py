import csv
import functools
import http.server
import json
import os
import threading

import pytest

from webcrawler import CrawlConfig, Crawler
from webcrawler.extractor import extract
from webcrawler.utils import in_scope, normalize_url, url_to_relpath

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
       b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
       b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")

SITE = {
    "index.html": """<!doctype html><html lang="en"><head><title>Home</title>
        <meta name="description" content="Test site">
        <meta property="og:image" content="/img/og.png">
        <link rel="stylesheet" href="/css/style.css"></head><body><main>
        <h1>Welcome</h1><p>Hello <b>world</b>, this is the home page with enough text to count as
        main content for the extractor. Lorem ipsum dolor sit amet, consectetur adipiscing elit,
        sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.</p>
        <ul><li><a href="/about.html">About</a></li><li><a href="/collection.html">Collection</a></li><li><a href="sub/">Sub</a></li>
        <li><a href="/files/report.pdf">Report</a></li>
        <li><a href="https://external.example.org/x">External</a></li></ul>
        <img src="/img/a.png" srcset="/img/a.png 1x, /img/b.png 2x" alt="A">
        <video src="/media/clip.mp4" poster="/img/poster.png"></video>
        <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>
        <a href="/missing.html">broken</a>
        </main></body></html>""",
    "about.html": """<html><head><title>About</title></head><body><p>About us.</p>
        <a href="/index.html">home</a><audio src="/media/sound.mp3"></audio></body></html>""",
    "sub/index.html": """<html><head><title>Sub</title></head><body>
        <a href="deeper.html">deeper</a><a href="../files/data.csv">data</a></body></html>""",
    "sub/deeper.html": "<html><head><title>Deeper</title></head><body><p>deep</p></body></html>",
    "css/style.css": "body{background:url('../img/bg.png')} @font-face{src:url(/fonts/f.woff2)}",
    "img/a.png": PNG, "img/b.png": PNG, "img/og.png": PNG, "img/poster.png": PNG, "img/bg.png": PNG,
    "fonts/f.woff2": b"wOF2fake",
    "media/clip.mp4": b"\x00\x00\x00\x18ftypmp42" + b"0" * 1000,
    "media/sound.mp3": b"ID3" + b"0" * 500,
    "files/report.pdf": b"%PDF-1.4 fake",
    "files/data.csv": "a,b\n1,2\n",
    "collection.html": """<html><head><title>Collection</title></head><body><main>
        <div class="card"><img src="/img/a.png" srcset="/img/a.png 400w, /img/b.png 1200w" alt="Water Lilies">
          <h3>Water Lilies, 1906</h3><p>Claude Monet. Oil on canvas. Part of the famous series.</p></div>
        <figure><img data-src="/img/og.png" alt="Bust"><figcaption>Marble bust, Roman, 2nd century</figcaption></figure>
        </main></body></html>""",
    "robots.txt": "User-agent: *\nDisallow:\n",
}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    for rel, content in SITE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_normalize_url():
    assert normalize_url("../a?utm_source=x&id=1#frag", "https://Ex.com:443/b/c") == "https://ex.com/a?id=1"
    assert normalize_url("mailto:x@y.z") is None
    assert normalize_url("javascript:void(0)") is None


def test_scope():
    s = "https://www.example.com/docs/intro"
    assert in_scope("https://blog.example.com/x", s, "domain")
    assert not in_scope("https://blog.example.com/x", s, "host")
    assert in_scope("https://www.example.com/docs/a", s, "prefix")
    assert not in_scope("https://www.example.com/blog", s, "prefix")


def test_url_to_relpath():
    assert url_to_relpath("https://ex.com/", page=True) == "ex.com/index.html"
    assert url_to_relpath("https://ex.com/a/b", page=True) == "ex.com/a/b.html"
    assert url_to_relpath("https://ex.com/a.php?id=2", page=True).startswith("ex.com/a.php_")
    assert url_to_relpath("https://ex.com/i/x.png", page=False) == "ex.com/i/x.png"


def test_extract_finds_everything():
    d = extract(SITE["index.html"], "http://h/")
    urls = {a.url for a in d.assets}
    assert "http://h/img/b.png" in urls and "http://h/media/clip.mp4" in urls
    assert "http://h/files/report.pdf" in urls and "http://h/img/og.png" in urls
    assert d.embeds == ["https://www.youtube.com/embed/dQw4w9WgXcQ"]
    assert "http://h/about.html" in d.page_links
    assert d.title == "Home" and d.description == "Test site"
    assert "# Welcome" in d.markdown and "**world**" in d.markdown


def test_full_crawl(site, tmp_path):
    out = tmp_path / "out"
    cfg = CrawlConfig(start_urls=[site + "/"], output_dir=str(out), delay=0,
                      streaming_videos=False, workers=4)
    summary = Crawler(cfg).run()

    assert summary["pages"] == 5  # index, about, collection, sub/, sub/deeper
    cats = summary["assets"]
    assert cats["images"]["count"] == 5
    assert cats["videos"]["count"] == 1 and cats["audio"]["count"] == 1
    assert cats["documents"]["count"] == 2  # pdf + csv
    assert cats["styles"]["count"] == 1 and cats["fonts"]["count"] == 1  # font found via CSS
    assert summary["embedded_videos"] == 1
    assert summary["errors"] == 1  # missing.html -> 404

    host = site.split("//")[1].replace(":", "_")
    html = (out / "pages" / host / "index.html").read_text()
    # links rewritten for offline browsing
    assert 'href="about.html"' in html
    about = (out / "pages" / host / "about.html").read_text()
    assert 'href="index.html"' in about
    assert "../../assets/images/" in html
    assert "../../assets/videos/" in html
    assert 'href="https://external.example.org/x"' in html
    css = next((out / "assets" / "styles").rglob("style.css")).read_text()
    assert "../../images/" in css and "fonts/" in css

    md = (out / "text" / host / "index.md").read_text()
    assert "Welcome" in md
    pages = [json.loads(l) for l in (out / "data" / "pages.jsonl").read_text().splitlines()]
    assert {p["title"] for p in pages} == {"Home", "About", "Collection", "Sub", "Deeper"}
    with open(out / "data" / "links.csv", encoding="utf-8-sig") as fh:
        links = list(csv.DictReader(fh))
    assert any(l["url"] == "https://external.example.org/x" and l["internal"] == "False" for l in links)
    assert (out / "index.html").exists() and (out / "manifest.json").exists()


def test_type_filter_and_depth(site, tmp_path):
    out = tmp_path / "out2"
    cfg = CrawlConfig(start_urls=[site + "/"], output_dir=str(out), delay=0, max_depth=0,
                      types={"documents"}, streaming_videos=False)
    summary = Crawler(cfg).run()
    assert summary["pages"] == 1
    assert set(summary["assets"]) == {"documents"}
    assert not (out / "assets" / "images").exists()


def test_image_descriptions(site, tmp_path):
    out = tmp_path / "out3"
    cfg = CrawlConfig(start_urls=[site + "/collection.html"], output_dir=str(out), delay=0,
                      max_depth=0, streaming_videos=False)
    Crawler(cfg).run()
    with open(out / "data" / "images.csv", encoding="utf-8-sig") as fh:
        rows = {r["alt"]: r for r in csv.DictReader(fh)}
    lilies = rows["Water Lilies"]
    assert lilies["url"].endswith("/img/b.png")  # largest srcset candidate
    assert lilies["heading"] == "Water Lilies, 1906"
    assert "Oil on canvas" in lilies["context"]
    assert lilies["local_path"].endswith("b.png") and (out / lilies["local_path"]).exists()
    assert rows["Bust"]["caption"] == "Marble bust, Roman, 2nd century"
    md = next((out / "text").rglob("collection.md")).read_text()
    assert "../../assets/images/" in md  # markdown points at the local image files
