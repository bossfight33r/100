"""Export-пакет для платформ без доступного API публикации (TikTok, Instagram).

Пакет: video.mp4, thumb.jpg, caption.txt, meta.json — загружается вручную в слот.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

from clipfactory.publish.base import PublishRequest, PublishResult, caption_text
from clipfactory.schemas import Platform, PublicationStatus


class ExportPublisher:
    def __init__(self, platform: Platform, exports_dir: Path) -> None:
        self.platform = platform
        self.exports_dir = Path(exports_dir)

    def package_dir(self, req: PublishRequest) -> Path:
        when = (
            req.scheduled_at.astimezone(ZoneInfo(req.account.timezone)).strftime("%Y-%m-%d_%H%M")
            if req.scheduled_at
            else "unscheduled"
        )
        return (
            self.exports_dir / self.platform.value / req.account.id / f"{when}_{req.publication_id}"
        )

    def publish(self, req: PublishRequest) -> PublishResult:
        target = self.package_dir(req)
        target.mkdir(parents=True, exist_ok=True)
        video = target / "video.mp4"
        if not video.exists():
            try:
                os.link(req.video_path, video)  # без копии гигабайтов, если та же ФС
            except OSError:
                shutil.copyfile(req.video_path, video)
        if req.thumb_path and req.thumb_path.exists():
            shutil.copyfile(req.thumb_path, target / "thumb.jpg")
        (target / "caption.txt").write_text(caption_text(req.meta) + "\n", encoding="utf-8")
        (target / "meta.json").write_text(
            json.dumps(
                {
                    "publication_id": req.publication_id,
                    "platform": self.platform.value,
                    "account_id": req.account.id,
                    "account_name": req.account.name,
                    "scheduled_at_utc": req.scheduled_at.isoformat() if req.scheduled_at else None,
                    "timezone": req.account.timezone,
                    **req.meta.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return PublishResult(status=PublicationStatus.exported, url=str(target))
