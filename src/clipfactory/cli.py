"""CLI `cf`. Тонкий слой: вся логика в services/pipeline."""

from __future__ import annotations

import json
import sys
from typing import Annotated

import typer

from clipfactory.log import configure_logging

app = typer.Typer(
    name="cf", help="ClipFactory: длинные видео -> вертикальные клипы.", no_args_is_help=True
)

_state: dict[str, bool] = {"json": False, "verbose": False}


def _out(data: object, human: str | None = None) -> None:
    if _state["json"]:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        typer.echo(
            human
            if human is not None
            else json.dumps(data, ensure_ascii=False, indent=2, default=str)
        )


def _fail(message: str, code: int = 1) -> None:
    if _state["json"]:
        typer.echo(json.dumps({"error": message}, ensure_ascii=False))
    else:
        typer.secho(f"Ошибка: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Подробные логи")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Машиночитаемый вывод")] = False,
) -> None:
    _state["json"], _state["verbose"] = json_output, verbose
    configure_logging(verbose=verbose, json_output=json_output)


@app.command()
def capabilities() -> None:
    """Возможности машины: ffmpeg, ffprobe, энкодеры, платформа, транскриберы."""
    from clipfactory.compute.capabilities import detect

    caps = detect()
    if _state["json"]:
        _out(caps.model_dump())
        return
    lines = [
        f"Платформа:     {caps.os} {caps.arch}",
        f"CPU потоков:   {caps.cpu_threads}",
        f"RAM:           {caps.ram_mb} MB",
        f"ffmpeg:        {'да' if caps.has_ffmpeg else 'НЕТ'}  {caps.ffmpeg_version or ''}",
        f"ffprobe:       {'да' if caps.has_ffprobe else 'НЕТ'}",
        f"VideoToolbox:  {'да' if caps.has_videotoolbox else 'нет'}",
        f"Энкодеры:      {', '.join(caps.available_encoders) or '-'}",
        f"Транскриберы:  {', '.join(caps.available_transcribers)}",
        f"Теги:          {', '.join(caps.tags)}",
    ]
    typer.echo("\n".join(lines))
    if not (caps.has_ffmpeg and caps.has_ffprobe):
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()
