"""Маршрутизация задач на воркеры по тегам возможностей.

Задача с ``requirements.tags`` идёт в очередь ``clipfactory@tag1+tag2`` (теги
отсортированы). Воркер слушает базовую очередь и очереди всех подмножеств своих
тегов, поэтому GPU-задачу возьмёт только воркер с тегом ``gpu``. Живые воркеры
публикуют heartbeat в Redis — по нему видно, есть ли кому выполнить задачу.
"""

from __future__ import annotations

import itertools
import json
import socket
import threading
from typing import Any

from clipfactory.compute.capabilities import TaskRequirements, WorkerCapabilities, satisfies

BASE_QUEUE = "clipfactory"
HEARTBEAT_PREFIX = "cf:worker:"
REGISTRY_KEY = "cf:workers"  # множество имён: без SCAN по всему Redis
HEARTBEAT_TTL = 60
MAX_TAGS = 6  # 2^6 очередей на воркер — с запасом


def derive_tags(caps: WorkerCapabilities, extra: list[str] | None = None) -> list[str]:
    """Функциональные теги машины: что она реально умеет."""
    tags = set(extra or [])
    if "h264_nvenc" in caps.working_encoders:
        tags.add("gpu")
    if "h264_videotoolbox" in caps.working_encoders:
        tags.add("mac")
    if "mlx" in caps.available_transcribers:
        tags.add("mlx")
    return sorted(tags)


def queue_for(req: TaskRequirements, base: str = BASE_QUEUE) -> str:
    tags = sorted(set(req.tags))
    return f"{base}@{'+'.join(tags)}" if tags else base


def queues_for_worker(tags: list[str], base: str = BASE_QUEUE) -> list[str]:
    """Сначала самые специфичные очереди: воркер с GPU в первую очередь берёт GPU-задачи."""
    tags = sorted(set(tags))[:MAX_TAGS]
    names = []
    for size in range(len(tags), 0, -1):
        for combo in itertools.combinations(tags, size):
            names.append(f"{base}@{'+'.join(combo)}")
    names.append(base)
    return names


def publish_heartbeat(conn: Any, name: str, caps: WorkerCapabilities) -> None:
    conn.set(HEARTBEAT_PREFIX + name, caps.model_dump_json(), ex=HEARTBEAT_TTL)
    conn.sadd(REGISTRY_KEY, name)


def live_workers(conn: Any) -> dict[str, WorkerCapabilities]:
    out: dict[str, WorkerCapabilities] = {}
    names = sorted(m.decode() if isinstance(m, bytes) else m for m in conn.smembers(REGISTRY_KEY))
    if not names:
        return out
    # один MGET вместо GET на каждого воркера
    for name, raw in zip(names, conn.mget([HEARTBEAT_PREFIX + n for n in names]), strict=True):
        if raw is None:  # heartbeat истёк — воркер мёртв, чистим реестр
            conn.srem(REGISTRY_KEY, name)
            continue
        try:
            out[name] = WorkerCapabilities.model_validate(json.loads(raw))
        except ValueError:
            continue
    return out


def eligible_workers(req: TaskRequirements, workers: dict[str, WorkerCapabilities]) -> list[str]:
    return sorted(name for name, caps in workers.items() if not satisfies(caps, req))


def worker_name() -> str:
    return f"{socket.gethostname()}-{threading.get_native_id()}"


class Heartbeat:
    """Фоновый heartbeat воркера (TTL 60 с, обновление каждые 20 с)."""

    def __init__(self, conn: Any, name: str, caps: WorkerCapabilities) -> None:
        self.conn, self.name, self.caps = conn, name, caps
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="cf-heartbeat", daemon=True)

    def _loop(self) -> None:
        while True:
            try:
                publish_heartbeat(self.conn, self.name, self.caps)
            except Exception:  # noqa: S110 — Redis моргнул; следующий тик повторит
                pass
            if self._stop.wait(HEARTBEAT_TTL / 3):
                return

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        try:
            self.conn.delete(HEARTBEAT_PREFIX + self.name)
            self.conn.srem(REGISTRY_KEY, self.name)
        except Exception:  # noqa: S110
            pass
