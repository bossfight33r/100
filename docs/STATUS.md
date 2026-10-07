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
- **Игры/стримы** (ADR-0014): `selection: signals` — моменты по пикам звука и YouTube «Most replayed» без Whisper и без LLM-выбора; кампания `cs2`. Проверка LLM-настроек до старта job.
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

## Решения из переписки с Боссом (не вывести из кода)

- **Модель для первого теста:** Gemini 3.5 Flash-Lite через `openai_compat` (`CF_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`, `CF_LLM_MODEL=gemini-3.5-flash-lite`). Ключ только в `.env` (не в git). Про отзыв ключа Боссу больше не напоминать — решение принято. Затем сравнить с DeepSeek V4 Flash на тех же видео. Живой запрос и выбор моментов на русском транскрипте с Gemini проходили (контейнерный тест), Whisper и реальное видео — нет.
- **LLM локально / VPS:** не выгодно без GPU (доли цента за видео по API против $15–40/мес за VPS); VPS имеет смысл только как 24/7-хост бота и очереди.
- **Десктоп-приложение:** SwiftUI на Mac M1, пишет GPT по `docs/gui-spec.md` + `docs/openapi.json`; ядро отдаёт API (`cf serve`). Дизайн: красиво, но спокойно, стандартный вид macOS. В репозитории приложения ещё нет (папка `desktop/`).
- **Первый реальный тест:** ролик с YouTube 3–5 минут, говорящий крупным планом, по-русски; `CF_WHISPER_MODEL=small` для быстрого первого прогона. Для 30-минутного видео поднять `clip_count` до 8–10 (в `example` стоит 3). Игровые видео подходят слабо: модель не видит картинку, кроп без лица идёт по центру.
- **Громкость:** клипы выходили на -17 LUFS вместо -14, исправлено двухпроходной нормализацией (render v2).
- **Контент по CS2:** выбор по речи для игр не годится → режим `signals` (ADR-0014). Ресёрч (GitHub/форумы): heatmap «Most replayed» из yt-dlp, пики звука, килфид-нейросеть (Crispy, MIT), демки CS2 (demoparser2/awpy, cs2-highlights-maker), всплески чата. Сделаны первые два; остальное — по результатам теста на реальных видео. Чужие видео: проверять разрешение кампании/автора (reused content, страйки).
- **Проверка Claude Code на Маке:** хук SessionStart работает только в облаке (`CLAUDE_CODE_REMOTE=true`); на Маке окружение ставится `make setup-mac`.

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

**Сначала (Босс на Маке):** кс-видео по ссылке с YouTube → `.venv/bin/cf run "URL" --campaign cs2`; проверить, что `data/jobs/<id>/source.info.json` содержит `heatmap` (у видео с малым числом просмотров его нет — тогда работает только звук), и что моменты попадают в клатчи/реакции. По результату — подстроить `WEIGHTS`, `PEAK_POSITION`, `target_duration` в `pipeline/signals.py`. Прошлый 25-минутный прогон `20261006-110014-d07406`: Gemini-строки в `.env`, затем `cf retry` (Whisper из кеша).


Все фазы 0–5 по ТЗ и раздел «Дальше» (перенос на сервер) выполнены. Дальше — пройти чеклист «Проверить на Маке» и исправить найденное на реальных бэкендах (в первую очередь: качество выбора моментов на реальном Claude, параметры гистерезиса кропа `ReframeParams`, размер шрифта субтитров `CF_CAPTION_FONT_SIZE`). Remote compute и улучшения из раздела README «Дальше» — только по решению Босса.
