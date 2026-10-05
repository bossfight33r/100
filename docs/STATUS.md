# STATUS — передача проекта

Новая сессия: прочитай `CLAUDE.md` и этот файл, затем продолжай с «Следующий шаг».

## Фазы

| Фаза | Статус |
|---|---|
| 0 Foundation | ✅ готово |
| 1 Local pipeline | ✅ готово |
| 2 Durable orchestration | ✅ готово |
| 3 Review + Telegram | ⏳ в работе |
| 4 Publishing | — |
| 5 Tracking + earnings | — |

## Что работает

- `cf capabilities`, `cf run`, `cf status`, `cf enqueue`, `cf retry`, `cf worker` (+ `--force-stage`, `--no-cache`).
- Манифесты и кеш этапов (хеши входов/выходов/конфига + validate), resume с первого невалидного этапа, stage_runs в DB.
- RQ-очередь и воркер с восстановлением после падения воркера/потери Redis (проверено тестами на fakeredis и вручную на реальном redis-server).
- Полный локальный pipeline: ingest → transcribe → select → reframe → captions → render → клипы в DB, job `awaiting_review`.
- E2E на синтетике (`tests/test_e2e.py`): 1080x1920, H.264, AAC, 30 fps, длительность, meta/captions/reframe/thumb.
- Реальный MediaPipe проверен в Linux-контейнере (нужны `libegl1 libgles2`).

## Что не работает / не реализовано

- Ревью, бот (Фаза 3), публикация (Фаза 4), статистика (Фаза 5).
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
# очередь
brew services start redis
CF_QUEUE=rq .venv/bin/cf enqueue ~/Movies/sample.mp4 -c example && CF_QUEUE=rq .venv/bin/cf worker --burst
```
Проверить: кириллица в субтитрах (шрифт `CF_CAPTION_FONT=Arial`), кроп держит лицо и не дёргается, videotoolbox-рендер проходит валидацию длительности.

## Следующий шаг

Фаза 3: сервис ревью (approve/reject/edit metadata/rerender captions/crop + review_actions), Telegram-бот на aiogram 3 (ADMIN_IDS, приём файла/URL, выбор кампании, прогресс одним сообщением, карточки клипов, retry упавшего этапа), тесты без сети.
