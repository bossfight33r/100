"""Прямая загрузка медиафайла по HTTP(S)."""

from __future__ import annotations

import shutil
import urllib.parse
import urllib.request
from pathlib import Path

from clipfactory.backends.downloader.base import DownloadError


def download_http(url: str, dest_dir: Path) -> Path:
    """Прямая загрузка медиафайла по HTTP(S) потоком."""
    name = Path(urllib.parse.urlparse(url).path).name or "download.bin"
    dest = dest_dir / name
    req = urllib.request.Request(url, headers={"User-Agent": "clipfactory/0.1"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as f:  # noqa: S310
            shutil.copyfileobj(resp, f, length=1024 * 1024)
    except OSError as e:
        raise DownloadError(f"download failed: {e}", retryable=True) from e
    return dest
