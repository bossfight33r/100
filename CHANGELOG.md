# Changelog

## [Фаза 0] Foundation

- Структура проекта (src-layout), `pyproject.toml` с extras `[dev]`, `[mac]`, Makefile (`setup`, `setup-mac`, `test`, `lint`, `worker`, `bot`).
- `schemas.py` — все контракты из ТЗ, `config.py` — Settings + загрузка YAML кампаний и аккаунтов.
- SQLite bootstrap (`db.py`): jobs, stage_runs, clips, publications, stats_snapshots (append-only триггеры), accounts, campaigns, review_actions.
- `ObjectStorage` + `LocalStorage` (атомарная запись, sha256 checksum), `Queue` + `InlineQueue`.
- `media/ffmpeg.py`: единая обёртка (argv, timeout, cancel, stderr в файл, хвост 30 строк), ffprobe-парсер, детекция энкодеров, сцены, кадры.
- `WorkerCapabilities`/`TaskRequirements`, бэкенды encoder/transcriber/llm/face с fake-реализациями.
- structlog с редактированием секретов, CLI `cf --help`, `cf capabilities`.
