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

### Игры и стримы (CS2 и т.п.)

Моменты ищутся без Whisper: по всплескам звука и кривой YouTube «Most replayed» (ADR-0014).

```bash
.venv/bin/cf run "https://youtu.be/..." --campaign cs2   # config/campaigns/cs2.yaml
```

`selection: signals` + `transcribe: false` в кампании. Если стример говорит и нужны субтитры — `transcribe: true`.

## Команды `cf`

| Команда | Что делает |
|---|---|
| `cf capabilities` | ffmpeg, ffprobe, энкодеры, платформа, доступные транскриберы |
| `cf run SOURCE --campaign ID` | синхронно выполнить весь pipeline (файл или URL) |
| `cf status JOB_ID` | статус job, упавший этап, этапы (cache/completed), клипы |
| `cf enqueue SOURCE --campaign ID` | создать job и поставить в очередь (`CF_QUEUE=rq`) |
| `cf retry JOB_ID [--force-stage STAGE]` | повторить с первого невалидного этапа |
| `cf cancel JOB_ID` | снять из очереди или остановить идущую job (продолжить потом — `cf retry`) |
| `cf worker [--burst]` | RQ-воркер; при старте восстанавливает потерянные job из SQLite |
| `cf auth youtube --account ID` | OAuth своего YouTube-канала, токен в `data/secrets/` (600) |
| `cf publish JOB_ID [--schedule-only] [--retry-failed]` | распределить одобренные клипы по слотам и загрузить/экспортировать; `--retry-failed` — повторить упавшие |
| `cf track [--every 6h]` | собрать статистику YouTube (append-only снимки); `--every` — периодически |
| `cf track --manual PUB_ID --views N [--likes --comments]` | ручной ввод для TikTok/Instagram |
| `cf report [--campaign ID] [--recommendations]` | доход и статистика по кампаниям, аккаунтам, клипам, хукам; файл рекомендаций к промпту |
| `cf serve [--port]` | локальный HTTP-API для десктоп-приложения (`CF_API_TOKEN`, только 127.0.0.1); схема — `docs/openapi.json`, ТЗ приложения — `docs/gui-spec.md` |
| `cf bot` | Telegram-бот ревью (нужны `TELEGRAM_BOT_TOKEN`, `CF_ADMIN_IDS`) |
| `cf review JOB_ID CLIP_ID approve\|reject\|edit\|captions\|crop` | ревью из терминала: `edit --title --description --hashtags [--platform]`, `crop --center 0–100\|auto`, `reject --reason` |

Флаги `run`: `--force-stage STAGE` (перезапустить этап и всё после), `--no-cache`.

Глобальные флаги: `--verbose`, `--json`. Бот: `/jobs`, `/status`, `/publish`, `/stats`, `/cancel` + кнопки ревью.

## Документация

- [docs/STATUS.md](docs/STATUS.md) — состояние проекта, блокеры, «Проверить на Маке»
- [docs/architecture.md](docs/architecture.md) — слои и поток данных
- [docs/pipeline.md](docs/pipeline.md) — этапы, артефакты, кеш
- [docs/runbook.md](docs/runbook.md) — эксплуатация и частые ошибки
- [docs/campaigns.md](docs/campaigns.md) — кампании и аккаунты
- [docs/decisions/](docs/decisions/) — ADR

## Перенос на сервер

Готово (ADR-0011):
- **S3/MinIO** вместо локальной ФС: `CF_STORAGE=s3 CF_S3_BUCKET=... [CF_S3_ENDPOINT_URL=http://minio:9000]`, `uv pip install -e '.[s3]'`, ключи — стандартные `AWS_*`.
- **NVENC** на GPU-сервере: выбирается автоматически, если реально кодирует (`cf capabilities` → «H.264 рабочие»).
- **Ollama** вместо Anthropic: `CF_LLM_PROVIDER=ollama CF_OLLAMA_MODEL=qwen2.5:7b-instruct`.
- **Маршрутизация на воркеры** (ADR-0012): `CF_JOB_REQUIRE_TAGS=gpu` при постановке — задачу возьмёт только воркер с тегом `gpu` (NVENC реально кодирует или `CF_WORKER_TAGS=gpu`).

- **PostgreSQL** вместо SQLite (ADR-0013): `CF_DB_URL=postgresql://user:pass@host/clipfactory`, `uv pip install -e '.[postgres]'`. Схема и миграции создаются сами.
