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
| 5 Tracking + earnings | ⏳ в работе |

## Что работает

- `cf capabilities`, `cf run`, `cf status`, `cf enqueue`, `cf retry`, `cf worker` (+ `--force-stage`, `--no-cache`).
- Манифесты и кеш этапов (хеши входов/выходов/конфига + validate), resume с первого невалидного этапа, stage_runs в DB.
- Ревью: approve/reject/edit metadata/rerender captions/rerender crop, всё в `review_actions`; `cf review`.
- Telegram-бот (`cf bot`): ADMIN_IDS, файл/URL/путь, выбор кампании, прогресс одним сообщением, карточки клипов, кнопки ревью, retry упавшего этапа. Протестирован через aiogram Dispatcher на фейковой сессии.
- Публикация: scheduler (окна, timezone, daily_limit, DST), YouTube resumable upload с publishAt, export-пакеты TikTok/Instagram, `cf auth youtube`, `cf publish`, `/publish` в боте; rejected не публикуются.
- RQ-очередь и воркер с восстановлением после падения воркера/потери Redis (проверено тестами на fakeredis и вручную на реальном redis-server).
- Полный локальный pipeline: ingest → transcribe → select → reframe → captions → render → клипы в DB, job `awaiting_review`.
- E2E на синтетике (`tests/test_e2e.py`): 1080x1920, H.264, AAC, 30 fps, длительность, meta/captions/reframe/thumb.
- Реальный MediaPipe проверен в Linux-контейнере (нужны `libegl1 libgles2`).

## Что не работает / не реализовано

- Статистика и доход (Фаза 5).
- Реальный YouTube OAuth/загрузка не проверялись (запрещено здесь), покрыты фейковым клиентом.
- Реальный Telegram не проверялся (запрещено в этом окружении).
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
# бот: в .env TELEGRAM_BOT_TOKEN и CF_ADMIN_IDS (свой id — у @userinfobot)
.venv/bin/cf bot   # отправить боту абсолютный путь к видео -> выбрать кампанию -> дождаться карточек
```
YouTube (своим тестовым каналом, видео уйдёт как private с publishAt):
```bash
# data/secrets/youtube_client_secret.json — Desktop OAuth client (см. docs/runbook.md#youtube)
.venv/bin/cf auth youtube --account yt_main
.venv/bin/cf review <JOB_ID> c01 approve
.venv/bin/cf publish <JOB_ID>          # проверить в YouTube Studio: private + запланировано
```
Проверить в боте: прогресс обновляется в одном сообщении, видео-карточки открываются, кнопки ✅/❌/✏️/🔤/🎯 работают, чужой аккаунт не получает ответов.
Проверить: кириллица в субтитрах (шрифт `CF_CAPTION_FONT=Arial`), кроп держит лицо и не дёргается, videotoolbox-рендер проходит валидацию длительности.

## Следующий шаг

Фаза 5: StatsSnapshot (append-only), YouTube collector (`cf track`), ручной ввод статистики, стратегии earnings, `cf report`, `/stats` в боте, отчёты по кампаниям/аккаунтам/топ-клипам, аналитика хуков + файл рекомендаций к промпту.
