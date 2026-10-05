# Архитектура

Local-first монолит с жёсткими границами. Три независимые оси: **оркестрация** (pipeline, очередь) ≠ **вычисления** (backends: whisper, LLM, лица, энкодер) ≠ **хранение** (ObjectStorage + SQLite).

## Слои

| Слой | Модули | Правило |
|---|---|---|
| Интерфейсы | `cli.py`, `bot/` | тонкие, вызывают сервисы, не трогают ffmpeg и SQL |
| Сервисы | `services.py`, `worker.py` | сборка зависимостей, создание job, ревью, публикация |
| Pipeline | `pipeline/*` | этапы `run(ctx) -> StageResult`, только через контракты `schemas.py` |
| Бэкенды | `backends/*`, `publish/*` | внешний мир за `Protocol`; в тестах — fake |
| Медиа | `media/*` | единственное место вызова ffmpeg/ffprobe |
| Хранение | `storage/*`, `db.py` | артефакты по ключам; метаданные в SQLite |
| Очередь | `queue/*` | Inline (dev/test), RQ (prod); этапы не знают про rq |

## Поток данных

```mermaid
flowchart LR
    SRC[файл / URL] --> ING[ingest<br/>source.mp4]
    ING --> TR[transcribe<br/>audio.wav, transcript.json]
    TR --> SEL[select<br/>highlights.json]
    SEL --> RF[reframe<br/>reframe.json]
    SEL --> CAP[captions<br/>captions.ass]
    TR --> CAP
    RF --> REN[render<br/>final.mp4, thumb.jpg, meta.json]
    CAP --> REN
    REN --> REV[review<br/>DB: clips, review_actions]
    REV --> PUB[publish<br/>DB: publications]
    PUB --> TRK[track<br/>DB: stats_snapshots]
    TRK --> ERN[earnings / report]
```

## Ключевые контракты

- `ObjectStorage` (`storage/base.py`), `Queue` (`queue/base.py`), `Transcriber`, `LLMProvider`, `FaceDetector`, `EncoderBackend` (`backends/*/base.py`) — `typing.Protocol`.
- `WorkerCapabilities` / `TaskRequirements` (`compute/capabilities.py`) — задачи описывают требования; сегодня всё выполняется локально.
- Все данные между этапами — модели `schemas.py`, сериализуются в JSON-артефакты.
