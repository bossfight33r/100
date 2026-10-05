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
