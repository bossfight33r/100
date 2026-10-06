# ADR-0009: Все внешние сервисы — в backends/ или publish/

**Контекст.** Code review: загрузка по HTTP/yt-dlp жила в `pipeline/ingest.py`, а вызов YouTube Data API для статистики — в `track/collector.py`, вопреки правилу 4 из CLAUDE.md.

**Решение.**
- `backends/downloader/` (`http.py`, `ytdlp.py`, `base.py` с `Downloader` и `DownloadError(retryable)`). Бэкенд не импортирует `pipeline`; ingest оборачивает `DownloadError` в `SourceError`, сохраняя retryable.
- Статистика YouTube — `publish/youtube.fetch_stats`; `track/collector.py` только оркестрирует и пишет снимки.

**Публичный контракт.** `IngestStage(http_downloader=..., ytdlp_downloader=...)` не изменился. Тесты подтверждают поведение: `test_ingest_url_routing`, `test_download_error_becomes_retryable_source_error`, `test_tracking.py`.

**Отброшено.** Отдельный `backends/youtube/` (дробит единый клиент YouTube между двумя пакетами).
