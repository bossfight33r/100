# STATUS — передача проекта

Новая сессия: прочитай `CLAUDE.md` и этот файл, затем продолжай с «Следующий шаг».

## Фазы

| Фаза | Статус |
|---|---|
| 0 Foundation | ✅ готово |
| 1 Local pipeline | ⏳ в работе |
| 2 Durable orchestration | — |
| 3 Review + Telegram | — |
| 4 Publishing | — |
| 5 Tracking + earnings | — |

## Что работает

- `cf --help`, `cf capabilities` (ffmpeg, ffprobe, энкодеры, платформа, транскриберы).
- Контракты `schemas.py`, Settings + YAML, SQLite-схема, LocalStorage, InlineQueue, ffmpeg-обёртка, бэкенды с fake.
- Тесты: `make test` (без сети, без GPU), lint: `make lint`.

## Что не работает / не реализовано

- Pipeline-этапы (Фаза 1).

## Блокеры

Нет.

## Проверить на Маке

```bash
make setup-mac
.venv/bin/cf capabilities   # ожидается: VideoToolbox: да, h264_videotoolbox в энкодерах, mlx в транскриберах
make test
```

## Следующий шаг

Фаза 1: этапы ingest → transcribe → select → reframe → captions → render, `cf run FILE --campaign example`.
