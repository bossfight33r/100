# ADR-0004: Контракт этапа, сервисный слой, ffmpeg-граф в argv

**Решение.**
- Этап = объект с `name/version/config/input_keys/run/validate`; `StageContext` (job, campaign, settings, storage, ленивые `Backends`, overrides, cancel). Контекст живёт в `pipeline/context.py`.
- `services.py` (`App`) — единственное место сборки зависимостей; CLI, бот и воркер вызывают его. Фабрики бэкендов подменяются в тестах.
- Filtergraph рендера передаётся через `-filter_complex` в argv (без shell), ASS-файл указывается относительным именем, ffmpeg запускается с `cwd` = каталог клипа. Так юникод и спецсимволы в путях не попадают в синтаксис фильтров.
- Метаданные (`meta.json`) генерирует render — так требует ownership (render владеет meta.json). Правки ревью хранятся в DB (`clips.meta_override`), а не в артефакте.

**Почему.** `-filter_complex_script` устаревает в ffmpeg 7+, на Маке будет свежий ffmpeg. Сервисный слой нужен для бота (Фаза 3), чтобы не дублировать логику CLI.

**Отброшено.** Отдельный этап metadata (нет в списке job states), абсолютные пути в фильтре ass (экранирование `:` `'` `\` ломается на произвольных именах).
