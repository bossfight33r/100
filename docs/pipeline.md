# Pipeline

Раскладка артефактов (ключи `ObjectStorage`, корень `CF_DATA_DIR`):

```
jobs/{job_id}/source.mp4 audio.wav transcript.json highlights.json manifest.json
jobs/{job_id}/clips/{clip_id}/reframe.json captions.ass final.mp4 thumb.jpg meta.json
jobs/{job_id}/logs/{stage}[-{clip_id}].log      # полный stderr ffmpeg
```

Каждый этап — класс с `name`, `version`, `config(ctx)`, `input_keys(ctx)`, `run(ctx) -> StageResult`, `validate(ctx, outputs)` (`pipeline/context.py`). Этап пишет только свои артефакты.

| Этап | Вход | Выход | Что делает |
|---|---|---|---|
| ingest | `job.source` (файл / http(s) / yt-dlp URL) | `source.mp4` | mp4/mov с h264/hevc/aac копируется байт-в-байт; иначе remux `-c copy`, при неудаче — перекодирование. Проверка: есть видео, длительность > 0 |
| transcribe | `source.mp4` | `audio.wav` (mono 16 kHz, `-bitexact`), `transcript.json` | Transcriber с пословными таймстемпами. Нет аудио/речи — ошибка, не retryable |
| select | `transcript.json` | `highlights.json` | чанки 20 мин / overlap 60 с → LLM (`prompts/highlights.md`) → нормализация → подгонка по словам/паузам → clip_min/max → дедуп overlap > 50% → top-N; id `c01…` по хронологии |
| reframe | `source.mp4`, `highlights.json` | `clips/{id}/reframe.json` | смены сцен (ffmpeg `select=scene`), кадры 4 fps (320 px) → лица → гистерезис (порог 8% ширины, удержание 0.75 с) → кусочно-постоянный кроп; без лиц — центр |
| captions | `transcript.json`, `highlights.json` | `clips/{id}/captions.ass` | группы 2–4 слова, активное слово жёлтым, обводка, MarginV 26% высоты |
| render | всё выше | `final.mp4`, `thumb.jpg`, `meta.json` | один ffmpeg: `-ss` trim, crop-выражение по t, scale 1080x1920, ass, loudnorm, 30 fps, H.264, AAC 128k, faststart; проверка ffprobe; метаданные по платформам с правилами кампании |

После render клипы записываются в таблицу `clips`, job → `awaiting_review`.

## Ошибки

`orchestrator.classify_error` → `error_type`, `retryable` в `jobs`. ffmpeg-ошибки не retryable (детерминированы), таймауты и сеть — retryable. В сообщении — последние 30 строк stderr и путь к логу.
