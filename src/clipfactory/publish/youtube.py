"""YouTube Data API v3: OAuth для собственного канала и resumable upload.

Токен хранится в data/secrets/{token_ref}.json (chmod 600), в логи не попадает.
Отложенная публикация: privacyStatus=private + publishAt — публикует сам YouTube.
"""

from __future__ import annotations

import json
import os
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipfactory.log import get_logger
from clipfactory.publish.base import PublishError, PublishRequest, PublishResult
from clipfactory.schemas import Account, Platform, PublicationStatus

log = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
CHUNK_SIZE = 8 * 1024 * 1024
MAX_RETRIES = 6
RETRYABLE_STATUS = {500, 502, 503, 504}
TITLE_MAX = 100
DESCRIPTION_MAX = 5000


def token_path(secrets_dir: Path, account: Account) -> Path:
    if not account.token_ref:
        raise PublishError(f"account {account.id} has no token_ref")
    return Path(secrets_dir) / f"{account.token_ref}.json"


def authorize(
    account: Account, client_secrets: Path, secrets_dir: Path
) -> Path:  # pragma: no cover
    """Интерактивный OAuth (браузер на localhost). Сохраняет токен с правами 600."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if account.platform != Platform.youtube:
        raise PublishError(f"account {account.id} is not a YouTube account")
    if not Path(client_secrets).is_file():
        raise PublishError(
            f"OAuth client secrets not found at {client_secrets}; see docs/runbook.md#youtube"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    path = token_path(secrets_dir, account)
    save_token(path, creds.to_json())
    return path


def save_token(path: Path, token_json: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token_json)
    os.chmod(path, 0o600)


def load_credentials(account: Account, secrets_dir: Path) -> Any:  # pragma: no cover - google
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(secrets_dir, account)
    if not path.exists():
        raise PublishError(
            f"no token for {account.id}; run `cf auth youtube --account {account.id}`"
        )
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_token(path, creds.to_json())
        else:
            raise PublishError(f"token for {account.id} is invalid; re-run `cf auth youtube`")
    return creds


def build_service(account: Account, secrets_dir: Path) -> Any:  # pragma: no cover - google
    from googleapiclient.discovery import build

    return build(
        "youtube", "v3", credentials=load_credentials(account, secrets_dir), cache_discovery=False
    )


def video_body(req: PublishRequest, now: datetime) -> dict[str, Any]:
    meta = req.meta
    tags = [t.lstrip("#") for t in meta.hashtags if t.lstrip("#")]
    description = "\n\n".join(p for p in (meta.description, " ".join(meta.hashtags)) if p)
    status: dict[str, Any] = {"selfDeclaredMadeForKids": False}
    if req.scheduled_at and req.scheduled_at > now:
        status["privacyStatus"] = "private"
        status["publishAt"] = req.scheduled_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        status["privacyStatus"] = "public"
    return {
        "snippet": {
            "title": meta.title[:TITLE_MAX],
            "description": description[:DESCRIPTION_MAX],
            "tags": tags[:30],
            "categoryId": "22",
        },
        "status": status,
    }


def _http_status(exc: Exception) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _reason(exc: Exception) -> str:
    content = getattr(exc, "content", b"") or b""
    try:
        data = json.loads(content.decode("utf-8", errors="replace"))
        return data["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return ""


class YouTubePublisher:
    platform = Platform.youtube

    def __init__(
        self,
        service_factory: Callable[[Account], Any],
        *,
        media_factory: Callable[[Path], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.service_factory = service_factory
        self.media_factory = media_factory or self._media
        self.sleep = sleep
        self.clock = clock

    @staticmethod
    def _media(path: Path) -> Any:  # pragma: no cover - google
        from googleapiclient.http import MediaFileUpload

        return MediaFileUpload(
            str(path), mimetype="video/mp4", chunksize=CHUNK_SIZE, resumable=True
        )

    def publish(self, req: PublishRequest) -> PublishResult:
        now = self.clock()
        service = self.service_factory(req.account)
        request = service.videos().insert(
            part="snippet,status",
            body=video_body(req, now),
            media_body=self.media_factory(req.video_path),
        )
        response = None
        retries = 0
        while response is None:
            try:
                _status, response = request.next_chunk()
            except Exception as e:  # HttpError / сетевые ошибки
                code = _http_status(e)
                reason = _reason(e)
                if reason in ("quotaExceeded", "uploadLimitExceeded"):
                    raise PublishError(f"YouTube quota exceeded ({reason})", retryable=True) from e
                if code in (401, 403):
                    raise PublishError(f"YouTube auth/permission error {code} {reason}") from e
                transient = code in RETRYABLE_STATUS or code is None
                if not transient or retries >= MAX_RETRIES:
                    raise PublishError(
                        f"YouTube upload failed ({code} {reason})", retryable=transient
                    ) from e
                retries += 1
                delay = min(60.0, 2**retries) + random.random()  # noqa: S311
                log.warning(
                    "youtube.retry", publication_id=req.publication_id, code=code, retry=retries
                )
                self.sleep(delay)
        video_id = response.get("id")
        if not video_id:
            raise PublishError("YouTube response has no video id")
        scheduled = req.scheduled_at is not None and req.scheduled_at > now
        return PublishResult(
            status=PublicationStatus.scheduled if scheduled else PublicationStatus.published,
            external_id=video_id,
            url=f"https://youtube.com/shorts/{video_id}",
        )


def fetch_stats(service: Any, video_ids: list[str]) -> dict[str, dict[str, int]]:
    """views/likes/comments по id видео, батчами по 50 (videos.list стоит 1 единицу квоты)."""
    out: dict[str, dict[str, int]] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = service.videos().list(part="statistics", id=",".join(batch), maxResults=50).execute()
        for item in resp.get("items", []):
            st = item.get("statistics", {})
            out[item["id"]] = {
                "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)),
            }
    return out
