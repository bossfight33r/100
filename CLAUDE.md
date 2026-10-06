# CLAUDE.md — правила работы в ClipFactory

Новая сессия начинает с чтения этого файла и `docs/STATUS.md`.

1. Не читать целиком медиа, `transcript.json`, логи ffmpeg и `refs/` — только head/tail/grep/range reads.
2. Не коммитить `data/`, медиа, `.env`, токены и секреты.
3. Данные между этапами — только через `src/clipfactory/schemas.py`.
4. Внешние сервисы (LLM, Whisper, YouTube, Telegram) — только через `backends/`, `publish/`, `bot/`.
5. ffmpeg/ffprobe — только через `src/clipfactory/media/ffmpeg.py`.
6. Этапы идемпотентны; кеш — только через манифест (hashes + stage_version + config_hash) или клиповый сайдкар `clipcache` (отпечаток входов + sha256 выходов, ADR-0010); `exists()` сам по себе — никогда не критерий кеша.
7. Работающие модули не переписывать без необходимости; изменение архитектуры — через ADR в `docs/decisions/`.
8. В тестах никаких реальных API (YouTube, Telegram, Anthropic, Redis, интернет).
9. Бот обслуживает только `ADMIN_IDS`.
10. Не реализовывать будущие фазы раньше времени.
11. После каждой фазы: `make lint` → `make test` → docs (`docs/STATUS.md`, `CHANGELOG.md`) → commit → push.
12. Новая сессия начинает работу с чтения `CLAUDE.md` и `docs/STATUS.md`.

Команды: `make setup` (uv venv + deps), `make test`, `make lint`, `.venv/bin/cf --help`.
