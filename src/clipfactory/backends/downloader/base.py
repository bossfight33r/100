from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


class DownloadError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


Downloader = Callable[[str, Path], Path]
"""(url, каталог назначения) -> путь к скачанному файлу. Ошибки — DownloadError."""
