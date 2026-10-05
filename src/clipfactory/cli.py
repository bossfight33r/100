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


def _app():
    from clipfactory.config import ConfigError, Settings
    from clipfactory.services import App

    try:
        return App(Settings())
    except ConfigError as e:
        _fail(str(e))


def _print_summary(app_, job_id: str) -> None:
    summary = app_.job_summary(job_id)
    if _state["json"]:
        _out(summary)
        return
    job = summary["job"]
    lines = [f"Job {job['id']}  [{job['status']}]  кампания={job['campaign_id']}"]
    if job["status"] == "failed":
        lines.append(
            f"  упал этап: {job['failed_stage']}  ({job['error_type']}, retryable={job['retryable']})"
        )
        lines.append(f"  {job['error_message'][:500]}")
    for c in summary["clips"]:
        lines.append(
            f"  {c['clip_id']}  {c['start']:.1f}-{c['end']:.1f}s  score={c['score']}  "
            f"[{c['status']}]  {c['hook'][:60]}"
        )
    lines.append(f"  артефакты: {summary['dir']}")
    typer.echo("\n".join(lines))


@app.command()
def run(
    source: Annotated[str, typer.Argument(help="Локальный файл или URL")],
    campaign: Annotated[str, typer.Option("--campaign", "-c", help="id кампании")],
) -> None:
    """Синхронно выполнить весь pipeline для SOURCE."""
    from clipfactory.pipeline.orchestrator import JobFailed

    app_ = _app()
    job = app_.create_job(source, campaign)
    if not _state["json"]:
        typer.echo(f"Job {job.id} создан, запускаю pipeline...")
    try:
        app_.run_job(job.id)
    except JobFailed:
        _print_summary(app_, job.id)
        raise typer.Exit(1) from None
    _print_summary(app_, job.id)


@app.command()
def status(job_id: Annotated[str, typer.Argument(help="id job")]) -> None:
    """Статус job и его клипов."""
    from clipfactory.db import NotFound

    app_ = _app()
    try:
        _print_summary(app_, job_id)
    except NotFound as e:
        _fail(str(e))


if __name__ == "__main__":  # pragma: no cover
    app()
