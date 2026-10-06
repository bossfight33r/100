"""Кеш на уровне клипа внутри reframe/captions/render.

Этап хранит рядом с выходами клипа ``clips/{id}/.{stage}.cache.json``:
отпечаток входов клипа + sha256 выходов. Если отпечаток совпал и выходы целы —
клип не пересчитывается. Так правка одного клипа не перерендеривает остальные.
Сайдкар принадлежит этапу (как и его выходы); ``--no-cache`` его игнорирует.
"""

from __future__ import annotations

import json
from typing import Any

from clipfactory.pipeline.context import StageContext, config_hash, stable_json
from clipfactory.storage.base import StorageError


def cache_key(ctx: StageContext, stage: str, clip_id: str) -> str:
    return ctx.clip_key(clip_id, f".{stage}.cache.json")


def fingerprint(**parts: Any) -> str:
    return config_hash(parts)


def reusable(ctx: StageContext, stage: str, clip_id: str, fp: str) -> list[str] | None:
    """Ключи выходов, если клип можно взять из кеша, иначе None."""
    if ctx.no_clip_cache:
        return None
    key = cache_key(ctx, stage, clip_id)
    try:
        if not ctx.storage.exists(key):
            return None
        with ctx.storage.open_read(key) as f:
            data = json.loads(f.read())
        if data.get("fingerprint") != fp:
            return None
        outputs: dict[str, str] = data["outputs"]
        for out_key, digest in outputs.items():
            if not ctx.storage.exists(out_key) or ctx.storage.checksum(out_key) != digest:
                return None
    except (StorageError, ValueError, KeyError, OSError):
        return None
    return list(outputs)


def record(ctx: StageContext, stage: str, clip_id: str, fp: str, outputs: list[str]) -> None:
    data = {"fingerprint": fp, "outputs": {k: ctx.storage.checksum(k) for k in outputs}}
    ctx.storage.put_bytes(cache_key(ctx, stage, clip_id), stable_json(data))
