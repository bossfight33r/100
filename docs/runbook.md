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

## Прогон без внешних API

```bash
CF_TRANSCRIBER=fake CF_LLM_PROVIDER=fake CF_FACE_DETECTOR=fake .venv/bin/cf run video.mp4 --campaign example
```
