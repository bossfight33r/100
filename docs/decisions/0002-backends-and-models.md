# ADR-0002: Бэкенды по умолчанию

**Решение.**
- LLM: Anthropic SDK, модель `claude-opus-5-5` (env `CF_LLM_MODEL`), effort `medium` (`CF_LLM_EFFORT`), стриминг + `get_final_message()` (чанки транскрипта по 20 мин — длинный вход), server-side fallback `fallbacks="default"` (beta `server-side-fallback-2026-07-01`) на случай отказа классификатора. Ошибки SDK мапятся в `LLMError(retryable=...)`.
- Лица: MediaPipe **Tasks** `FaceDetector` + модель `blaze_face_short_range.tflite` (скачивается `make face-model` в `data/models/`). В mediapipe 1.x устаревший `mp.solutions` удалён, поэтому только Tasks API.
- Транскрипция: `auto` = mlx-whisper на macOS arm64 при наличии пакета, иначе faster-whisper CPU int8. Модель `large-v3-turbo`.
- Кадры для анализа читаются из ffmpeg rawvideo-пайпа (`media/ffmpeg.iter_frames`), без OpenCV.

**Почему.** Минимум тяжёлых зависимостей (без torch), всё работает на M1 без CUDA, модель LLM меняется без кода.

**Отброшено.** OpenCV Haar (хуже качество), MTCNN/pyannote (torch, HF-токен), sentence-transformers TextTiling (смысловые границы не равны вирусности).
