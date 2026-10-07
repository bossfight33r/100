# Changelog

## [Сигналы] Выбор моментов без речи — игры и стримы (ADR-0014)

- Кампания `selection: signals`: моменты по всплескам звука (над медианой за 60 с) и кривой YouTube «Most replayed», без LLM; LLM пишет только метаданные. `transcribe: false` — без Whisper.
- ingest сохраняет `source.info.json` из yt-dlp (название, канал, главы, heatmap; битые точки отбрасываются).
- Кампания `config/campaigns/cs2.yaml`.
- `cf run`/`enqueue`, бот и API проверяют настройки LLM до создания job: пустой ключ — сразу понятная ошибка, а не падение select после транскрипции.
- Кеш кампаний `transcript` не меняется: идущие job не пересчитывают Whisper.
- Тесты `test_signals.py`: ряды, нормировка heatmap, окна, тихое/короткое видео, проверка LLM-настроек, e2e без Whisper.

## [Звук] Двухпроходная нормализация громкости

- Клипы выходили на -17 LUFS при цели -14, пики до -0,6 дБ. Теперь render измеряет звук клипа (первый проход `loudnorm`) и применяет линейное усиление; цель -14 LUFS, TP -2 дБ (запас под AAC). Тишина — запасной однопроходный фильтр. `RenderStage.version` 2: старые клипы перерендерятся.
- Тесты `test_loudness.py` меряют готовые клипы (тихий -38 дБ и громкий -8 дБ источник): ±1 LU от цели, пик ≤ -1 дБ.

## [API] Локальный HTTP-API и ТЗ десктоп-приложения

- `api/app.py` (FastAPI): задачи, клипы и ревью, видео/обложки с Range, публикации, статистика, отчёт; только 127.0.0.1, Bearer-токен, access-лог выключен. `cf serve`.
- `docs/openapi.json` и `docs/gui-spec.md` — ТЗ для SwiftUI-приложения (macOS, Apple Silicon): запуск ядра, экраны, дизайн, запреты, критерии приёмки.

## [LLM] Универсальный OpenAI-совместимый бэкенд

- `backends/llm/openai_compat.py` (`CF_LLM_PROVIDER=openai_compat`): один код для OpenAI, DeepSeek, Gemini, OpenRouter, vLLM, LM Studio; без новых зависимостей; ключ из env и не попадает в ошибки; переключатели `CF_LLM_JSON_MODE`, `CF_LLM_TOKEN_PARAM`, `CF_LLM_TEMPERATURE`.

## [Перенос на сервер] S3, NVENC, Ollama, маршрутизация, PostgreSQL

- `storage/s3.py` — S3/MinIO (`CF_STORAGE=s3`), sha256 в метаданных объекта, `App.materialize`, логи ffmpeg загружаются в хранилище, путь к логу в ошибке ведёт туда (ADR-0011).
- Энкодер выбирается после пробного кодирования; `NvencEncoder`; `cf capabilities` показывает рабочие H.264-энкодеры.
- `backends/llm/ollama.py` — локальные модели (`CF_LLM_PROVIDER=ollama`, `CF_OLLAMA_NUM_CTX`).
- `compute/routing.py` — очереди `clipfactory@<теги>`, теги воркера из реальных возможностей, heartbeat-реестр, требования job сохраняются в БД (миграция v3) (ADR-0012).
- PostgreSQL как второй диалект `Database` с пулом соединений (`CF_DB_URL`), проверен на настоящем PostgreSQL 16 (ADR-0013).
- Третий проход code review: 10 находок закрыто.

## [После фаз] Code review и доработки

- Code review всей ветки: 10 находок, все закрыты (статус ревью при смене момента, зависший трекер бота, публикация в прошедший слот, код выхода ffmpeg, статус job, превью, двойное хеширование, зависшие `publishing`, слои внешних сервисов — ADR-0009) + найденные тестами: воскрешение failed-публикаций при повторном планировании, несработавший сброс статуса клипа.
- Двухуровневое экранирование путей в filtergraph ffmpeg; `JobBusy` против дублей задач; понятная ошибка неизвестной кампании; `CF_LLM_MAX_TOKENS` используется.
- Кеш на уровне клипа в reframe/render (ADR-0010): правка одного клипа не перекодирует остальные.
- Отмена идущей job: `cf cancel`, `/cancel`, флаг в SQLite + наблюдатель, миграции схемы БД (v2).
- `cf review` с edit/captions/crop, `cf publish --retry-failed`, `cf track --every`.
- Тесты адаптеров faster-whisper/mlx/Anthropic/YouTube на настоящих типах библиотек.
- Второй проход code review: 10 находок закрыто — восстановление прерванной загрузки (было недостижимо), лиза 30 мин для `publishing`, гонка миграций (`BEGIN IMMEDIATE`), мгновенная проверка отмены и верный `failed_stage`, устойчивый `cf track --every`, `/cancel` вне event loop, проверка занятости job до перерендера, уточнено правило 6 в CLAUDE.md.
- Security review всей ветки: HIGH/MEDIUM не найдено; усилено — редактирование токена бота в URL Bot API, отказ от плейлистов (hls/concat) как источника.
- SessionStart-хук для облачных сессий; проверено: чистый клон → `make setup` → lint → 141 тест зелёные.
- Локальный Telegram Bot API server (`CF_TELEGRAM_API_URL`) — файлы до 2 ГБ.

## [Фаза 5] Tracking + earnings

- `track/collector.py`: сбор статистики YouTube (батчи по 50), отметка опубликованных по publishAt, ручной ввод; снимки append-only.
- `track/earnings.py`: стратегия `FlatRatePerK` (Decimal, округление до центов), точка расширения `strategy_for`.
- `track/report.py`: отчёты по кампаниям, аккаунтам, топ-клипам; аналитика хуков (lift), корреляция score ↔ просмотры; файл рекомендаций к промпту без изменения production-промпта.
- CLI `cf track`, `cf report`; бот `/stats`.

## [Фаза 4] Publishing

- `publish/scheduler.py`: слоты по posting_windows/timezone/daily_limit, шаг 90 мин, DST.
- `publish/youtube.py`: OAuth (`cf auth youtube`), токен 600 в `data/secrets/`, resumable upload с ретраями, private + publishAt, классификация квоты/auth-ошибок.
- `publish/export.py`: пакеты для ручной заливки в TikTok/Instagram.
- `pipeline/publish.py`: идемпотентное планирование одобренных клипов, публикация, запрет rejected (двойная проверка), `mark_due_published`.
- CLI `cf auth youtube`, `cf publish`; бот `/publish`.

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
