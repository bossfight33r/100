"""Построение filtergraph-строк. Здесь нет вызовов процессов."""

from __future__ import annotations

from collections.abc import Sequence

from clipfactory.schemas import CropKeyframe


def scene_filter(threshold: float) -> str:
    return f"scale=320:-2,select='gt(scene,{threshold:.3f})',metadata=print:file=-"


def piecewise_expr(keyframes: Sequence[CropKeyframe], attr: str) -> str:
    """Кусочно-постоянное выражение по времени t: if(lt(t,T1),V0,if(lt(t,T2),V1,V2)).

    Запятые экранируются, т.к. выражение идёт внутри filtergraph.
    """
    values = [getattr(k, attr) for k in keyframes]
    expr = str(values[-1])
    for k_next, v in zip(reversed(keyframes[1:]), reversed(values[:-1]), strict=True):
        expr = f"if(lt(t\\,{k_next.t:.3f})\\,{v}\\,{expr})"
    return expr


def crop_filter(keyframes: Sequence[CropKeyframe]) -> str:
    w, h = keyframes[0].w, keyframes[0].h
    if any(k.w != w or k.h != h for k in keyframes):
        raise ValueError("crop size must be constant across keyframes")
    x = piecewise_expr(keyframes, "x")
    y = piecewise_expr(keyframes, "y")
    return f"crop=w={w}:h={h}:x={x}:y={y}:exact=1"


def escape_filter_value(value: str) -> str:
    """Экранирование значения опции фильтра (путь к файлу и т.п.)."""
    for ch in ("\\", "'", ":", ",", "[", "]", ";"):
        value = value.replace(ch, "\\" + ch)
    return value


def render_filtergraph(
    *,
    keyframes: Sequence[CropKeyframe],
    target_width: int,
    target_height: int,
    fps: int,
    ass_file: str | None,
    fonts_dir: str | None,
    has_audio: bool,
) -> str:
    video = [
        crop_filter(keyframes),
        f"scale={target_width}:{target_height}:flags=lanczos",
        "setsar=1",
        f"fps={fps}",
    ]
    if ass_file:
        ass = f"ass=filename={escape_filter_value(ass_file)}"
        if fonts_dir:
            ass += f":fontsdir={escape_filter_value(fonts_dir)}"
        video.append(ass)
    video.append("format=yuv420p")
    graph = f"[0:v]{','.join(video)}[v]"
    if has_audio:
        graph += ";[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
    return graph
