"""Download streaming / embedded videos (YouTube, Bilibili, Vimeo, HLS...) via yt-dlp."""
from __future__ import annotations

import logging
import os
import threading
from typing import Dict, List, Optional

log = logging.getLogger("webcrawler")


def yt_dlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


class VideoDownloader:
    def __init__(self, out_dir: str, max_height: Optional[int] = None,
                 cookies_file: Optional[str] = None, proxy: Optional[str] = None,
                 audio_only: bool = False):
        self.out_dir = out_dir
        self.max_height = max_height
        self.cookies_file = cookies_file
        self.proxy = proxy
        self.audio_only = audio_only
        self._lock = threading.Lock()  # yt-dlp is happiest one-at-a-time

    def _opts(self) -> Dict:
        if self.audio_only:
            fmt = "bestaudio/best"
        elif self.max_height:
            fmt = f"bestvideo[height<={self.max_height}]+bestaudio/best[height<={self.max_height}]/best"
        else:
            fmt = "bestvideo+bestaudio/best"
        opts = {
            "format": fmt,
            "outtmpl": os.path.join(self.out_dir, "%(extractor)s", "%(title).120B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "writeinfojson": True,
            "writesubtitles": True,
            "writeautomaticsub": False,
            "writethumbnail": True,
            "ignoreerrors": False,
            "retries": 3,
            "merge_output_format": "mp4",
        }
        if self.cookies_file:
            opts["cookiefile"] = self.cookies_file
        if self.proxy:
            opts["proxy"] = self.proxy
        return opts

    def download(self, url: str) -> List[str]:
        """Download one video; returns the list of written media files."""
        import yt_dlp

        files: List[str] = []

        def hook(d):
            if d.get("status") == "finished" and d.get("filename"):
                files.append(d["filename"])

        opts = self._opts()
        opts["progress_hooks"] = [hook]
        with self._lock:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    final = ydl.prepare_filename(info)
                    base = os.path.splitext(final)[0]
                    for ext in (".mp4", ".mkv", ".webm", ".m4a", ".mp3"):
                        if os.path.exists(base + ext) and base + ext not in files:
                            files.insert(0, base + ext)
        # keep only files that still exist (format fragments get merged & deleted)
        return [f for f in dict.fromkeys(files) if os.path.exists(f)]
