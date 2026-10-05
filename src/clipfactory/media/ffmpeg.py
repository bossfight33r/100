"""Единственная точка вызова ffmpeg/ffprobe в проекте.

argv-список без shell, timeout, отмена через threading.Event, stderr в файл,
структурированный результат. В исключение попадают последние 30 строк stderr.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from clipfactory.log import get_logger, redact_text

log = get_logger(__name__)

STDERR_TAIL_LINES = 30
_POLL_SEC = 0.1


class FFmpegError(Exception):
    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stderr_tail: str = "",
        log_path: Path | None = None,
    ) -> None:
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        self.log_path = log_path
        details = (
            f"\n--- stderr (last {STDERR_TAIL_LINES} lines) ---\n{stderr_tail}"
            if stderr_tail
            else ""
        )
        where = f"\nfull log: {log_path}" if log_path else ""
        super().__init__(f"{message}{details}{where}")


class FFmpegTimeout(FFmpegError):
    pass


class FFmpegCancelled(FFmpegError):
    pass


class FFmpegNotFound(FFmpegError):
    pass


@dataclass(frozen=True)
class FFmpegResult:
    argv: list[str]
    returncode: int
    duration_ms: int
    stdout: bytes
    stderr_path: Path | None


def which(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise FFmpegNotFound(f"{binary} not found on PATH (macOS: brew install ffmpeg)")
    return path


def sanitize_argv(argv: Sequence[str], max_arg_len: int = 200) -> str:
    out = []
    for a in argv:
        a = redact_text(str(a))
        if len(a) > max_arg_len:
            a = a[: max_arg_len - 15] + f"...(+{len(a) - max_arg_len + 15})"
        out.append(a)
    return " ".join(out)


def _tail(path: Path, lines: int = STDERR_TAIL_LINES) -> str:
    try:
        data = path.read_bytes()[-64 * 1024 :]
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return redact_text("\n".join(text.splitlines()[-lines:]))


def run(
    binary: str,
    args: Sequence[str],
    *,
    timeout: float = 3600,
    log_path: Path | None = None,
    cwd: Path | None = None,
    cancel: threading.Event | None = None,
    capture_stdout: bool = False,
) -> FFmpegResult:
    """Запустить ffmpeg/ffprobe. stderr пишется в ``log_path`` (или во временный файл)."""
    argv = [which(binary), *map(str, args)]
    if binary == "ffmpeg" and "-nostdin" not in argv:
        argv.insert(1, "-nostdin")
    own_log = log_path is None
    if own_log:
        fd, tmp = tempfile.mkstemp(prefix="cf-ffmpeg-", suffix=".log")
        import os

        os.close(fd)
        log_path = Path(tmp)
    assert log_path is not None
    log_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    log.debug("ffmpeg.start", binary=binary, cmd=sanitize_argv(argv))
    with log_path.open("wb") as err, tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=out if capture_stdout else subprocess.DEVNULL,
            stderr=err,
            cwd=str(cwd) if cwd else None,
        )
        try:
            while True:
                try:
                    proc.wait(timeout=_POLL_SEC)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise FFmpegCancelled(f"{binary} cancelled", log_path=log_path)
                if time.monotonic() - started > timeout:
                    proc.kill()
                    proc.wait()
                    raise FFmpegTimeout(
                        f"{binary} timed out after {timeout:.0f}s",
                        stderr_tail=_tail(log_path),
                        log_path=log_path,
                    )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        out.seek(0)
        stdout = out.read() if capture_stdout else b""

    duration_ms = int((time.monotonic() - started) * 1000)
    log.debug(
        "ffmpeg.done",
        binary=binary,
        cmd=sanitize_argv(argv),
        duration_ms=duration_ms,
        exit_status=proc.returncode,
    )
    if proc.returncode != 0:
        raise FFmpegError(
            f"{binary} failed with exit code {proc.returncode}: {sanitize_argv(argv)}",
            returncode=proc.returncode,
            stderr_tail=_tail(log_path),
            log_path=log_path,
        )
    if own_log:
        log_path.unlink(missing_ok=True)
        log_path = None
    return FFmpegResult(argv, proc.returncode, duration_ms, stdout, log_path)


def ffmpeg(args: Sequence[str], **kwargs: object) -> FFmpegResult:
    return run("ffmpeg", ["-hide_banner", "-y", *args], **kwargs)  # type: ignore[arg-type]


def ffprobe_json(path: Path, *, timeout: float = 120) -> dict:
    res = run(
        "ffprobe",
        [
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=timeout,
        capture_stdout=True,
    )  # fmt: skip
    try:
        return json.loads(res.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise FFmpegError(f"ffprobe returned invalid JSON for {path}") from e


@lru_cache(maxsize=1)
def list_encoders() -> frozenset[str]:
    res = run("ffmpeg", ["-hide_banner", "-encoders"], timeout=30, capture_stdout=True)
    return frozenset(parse_encoders(res.stdout.decode("utf-8", errors="replace")))


def parse_encoders(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        m = re.match(r"^\s*[VAS][.FSXBD]{5}\s+(\S+)\s", line)
        if m and m.group(1) != "=":
            names.add(m.group(1))
    return names


@lru_cache(maxsize=1)
def version() -> str:
    res = run("ffmpeg", ["-hide_banner", "-version"], timeout=30, capture_stdout=True)
    first = res.stdout.decode("utf-8", errors="replace").splitlines()[:1]
    return first[0] if first else "unknown"


def detect_scenes(
    path: Path,
    *,
    start: float,
    duration: float,
    threshold: float = 0.35,
    timeout: float = 600,
) -> list[float]:
    """Времена смен сцен (сек от ``start``) через фильтр select=scene."""
    from clipfactory.media.filters import scene_filter

    res = ffmpeg(
        [
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path),
            "-an", "-vf", scene_filter(threshold), "-f", "null", "-",
        ],
        timeout=timeout,
        capture_stdout=True,
    )  # fmt: skip
    return parse_scene_times(res.stdout.decode("utf-8", errors="replace"))


def parse_scene_times(text: str) -> list[float]:
    times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", text)]
    return sorted(set(round(t, 3) for t in times))


def iter_frames(
    path: Path,
    *,
    start: float,
    duration: float,
    fps: float,
    width: int,
    height: int,
    timeout: float = 1800,
) -> Iterator[tuple[float, np.ndarray]]:
    """Кадры клипа как RGB numpy (t от начала клипа, frame HxWx3)."""
    argv = [
        which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path),
        "-an", "-vf", f"fps={fps},scale={width}:{height}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]  # fmt: skip
    frame_size = width * height * 3
    started = time.monotonic()
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err
        )
        assert proc.stdout is not None
        idx = 0
        try:
            while True:
                if time.monotonic() - started > timeout:
                    raise FFmpegTimeout(f"frame extraction timed out after {timeout:.0f}s")
                buf = proc.stdout.read(frame_size)
                if len(buf) < frame_size:
                    break
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
                yield idx / fps, frame
                idx += 1
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        if proc.returncode not in (0, -9):
            err.seek(0)
            tail = err.read()[-8192:].decode("utf-8", errors="replace")
            raise FFmpegError(
                f"frame extraction failed ({proc.returncode})",
                returncode=proc.returncode,
                stderr_tail="\n".join(tail.splitlines()[-STDERR_TAIL_LINES:]),
            )
