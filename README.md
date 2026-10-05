# ClipFactory

Длинные видео → вертикальные клипы 9:16 с субтитрами → ревью → публикация на **свои** аккаунты → статистика → доход по клиппинг-кампаниям. Local-first: всё работает на одном Mac M1, тяжёлые этапы позже можно вынести на сервер без переписывания pipeline.

Статус фаз и что проверить на Маке — в [docs/STATUS.md](docs/STATUS.md).

## Установка на Mac M1

```bash
brew install uv ffmpeg redis       # если ещё нет
make setup-mac                     # venv на Python 3.12, зависимости + mlx-whisper, модель лиц
cp .env.example .env               # заполнить ANTHROPIC_API_KEY и т.д.
cp config/accounts.example.yaml config/accounts.yaml
.venv/bin/cf capabilities          # должно показать h264_videotoolbox и mlx
```

Linux или Intel: `make setup` (faster-whisper CPU int8, libx264).

## Быстрый старт за 5 минут

```bash
# 1. Проверить окружение
.venv/bin/cf capabilities

# 2. Прогон без внешних API: фейковые транскрибер, LLM и детектор лиц
CF_TRANSCRIBER=fake CF_LLM_PROVIDER=fake CF_FACE_DETECTOR=fake \
  .venv/bin/cf run path/to/video.mp4 --campaign example

# 3. Настоящий прогон (mlx-whisper + Claude + MediaPipe)
.venv/bin/cf run path/to/video.mp4 --campaign example
.venv/bin/cf status <JOB_ID>
```

Результат лежит в `data/jobs/<JOB_ID>/clips/<clip_id>/final.mp4`.

## Команды `cf`

| Команда | Что делает |
|---|---|
| `cf capabilities` | ffmpeg, ffprobe, энкодеры, платформа, доступные транскриберы |
| `cf run SOURCE --campaign ID` | синхронно выполнить весь pipeline (файл или URL) |
| `cf status JOB_ID` | статус job, упавший этап, клипы |

Глобальные флаги: `--verbose`, `--json`. Полный список команд пополняется по фазам (см. docs/STATUS.md).

## Документация

- [docs/STATUS.md](docs/STATUS.md) — состояние проекта, блокеры, «Проверить на Маке»
- [docs/architecture.md](docs/architecture.md) — слои и поток данных
- [docs/pipeline.md](docs/pipeline.md) — этапы, артефакты, кеш
- [docs/runbook.md](docs/runbook.md) — эксплуатация и частые ошибки
- [docs/campaigns.md](docs/campaigns.md) — кампании и аккаунты
- [docs/decisions/](docs/decisions/) — ADR

## Дальше

Не реализовано намеренно (только точки расширения через Protocol):
- `storage/s3.py` — S3/MinIO-реализация `ObjectStorage` для выноса артефактов с машины.
- `backends/encoder/nvenc.py` — NVENC-энкодер для GPU-сервера.
- `backends/llm/ollama.py` — локальная LLM вместо Anthropic.
- `compute/routing.py` — маршрутизация задач по `TaskRequirements`/`WorkerCapabilities` на удалённые воркеры.
- PostgreSQL вместо SQLite (весь SQL изолирован в `db.py`).
