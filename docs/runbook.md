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

## Прогон без внешних API

```bash
CF_TRANSCRIBER=fake CF_LLM_PROVIDER=fake CF_FACE_DETECTOR=fake .venv/bin/cf run video.mp4 --campaign example
```
