# ADR-0006: Ревью через overrides и DB, бот = контроллер + тонкая обвязка

**Решение.**
- Ревью-действия (`pipeline/review.py`) пишут только в DB (`clips.status`, `clips.meta_override`, `review_actions`) и в `jobs/{id}/overrides.json` — пользовательский ввод для этапов (центр кропа, nonce субтитров). Артефакты этапов ревью не трогает.
- Перерендер = запуск job с `force_stage` (captions или reframe); кеш пересчитывает остальное. render переиспользует `meta.json`, если `meta_hash` (кандидат + конфиг метаданных) не изменился: LLM не перегенерирует уже проверенные метаданные.
- Бот: `BotController` (вся логика, возвращает Reply/ClipCard/TrackJob) + `build_router` (aiogram). Доступ — outer-middleware по `CF_ADMIN_IDS`. Синхронный pipeline запускается через `asyncio.to_thread`, прогресс — опрос SQLite и `edit_message_text`.
- Тесты бота идут через настоящий `aiogram.Dispatcher.feed_update` с фейковой `BaseSession` — без сети.

**Почему.** Ownership артефактов сохраняется; логика бота тестируется без Telegram.

**Отброшено.** aiogram FSM-storage (для одного админа хватает dict в контроллере); webhook (нужен публичный адрес).
