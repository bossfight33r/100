from __future__ import annotations

import json
import re
from collections.abc import Callable


class FakeLLM:
    """Детерминированная LLM для тестов.

    Понимает маркеры, которые вставляют наши промпты:
    ``TASK: highlights`` + ``CHUNK_RANGE: a-b`` и ``TASK: metadata`` + ``PLATFORM: x``.
    Можно передать ``responder`` для произвольных сценариев.
    """

    name = "fake"
    model = "fake-llm"

    def __init__(
        self,
        responder: Callable[[str, str], str] | None = None,
        clip_len: float = 25.0,
        per_chunk: int = 3,
    ) -> None:
        self.responder = responder
        self.clip_len = clip_len
        self.per_chunk = per_chunk
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str, max_tokens: int = 4096) -> str:
        self.calls.append((system, prompt))
        if self.responder is not None:
            return self.responder(system, prompt)
        full = system + "\n" + prompt
        if "TASK: highlights" in full:
            return self._highlights(full)
        if "TASK: metadata" in full:
            return self._metadata(full)
        return "{}"

    def _highlights(self, text: str) -> str:
        m = re.search(r"CHUNK_RANGE:\s*([0-9.]+)-([0-9.]+)", text)
        a, b = (float(m.group(1)), float(m.group(2))) if m else (0.0, 60.0)
        span = b - a
        items = []
        for i in range(self.per_chunk):
            start = a + 1.0 + i * max(span / self.per_chunk, 1.0)
            end = min(b, start + self.clip_len)
            if end - start < 3:
                continue
            items.append(
                {
                    "title": f"Момент {i + 1}",
                    "start_time": round(start, 2),
                    "end_time": round(end, 2),
                    "score": 90 - i * 10,
                    "hook_sentence": "Никто не говорит об этом",
                    "virality_reason": "сильный хук и законченная мысль",
                }
            )
        return "```json\n" + json.dumps({"highlights": items}, ensure_ascii=False) + "\n```"

    def _metadata(self, text: str) -> str:
        m = re.search(r"PLATFORM:\s*(\w+)", text)
        platform = m.group(1) if m else "youtube"
        return json.dumps(
            {
                "title": f"Секрет, о котором молчат ({platform})",
                "description": "Короткий фрагмент с главной мыслью выпуска.",
                "hashtags": ["#советы", "#подкаст"],
            },
            ensure_ascii=False,
        )
