from __future__ import annotations

from pathlib import Path

from clipfactory.media import ffmpeg
from clipfactory.media.models import AudioStream, MediaInfo, VideoStream


class ProbeError(Exception):
    pass


def _fps(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        return float(num) / float(den) if float(den) else 0.0
    return float(rate)


def _rotation(stream: dict) -> int:
    rot = stream.get("tags", {}).get("rotate")
    if rot is None:
        for sd in stream.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = sd["rotation"]
    try:
        return int(float(rot or 0)) % 360
    except (TypeError, ValueError):
        return 0


def parse_probe(data: dict) -> MediaInfo:
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = audio = None
    for s in streams:
        kind = s.get("codec_type")
        if kind == "video" and video is None and s.get("disposition", {}).get("attached_pic") != 1:
            video = VideoStream(
                codec=s.get("codec_name", "unknown"),
                width=int(s.get("width") or 0),
                height=int(s.get("height") or 0),
                fps=_fps(s.get("avg_frame_rate")) or _fps(s.get("r_frame_rate")),
                pix_fmt=s.get("pix_fmt"),
                rotation=_rotation(s),
            )
        elif kind == "audio" and audio is None:
            audio = AudioStream(
                codec=s.get("codec_name", "unknown"),
                sample_rate=int(s.get("sample_rate") or 0),
                channels=int(s.get("channels") or 0),
            )
    duration = fmt.get("duration")
    if duration is None:
        durations = [float(s["duration"]) for s in streams if s.get("duration")]
        duration = max(durations) if durations else 0.0
    size = fmt.get("size")
    return MediaInfo(
        format_name=fmt.get("format_name", "unknown"),
        duration=float(duration),
        size=int(size) if size is not None else None,
        video=video,
        audio=audio,
    )


def probe(path: Path) -> MediaInfo:
    try:
        return parse_probe(ffmpeg.ffprobe_json(Path(path)))
    except ffmpeg.FFmpegError as e:
        raise ProbeError(f"cannot probe {path}: {e}") from e
