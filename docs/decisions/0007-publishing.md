# ADR-0007: Публикация — YouTube API с publishAt, export-пакеты для TikTok/Instagram

**Решение.**
- `Publisher` Protocol; `YouTubePublisher` — resumable upload (`MediaFileUpload`, чанки 8 МБ, ретраи 5xx и сетевых ошибок с экспоненциальной паузой), отложенная публикация через `privacyStatus=private` + `publishAt`. Квота — retryable (публикация остаётся `scheduled`), 401/403 — нет.
- TikTok/Instagram — `ExportPublisher`: пакет для ручной заливки (официальные API публикации требуют аудита приложения).
- Scheduler — чистая функция `plan_slots`: окна в timezone аккаунта, daily_limit по календарным суткам аккаунта с учётом уже запланированных, минимальный шаг 90 мин, пропуск несуществующего времени при переходе на летнее.
- Id публикации детерминирован `{job}-{clip}-{account}` + UNIQUE(job, clip, account) → идемпотентное планирование.
- Токены: `data/secrets/{token_ref}.json`, права 600/700, в YAML только `token_ref`.

**Почему.** YouTube сам публикует по расписанию — не нужен постоянно работающий планировщик загрузок.

**Отброшено.** Неофициальные API/скрейпинг TikTok/Instagram (риск бана, нарушение правил), хранение токенов в SQLite.
