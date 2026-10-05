PY := .venv/bin/python
UV ?= uv
FACE_MODEL_URL := https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite

.PHONY: setup setup-mac face-model test lint fmt worker bot redis

setup:
	$(UV) venv --python 3.12 .venv
	$(UV) pip install --python $(PY) -e ".[dev]"
	$(MAKE) face-model

setup-mac:
	command -v ffmpeg >/dev/null || brew install ffmpeg
	command -v redis-server >/dev/null || brew install redis
	$(UV) venv --python 3.12 .venv
	$(UV) pip install --python $(PY) -e ".[dev,mac]"
	$(MAKE) face-model
	.venv/bin/cf capabilities

face-model:
	mkdir -p data/models
	test -f data/models/blaze_face_short_range.tflite || curl -sSfL -o data/models/blaze_face_short_range.tflite $(FACE_MODEL_URL)

test:
	$(PY) -m pytest

lint:
	.venv/bin/ruff check src tests
	.venv/bin/ruff format --check src tests

fmt:
	.venv/bin/ruff format src tests
	.venv/bin/ruff check --fix src tests

worker:
	.venv/bin/cf worker

bot:
	.venv/bin/cf bot
