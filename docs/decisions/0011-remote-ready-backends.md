# ADR-0011: Реализации для выноса на сервер — S3, NVENC, Ollama

**Контекст.** ТЗ (режим бюджета) запрещало создавать `s3.py`, `nvenc.py`, `ollama.py` до готовности фаз 0–5. Фазы закрыты, Босс снял ограничение («продолжай до максимума»).

**Решение.**
- `storage/s3.py` (`CF_STORAGE=s3`, `CF_S3_BUCKET`, `CF_S3_PREFIX`, `CF_S3_ENDPOINT_URL` для MinIO): sha256 в метаданных объекта -> `checksum` = HEAD, кеш этапов работает без скачивания. Этапы уже работали через ключи и `ctx.local_path`/`commit`; `App.materialize` даёт локальный файл публикации и боту; scratch job удаляется после прогона, логи ffmpeg перед этим загружаются в `jobs/{id}/logs/`.
- Энкодер выбирается только после пробного кодирования (`ffmpeg.encoder_works`): ffmpeg перечисляет `h264_nvenc` и без GPU. auto: videotoolbox -> nvenc -> x264. `NvencEncoder` для GPU-сервера.
- `backends/llm/ollama.py` (`CF_LLM_PROVIDER=ollama`): `/api/chat`, `format=json`, без новых зависимостей.

**Проверка.** Весь pipeline на S3 через moto (`tests/test_s3.py`), Ollama против локального HTTP-сервера, выбор энкодера с «неработающим» nvenc.

**Отброшено.** fsspec (лишний слой), aiobotocore (pipeline синхронный), выбор энкодера по `platform.system()`.
