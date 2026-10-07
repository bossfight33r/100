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
| ingest | `job.source` (файл / http(s) / yt-dlp URL) | `source.mp4`, `source.info.json` (для yt-dlp: название, главы, кривая «Most replayed») | mp4/mov с h264/hevc/aac копируется байт-в-байт; иначе remux `-c copy`, при неудаче — перекодирование. Проверка: есть видео, длительность > 0 |
| transcribe | `source.mp4` | `audio.wav` (mono 16 kHz, `-bitexact`), `transcript.json` | Transcriber с пословными таймстемпами. Нет аудио/речи — ошибка, не retryable. `transcribe: false` — только `audio.wav` и пустой транскрипт; при `selection: signals` пустая речь не ошибка |
| select | `transcript.json` (+ `audio.wav`, `source.info.json` при `selection: signals`) | `highlights.json` | **signals** (ADR-0014): громкость по 0,5 с → всплеск над медианой за 60 с → + кривая «Most replayed» (вес 0,6) → пики → окна, пик на 65% клипа → не резать слова → top-N; **transcript**: чанки 20 мин / overlap 60 с → LLM (`prompts/highlights.md`) → нормализация → подгонка по словам/паузам → clip_min/max → дедуп overlap > 50% → top-N; id `c01…` по хронологии |
| reframe | `source.mp4`, `highlights.json` | `clips/{id}/reframe.json` | смены сцен (ffmpeg `select=scene`), кадры 4 fps (320 px) → лица → гистерезис (порог 8% ширины, удержание 0.75 с) → кусочно-постоянный кроп; без лиц — центр |
| captions | `transcript.json`, `highlights.json` | `clips/{id}/captions.ass` | группы 2–4 слова, активное слово жёлтым, обводка, MarginV 26% высоты |
| render | всё выше | `final.mp4`, `thumb.jpg`, `meta.json` | один ffmpeg: `-ss` trim, crop-выражение по t, scale 1080x1920, ass, loudnorm, 30 fps, H.264, AAC 128k, faststart; проверка ffprobe; метаданные по платформам с правилами кампании |

После render клипы записываются в таблицу `clips`, job → `awaiting_review`.

## Манифесты и кеш

`jobs/{id}/manifest.json` (пишет только оркестратор): для каждого этапа `stage, stage_version, input_hashes, config_hash, output_hashes, started_at, completed_at, status`.

Этап пропускается (cache hit), только если одновременно:
1. есть запись со `status=completed`;
2. `stage_version` совпадает с кодом (повышай `version` при изменении логики этапа);
3. `config_hash` = sha256 от `stage.config(ctx)` (бэкенд, модель, параметры кампании, промпт, overrides);
4. `input_hashes` совпадают с текущими sha256 входных артефактов (для ingest — sha256 исходного файла или URL);
5. все выходы существуют и их sha256 совпадают;
6. `stage.validate()` проходит.

Перед запуском этап удаляется из манифеста, после успешной валидации записывается снова: прерванный этап никогда не считается готовым.

Перезапуск: `cf retry JOB_ID` — с первого невалидного этапа; `--force-stage reframe` — reframe и всё после; `cf run ... --no-cache` — всё заново. Каждое выполнение/пропуск пишется в `stage_runs` (`cached=1` для кеша).

## Кеш на уровне клипа

Внутри reframe и render каждый клип хранит `clips/{id}/.{stage}.cache.json` — отпечаток своих входов (версия этапа, sha256 исходника, границы клипа, параметры/энкодер, правка ревью, sha256 `reframe.json`/`captions.ass`) и sha256 своих выходов. Если этап перезапускается (сменился конфиг, правка одного клипа, `--force-stage`), клипы с совпавшим отпечатком и целыми выходами не пересчитываются. `--no-cache` отключает и этот кеш. captions не кешируется — генерация мгновенная и детерминированная.

## Ошибки

`orchestrator.classify_error` → `error_type`, `retryable` в `jobs`. ffmpeg-ошибки не retryable (детерминированы), таймауты и сеть — retryable. В сообщении — последние 30 строк stderr и путь к логу.
