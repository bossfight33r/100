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

- **CLI**: `capabilities`, `run`, `status`, `enqueue`, `retry`, `worker`, `review`, `auth youtube`, `publish`, `track`, `report`, `bot`; флаги `--json`, `--verbose`, `--force-stage`, `--no-cache`.
- **Pipeline**: ingest (файл/HTTP/yt-dlp, remux без перекодирования) → transcribe (mlx/faster-whisper, пословно) → select (Claude, чанки 20 мин/60 с, подгонка по словам и паузам) → reframe (MediaPipe + смены сцен, гистерезис, кусочно-постоянный кроп) → captions (ASS, подсветка слова, safe zone) → render (один ffmpeg, 1080x1920, loudnorm, H.264/AAC, валидация ffprobe) → meta.json по платформам.
- **Надёжность**: манифесты и кеш по хешам, resume/retry с первого невалидного этапа, stage_runs, RQ + SimpleWorker, восстановление job из SQLite после падения воркера/потери Redis.
- **Ревью**: approve/reject/edit metadata/rerender captions/crop, журнал review_actions; метаданные переиспользуются при перерендере.
- **Бот**: ADMIN_IDS, файл/URL/путь, выбор кампании, прогресс одним сообщением, карточки клипов, кнопки ревью, retry, `/jobs`, `/status`, `/publish`, `/stats`.
- **Публикация**: scheduler (окна, timezone, daily_limit, DST), YouTube resumable upload + publishAt, export-пакеты TikTok/Instagram, rejected не публикуются.
- **Статистика/доход**: YouTube collector, ручной ввод, append-only снимки, earnings-стратегия, отчёты, аналитика хуков, файл рекомендаций к промпту.
- Проверено в Linux-контейнере: реальный ffmpeg-рендер, реальный MediaPipe (нужны `libegl1 libgles2`), реальный redis-server + `cf worker`.

## Что не проверено / ограничения

- mlx-whisper, h264_videotoolbox — нет Мака в окружении; faster-whisper не запускался (модель не скачивалась). Всё покрыто fake-бэкендами.
- Anthropic API, Telegram, YouTube OAuth/загрузка/статистика — реальные вызовы запрещены в этом окружении; покрыты фейками (SDK-вызовы написаны по документации SDK `anthropic` 1.x).
- Облачный Bot API скачивает файлы только до 20 МБ — для больших видео слать боту путь или ссылку.
- TikTok/Instagram — только export-пакеты (ручная заливка).
- Перерендер одного клипа перерендеривает видео всех клипов job (детерминированно; метаданные переиспользуются).

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

Все фазы 0–5 по ТЗ выполнены. Дальше — пройти чеклист «Проверить на Маке» и исправить найденное на реальных бэкендах (в первую очередь: качество выбора моментов на реальном Claude, параметры гистерезиса кропа `ReframeParams`, размер шрифта субтитров `CF_CAPTION_FONT_SIZE`). Remote compute и улучшения из раздела README «Дальше» — только по решению Босса.
