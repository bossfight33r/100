# Runbook

## Проверка окружения

```bash
.venv/bin/cf capabilities          # ffmpeg/ffprobe/энкодеры/транскриберы
.venv/bin/cf --json capabilities   # то же в JSON
```

## Частые ошибки

| Симптом | Причина | Решение |
|---|---|---|
| `ffmpeg not found on PATH` | нет ffmpeg | `brew install ffmpeg` |
| `FFmpegError ... exit code` | ошибка кодирования/фильтра | в исключении последние 30 строк stderr и путь к полному логу; лог лежит рядом с артефактом в `data/jobs/<id>/logs/` |
| `FFmpegTimeout` | слишком долгая операция | увеличить `CF_FFMPEG_TIMEOUT_SEC` |
| `h264_videotoolbox is not available` | ffmpeg собран без VideoToolbox | `CF_ENCODER=auto` или `x264` |
| `MediaPipe face model not found` | не скачана модель | `make face-model` |
| `cannot initialize MediaPipe face detector: libEGL.so.1` | Linux без OpenGL ES | `apt-get install libegl1 libgles2` (на macOS не нужно) или `CF_FACE_DETECTOR=fake` |
| `source has no audio track` | в видео нет звука | транскрибировать нечего; не retryable |
| `LLM returned no usable highlights` | модель не нашла моменты или ответила не JSON 3 раза | проверить `notes` кампании, длину видео; `cf retry` |

## Воркер и очередь

```bash
redis-server &                      # или brew services start redis
export CF_QUEUE=rq
.venv/bin/cf enqueue video.mp4 -c example
.venv/bin/cf worker                 # Ctrl+C для остановки; --burst — выйти, когда очередь пуста
```

- Воркер — `rq.SimpleWorker` (без fork: на macOS fork + ObjC/Metal небезопасен).
- При старте воркер чистит мёртвые started-задачи RQ и ставит заново все job из SQLite в статусах `queued`…`rendering`, у которых нет живой задачи. Потеря Redis не теряет состояние: оно в SQLite + манифестах.
- Retry: `cf retry JOB_ID` — валидные этапы берутся из кеша. Повторять имеет смысл при `retryable=true` (сеть, таймаут, rate limit); при `false` сначала исправить причину.
- Отмена: `Queue.cancel` снимает только ещё не начатую задачу.

## Telegram-бот

```bash
# .env: TELEGRAM_BOT_TOKEN=... (от @BotFather), CF_ADMIN_IDS=<твой user id через запятую>
.venv/bin/cf bot
```

- Отвечает только `CF_ADMIN_IDS`; остальным — тишина. Пустой `CF_ADMIN_IDS` — бот не стартует.
- Источник: видеофайл (лимит облачного Bot API на скачивание — **20 МБ**), ссылка или абсолютный путь к файлу на машине с ботом (для больших файлов — самый простой путь).
- Прогресс — одно сообщение, редактируется каждые 3 с; после — карточки клипов (видео + метаданные + кнопки).
- Кнопки: одобрить, отклонить, метаданные (ответ: заголовок / описание / хэштеги построчно), субтитры заново, кроп (центр 0–100% или `auto`).
- Ошибка показывает упавший этап, тип ошибки и кнопку «Повторить» (retry с кешем). Текст ошибки проходит через редактирование секретов и обрезается.
- С `CF_QUEUE=inline` pipeline идёт в потоке процесса бота; с `CF_QUEUE=rq` бот только ставит задачу — нужен запущенный `cf worker`.

## YouTube

Однократная настройка (Google Cloud Console):
1. Создать проект → включить **YouTube Data API v3**.
2. OAuth consent screen: External, добавить себя в Test users.
3. Credentials → Create OAuth client ID → **Desktop app** → скачать JSON в `data/secrets/youtube_client_secret.json` (или путь в `CF_YOUTUBE_CLIENT_SECRETS`).
4. В `config/accounts.yaml` у аккаунта `platform: youtube`, `token_ref: yt_main`.
5. `.venv/bin/cf auth youtube --account yt_main` — откроется браузер, токен сохранится в `data/secrets/yt_main.json` (права 600). Refresh-токен обновляется автоматически.

Публикация: `cf publish JOB_ID` (или `/publish JOB_ID` в боте). В YouTube видео загружается сразу как `private` с `publishAt` = слот; публикует сам YouTube. Слоты: окна `posting_windows` в `timezone` аккаунта, не больше `daily_limit` в сутки, шаг ≥ 90 мин, не раньше чем через 20 мин.

| Ошибка | Что делать |
|---|---|
| `YouTube quota exceeded` | дневная квота API (по умолчанию 10 000 единиц, загрузка стоит ~1600 → ~6 видео/сутки). Публикация остаётся `scheduled`; повторить `cf publish JOB_ID` завтра или запросить квоту |
| `auth/permission error 401/403` | токен отозван или нет прав — `cf auth youtube --account ID` заново |
| `no token for ...` | не выполнен `cf auth youtube` |
| `clip is rejected` | отклонённые клипы не публикуются никогда (проверка и при планировании, и перед загрузкой) |

## TikTok / Instagram

Официальная публикация через API требует одобренного приложения, поэтому сейчас `cf publish` собирает **export-пакет**: `data/exports/{platform}/{account}/{локальная дата_время}_{publication_id}/` с `video.mp4`, `thumb.jpg`, `caption.txt`, `meta.json`. Залить вручную в указанное время. Статус публикации — `exported`.

## Статистика и доход

```bash
.venv/bin/cf track                                  # YouTube: views/likes/comments (1 ед. квоты на 50 видео)
.venv/bin/cf track --manual <PUB_ID> --views 12000  # TikTok/Instagram вручную
.venv/bin/cf report                                 # кампании, аккаунты, топ-клипы, хуки
.venv/bin/cf report --recommendations               # + data/reports/prompt_recommendations_*.md
```

- Снимки `stats_snapshots` только добавляются (UPDATE/DELETE запрещены триггером) — история не теряется.
- Доход считается при каждом отчёте из последнего снимка: `views / 1000 × rate_per_1k_views` (`track/earnings.py`, стратегия расширяется).
- Рекомендации к промпту — отдельный файл; `prompts/highlights.md` автоматически не меняется.
- Периодический сбор на Маке: `crontab -e` → `0 */6 * * * cd ~/clipfactory && .venv/bin/cf track >> data/track.log 2>&1`.

## Прогон без внешних API

```bash
CF_TRANSCRIBER=fake CF_LLM_PROVIDER=fake CF_FACE_DETECTOR=fake .venv/bin/cf run video.mp4 --campaign example
```
