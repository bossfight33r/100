# STATUS — передача проекта

Новая сессия: прочитай `CLAUDE.md` и этот файл, затем продолжай с «Следующий шаг».

## Фазы

| Фаза | Статус |
|---|---|
| 0 Foundation | ✅ готово |
| 1 Local pipeline | ✅ готово |
| 2 Durable orchestration | ✅ готово |
| 3 Review + Telegram | ✅ готово |
| 4 Publishing | ✅ готово |
| 5 Tracking + earnings | ✅ готово |

Ветка: `claude/laughing-carson-ti2qk0`. `make lint` и `make test` зелёные (без сети, GPU, Redis, реальных API).

## Что работает

- **CLI**: `capabilities`, `run`, `status`, `enqueue`, `retry`, `cancel`, `worker`, `review` (approve/reject/edit/captions/crop), `auth youtube`, `publish` (`--retry-failed`), `track` (`--every`), `report`, `bot`; флаги `--json`, `--verbose`, `--force-stage`, `--no-cache`.
- **Pipeline**: ingest (файл/HTTP/yt-dlp, remux без перекодирования) → transcribe (mlx/faster-whisper, пословно) → select (Claude, чанки 20 мин/60 с, подгонка по словам и паузам) → reframe (MediaPipe + смены сцен, гистерезис, кусочно-постоянный кроп) → captions (ASS, подсветка слова, safe zone) → render (один ffmpeg, 1080x1920, loudnorm, H.264/AAC, валидация ffprobe) → meta.json по платформам.
- **Надёжность**: манифесты и кеш по хешам, кеш на уровне клипа (правка одного клипа не перекодирует остальные), resume/retry с первого невалидного этапа, отмена идущей job, stage_runs, миграции схемы БД, RQ + SimpleWorker, защита от дублей задач, восстановление job из SQLite после падения воркера/потери Redis.
- **Ревью**: approve/reject/edit metadata/rerender captions/crop, журнал review_actions; метаданные переиспользуются при перерендере.
- **Бот**: ADMIN_IDS, файл/URL/путь, выбор кампании, прогресс одним сообщением, карточки клипов, кнопки ревью, retry, `/jobs`, `/status`, `/publish`, `/stats`, `/cancel`; локальный Bot API server (`CF_TELEGRAM_API_URL`) для файлов до 2 ГБ.
- **Публикация**: scheduler (окна, timezone, daily_limit, DST), YouTube resumable upload + publishAt, перенос прошедшего слота, export-пакеты TikTok/Instagram, rejected не публикуются; прерванная загрузка не повторяется автоматически (лиза 30 мин, затем `--retry-failed` после проверки канала).
- **Статистика/доход**: YouTube collector, ручной ввод, append-only снимки, earnings-стратегия, отчёты, аналитика хуков, файл рекомендаций к промпту.
- **Перенос на сервер**: S3/MinIO, NVENC (только если реально кодирует), Ollama, маршрутизация задач по тегам воркеров, PostgreSQL с пулом — ADR-0011…0013, см. README «Перенос на сервер» и runbook «Несколько машин».
- Три прохода независимого code review: 30 находок, все закрыты; security review — HIGH/MEDIUM нет (подробно — CHANGELOG).
- Адаптеры faster-whisper, mlx, Anthropic, YouTube протестированы на настоящих типах/исключениях библиотек.
- Проверено в Linux-контейнере: реальный ffmpeg-рендер, реальный MediaPipe (нужны `libegl1 libgles2`), реальный redis-server + `cf worker`.

## Что не проверено / ограничения

- mlx-whisper, h264_videotoolbox — нет Мака в окружении. Реальное распознавание faster-whisper не запускалось: HuggingFace и CDN OpenAI закрыты политикой прокси этого окружения, весов нет на PyPI. Адаптер проверен на настоящих классах `faster_whisper`.
- Anthropic API, Telegram, YouTube OAuth/загрузка/статистика — реальные вызовы запрещены в этом окружении; покрыты фейками (SDK-вызовы написаны по документации SDK `anthropic` 1.x).
- Облачный Bot API скачивает файлы только до 20 МБ — для больших видео: путь, ссылка или локальный Bot API server.
- TikTok/Instagram — только export-пакеты (ручная заливка).
- Загрузка дольше 30 мин в другом процессе может быть ошибочно признана прерванной при параллельном `cf publish` (для shorts нереалистично; см. `PUBLISHING_LEASE`).
- Отмена не прерывает вызов Whisper/LLM посередине — срабатывает сразу после него.

## Десктоп-приложение

Ядро отдаёт API (`cf serve`, `docs/openapi.json`); само приложение (SwiftUI) пишет GPT по `docs/gui-spec.md` — в репозитории его ещё нет. Проверить результат на Маке: критерии в §9 ТЗ.

## Облачные сессии

`.claude/hooks/session-start.sh` (SessionStart, синхронный): ffmpeg, libegl1/libgles2, uv venv на Python 3.12, `pip install -e .[dev]`, модель лиц. После него сразу работают `make lint` и `make test`.

## Блокеры

Нет.

## Проверить на Маке

```bash
make setup-mac
.venv/bin/cf capabilities          # VideoToolbox: да, h264_videotoolbox, mlx в транскриберах
make test

# 1. Реальный pipeline (2–5 мин видео с речью и лицом)
cp .env.example .env               # ANTHROPIC_API_KEY=...
cp config/accounts.example.yaml config/accounts.yaml
.venv/bin/cf run ~/Movies/sample.mp4 --campaign example
open data/jobs/<JOB_ID>/clips/c01/final.mp4
#   проверить: кириллица в субтитрах (CF_CAPTION_FONT=Arial), кроп держит лицо и не дёргается,
#   videotoolbox-рендер проходит валидацию длительности, транскрипт mlx с пословными таймстемпами

# 2. Кеш и retry
.venv/bin/cf run ~/Movies/sample.mp4 --campaign example   # новый job; затем:
.venv/bin/cf retry <JOB_ID> --force-stage captions         # ingest/transcribe/select = cache

# 3. Очередь
brew services start redis
CF_QUEUE=rq .venv/bin/cf enqueue ~/Movies/sample.mp4 -c example && CF_QUEUE=rq .venv/bin/cf worker --burst

# 4. Бот (.env: TELEGRAM_BOT_TOKEN, CF_ADMIN_IDS — свой id у @userinfobot)
.venv/bin/cf bot
#   прислать абсолютный путь к видео -> кампания -> прогресс в одном сообщении -> карточки ->
#   ✅/❌/✏️/🔤/🎯; с чужого аккаунта бот молчит; /stats, /publish JOB_ID

# 5. YouTube (тестовый канал; см. docs/runbook.md#youtube)
.venv/bin/cf auth youtube --account yt_main
.venv/bin/cf review <JOB_ID> c01 approve
.venv/bin/cf publish <JOB_ID>      # YouTube Studio: private + запланировано; TikTok — data/exports/...
.venv/bin/cf track                 # после publishAt
.venv/bin/cf report --recommendations
```

## Следующий шаг

Все фазы 0–5 по ТЗ и раздел «Дальше» (перенос на сервер) выполнены. Дальше — пройти чеклист «Проверить на Маке» и исправить найденное на реальных бэкендах (в первую очередь: качество выбора моментов на реальном Claude, параметры гистерезиса кропа `ReframeParams`, размер шрифта субтитров `CF_CAPTION_FONT_SIZE`). Remote compute и улучшения из раздела README «Дальше» — только по решению Босса.
