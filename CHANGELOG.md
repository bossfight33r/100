# Changelog

## [Фаза 3] Review + Telegram

- `pipeline/review.py`: approve, reject, edit metadata (с повторным применением правил кампании), rerender captions/crop через `overrides.json`, журнал `review_actions`.
- render переиспользует метаданные при перерендере (`ClipMeta.meta_hash`).
- Бот aiogram 3: `BotController` + router, AdminMiddleware по `CF_ADMIN_IDS`, приём файла/URL/пути, выбор кампании, прогресс одним сообщением, карточки клипов, кнопки ревью, retry упавшего этапа; секреты редактируются.
- CLI: `cf bot`, `cf review`.
- Тесты: сервис ревью, контроллер, полный цикл через `Dispatcher.feed_update` на фейковой сессии.

## [Фаза 2] Durable orchestration

- `manifest.json` на job, кеш этапа по stage_version + config_hash + input/output sha256 + validate().
- Resume с первого невалидного этапа, `--force-stage`, `--no-cache`, запись `stage_runs` (cached/completed/failed/abandoned).
- `RQQueue` (Redis/RQ), `worker.py` (`execute_task`, `recover`, SimpleWorker), CLI `cf enqueue`, `cf retry`, `cf worker`.
- Восстановление после падения воркера и потери Redis из SQLite.
- Тесты: acceptance «сломанный render → retry без повторного ingest/transcribe/select», инвалидация по подмене/удалению выходов и смене конфига, recover на fakeredis.

## [Фаза 1] Local pipeline

- Этапы ingest, transcribe, select, reframe, captions, render (`pipeline/`), оркестратор, сервисный слой `services.App`.
- select: чанки с перекрытием (абсолютные таймстемпы), LLM-промпт `prompts/highlights.md`, нормализация JSON, подгонка границ по словам и паузам, clip_min/max, дедуп.
- reframe: MediaPipe Tasks + сцены ffmpeg, гистерезис, кусочно-постоянный кроп, center crop без лиц.
- captions: ASS с подсветкой активного слова, safe zone, экранирование.
- render: один вызов ffmpeg на клип, валидация ffprobe, thumb, meta.json с правилами кампании.
- CLI: `cf run`, `cf status`. E2E-тест на синтетике (1080x1920, H.264, AAC, 30 fps).

## [Фаза 0] Foundation

- Структура проекта (src-layout), `pyproject.toml` с extras `[dev]`, `[mac]`, Makefile (`setup`, `setup-mac`, `test`, `lint`, `worker`, `bot`).
- `schemas.py` — все контракты из ТЗ, `config.py` — Settings + загрузка YAML кампаний и аккаунтов.
- SQLite bootstrap (`db.py`): jobs, stage_runs, clips, publications, stats_snapshots (append-only триггеры), accounts, campaigns, review_actions.
- `ObjectStorage` + `LocalStorage` (атомарная запись, sha256 checksum), `Queue` + `InlineQueue`.
- `media/ffmpeg.py`: единая обёртка (argv, timeout, cancel, stderr в файл, хвост 30 строк), ffprobe-парсер, детекция энкодеров, сцены, кадры.
- `WorkerCapabilities`/`TaskRequirements`, бэкенды encoder/transcriber/llm/face с fake-реализациями.
- structlog с редактированием секретов, CLI `cf --help`, `cf capabilities`.
