"""Построение filtergraph-строк. Здесь нет вызовов процессов."""

from __future__ import annotations

from collections.abc import Sequence

from clipfactory.schemas import CropKeyframe

LOUDNORM_I, LOUDNORM_TP, LOUDNORM_LRA = -14.0, -2.0, 11.0  # TP -2: запас под перерегулировку AAC
LOUDNORM_SINGLE_PASS = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"


def loudnorm_measure_filter() -> str:
    return f"{LOUDNORM_SINGLE_PASS}:print_format=json"


def loudnorm_apply_filter(m: dict[str, float]) -> str:
    """Второй проход loudnorm по измеренным значениям: линейное усиление, без «дыхания»."""
    return (
        f"{LOUDNORM_SINGLE_PASS}:measured_I={m['input_i']:.2f}:measured_LRA={m['input_lra']:.2f}"
        f":measured_TP={m['input_tp']:.2f}:measured_thresh={m['input_thresh']:.2f}"
        f":offset={m['target_offset']:.2f}:linear=true"
    )


def parse_loudnorm_json(stderr_text: str) -> dict[str, float] | None:
    """Достать блок JSON из stderr первого прохода; None — тишина/нечитаемо (-inf)."""
    import json
    import math

    start, end = stderr_text.rfind("{"), stderr_text.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        raw = json.loads(stderr_text[start : end + 1])
        values = {
            k: float(raw[k])
            for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        }
    except (ValueError, KeyError, TypeError):
        return None
    return values if all(math.isfinite(v) for v in values.values()) else None


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
    """Экранирование значения опции фильтра для -filter_complex (два уровня ffmpeg).

    1) уровень опции фильтра: \\ ' : ; 2) уровень filtergraph: \\ ' [ ] , ;
    """
    level1 = "".join("\\" + ch if ch in "\\':" else ch for ch in value)
    return "".join("\\" + ch if ch in "\\'[],;" else ch for ch in level1)


def render_filtergraph(
    *,
    keyframes: Sequence[CropKeyframe],
    target_width: int,
    target_height: int,
    fps: int,
    ass_file: str | None,
    fonts_dir: str | None,
    has_audio: bool,
    loudnorm: str | None = None,
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
        graph += f";[0:a]{loudnorm or LOUDNORM_SINGLE_PASS},aresample=48000[a]"
    return graph
