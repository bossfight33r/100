# STATUS — передача проекта

Новая сессия: прочитай `CLAUDE.md` и этот файл, затем продолжай с «Следующий шаг».

## Фазы

| Фаза | Статус |
|---|---|
| 0 Foundation | ✅ готово |
| 1 Local pipeline | ✅ готово |
| 2 Durable orchestration | ⏳ в работе |
| 3 Review + Telegram | — |
| 4 Publishing | — |
| 5 Tracking + earnings | — |

## Что работает

- `cf capabilities`, `cf run SOURCE --campaign ID`, `cf status JOB_ID`.
- Полный локальный pipeline: ingest → transcribe → select → reframe → captions → render → клипы в DB, job `awaiting_review`.
- E2E на синтетике (`tests/test_e2e.py`): 1080x1920, H.264, AAC, 30 fps, длительность, meta/captions/reframe/thumb.
- Реальный MediaPipe проверен в Linux-контейнере (нужны `libegl1 libgles2`).

## Что не работает / не реализовано

- Кеш/манифесты/resume/retry, RQ-воркер (Фаза 2).
- Реальные mlx-whisper, faster-whisper (модель не скачивалась), Anthropic API — не вызывались (запрещено/нет сети), покрыты fake.

## Блокеры

Нет.

## Проверить на Маке

```bash
make setup-mac
.venv/bin/cf capabilities          # VideoToolbox: да, h264_videotoolbox, mlx в транскриберах
make test
# реальный прогон на коротком ролике (2–5 мин, с речью и лицом в кадре):
export ANTHROPIC_API_KEY=...
.venv/bin/cf run ~/Movies/sample.mp4 --campaign example
open data/jobs/<JOB_ID>/clips/c01/final.mp4
```
Проверить: кириллица в субтитрах (шрифт `CF_CAPTION_FONT=Arial`), кроп держит лицо и не дёргается, videotoolbox-рендер проходит валидацию длительности.

## Следующий шаг

Фаза 2: манифесты этапов, валидация кеша, resume/retry, stage_runs в DB, RQQueue + `cf worker`, `cf enqueue`, `cf retry`, флаги `--force-stage`, `--no-cache`.
