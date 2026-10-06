#!/bin/bash
# SessionStart: зависимости для `make lint` и `make test` в облачных сессиях Claude Code.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

# Системные пакеты: ffmpeg (pipeline/тесты), libEGL/GLES (MediaPipe на Linux).
missing=()
command -v ffmpeg >/dev/null 2>&1 || missing+=(ffmpeg)
ldconfig -p 2>/dev/null | grep -q libEGL.so.1 || missing+=(libegl1)
ldconfig -p 2>/dev/null | grep -q libGLESv2.so.2 || missing+=(libgles2)
if [ "${#missing[@]}" -gt 0 ] && command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -q "${missing[@]}" >/dev/null 2>&1 \
    || { apt-get update -q >/dev/null 2>&1 && apt-get install -y -q "${missing[@]}" >/dev/null 2>&1; } \
    || echo "session-start: could not install ${missing[*]} (tests needing them will skip/fail)" >&2
fi

# Python 3.12 venv + проект с dev-зависимостями (идемпотентно; uv кеширует).
command -v uv >/dev/null 2>&1 || pip install -q uv
[ -x .venv/bin/python ] || uv venv -q --python 3.12 .venv
uv pip install -q --python .venv/bin/python -e ".[dev]"

# Модель лиц MediaPipe (нужна для smoke-теста реального детектора).
make -s face-model >/dev/null 2>&1 || echo "session-start: face model download failed (smoke test will skip)" >&2

echo 'export PATH="$CLAUDE_PROJECT_DIR/.venv/bin:$PATH"' >> "${CLAUDE_ENV_FILE:-/dev/null}"
