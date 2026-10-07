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
        f"H.264 рабочие: {', '.join(caps.working_encoders) or 'НЕТ'}",
        f"Транскриберы:  {', '.join(caps.available_transcribers)}",
        f"Теги:          {', '.join(caps.tags)}",
    ]
    typer.echo("\n".join(lines))
    if not (caps.has_ffmpeg and caps.has_ffprobe):
        sys.exit(1)


def parse_interval(value: str) -> int:
    """'30m' / '6h' / '1d' / '3600' -> секунды (минимум 5 минут — бережём квоту API)."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    value = value.strip().lower()
    try:
        seconds = int(value[:-1]) * units[value[-1]] if value[-1] in units else int(value)
    except (ValueError, IndexError):
        raise typer.BadParameter(f"bad interval {value!r}; use 30m, 6h, 1d") from None
    if seconds < 300:
        raise typer.BadParameter("interval must be at least 5 minutes")
    return seconds


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
    runs = summary.get("stage_runs") or []
    if runs:
        last = {}
        for r in runs:
            last[r["stage"]] = r
        lines.append(
            "  этапы: "
            + ", ".join(f"{k}={'cache' if v['cached'] else v['status']}" for k, v in last.items())
        )
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


ForceStage = Annotated[
    str | None,
    typer.Option("--force-stage", help="Перезапустить этап и все последующие (ingest…render)"),
]
NoCache = Annotated[bool, typer.Option("--no-cache", help="Игнорировать кеш всех этапов")]


def _stage(value: str | None):
    from clipfactory.schemas import StageName

    if value is None:
        return None
    try:
        return StageName(value)
    except ValueError:
        _fail(f"unknown stage {value!r}; one of: {', '.join(s.value for s in StageName)}")


@app.command()
def run(
    source: Annotated[str, typer.Argument(help="Локальный файл или URL")],
    campaign: Annotated[str, typer.Option("--campaign", "-c", help="id кампании")],
    force_stage: ForceStage = None,
    no_cache: NoCache = False,
) -> None:
    """Синхронно выполнить весь pipeline для SOURCE."""
    from clipfactory.config import ConfigError
    from clipfactory.pipeline.orchestrator import JobFailed

    app_ = _app()
    try:
        job = app_.create_job(source, campaign)
    except ConfigError as e:
        _fail(str(e))
    if not _state["json"]:
        typer.echo(f"Job {job.id} создан, запускаю pipeline...")
    try:
        app_.run_job(job.id, force_stage=_stage(force_stage), no_cache=no_cache)
    except JobFailed:
        _print_summary(app_, job.id)
        raise typer.Exit(1) from None
    _print_summary(app_, job.id)


@app.command()
def enqueue(
    source: Annotated[str, typer.Argument(help="Локальный файл или URL")],
    campaign: Annotated[str, typer.Option("--campaign", "-c", help="id кампании")],
    no_cache: NoCache = False,
) -> None:
    """Создать job и поставить в очередь (CF_QUEUE=rq — выполнит `cf worker`)."""
    from clipfactory.config import ConfigError

    app_ = _app()
    try:
        job = app_.create_job(source, campaign)
    except ConfigError as e:
        _fail(str(e))
    task_id = app_.enqueue_job(job.id, no_cache=no_cache)
    _out({"job_id": job.id, "task_id": task_id}, f"Job {job.id} поставлен в очередь ({task_id})")


def _fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "  ?  "
    m, s = divmod(int(sec), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m}:{s:02d}"


def _fmt_views(n: int | None) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.0f}K" if n >= 1000 else str(n)


@app.command()
def discover(
    url: Annotated[str, typer.Argument(help="Канал, вкладка канала или плейлист")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Сколько видео смотреть")] = 30,
    min_minutes: Annotated[float, typer.Option("--min-minutes")] = 5,
    max_minutes: Annotated[float, typer.Option("--max-minutes")] = 240,
    heatmap: Annotated[
        bool, typer.Option("--heatmap", help="Проверить «Most replayed» (запрос на каждое видео)")
    ] = False,
    include_processed: Annotated[
        bool, typer.Option("--all", help="Показывать и уже обработанные")
    ] = False,
    enqueue_top: Annotated[
        int, typer.Option("--enqueue", help="Поставить в очередь N лучших (нужен --campaign)")
    ] = 0,
    campaign: Annotated[str | None, typer.Option("--campaign", "-c")] = None,
) -> None:
    """Найти исходники: видео канала/плейлиста по просмотрам, без скачивания."""
    from clipfactory.backends.downloader import DownloadError
    from clipfactory.backends.downloader.discover import list_videos_ytdlp, probe_video_ytdlp
    from clipfactory.config import ConfigError
    from clipfactory.discover import discover as run_discover
    from clipfactory.pipeline.orchestrator import JobFailed

    if enqueue_top and not campaign:
        _fail("--enqueue требует --campaign")
    app_ = _app()
    if campaign:
        try:
            app_.settings.campaign(campaign)
        except ConfigError as e:
            _fail(str(e))
    try:
        found = run_discover(
            url,
            lister=list_videos_ytdlp,
            prober=probe_video_ytdlp if heatmap else None,
            limit=limit,
            min_sec=min_minutes * 60,
            max_sec=max_minutes * 60,
            seen=app_.db.job_sources(),
            include_processed=include_processed,
        )
    except DownloadError as e:
        _fail(str(e))
    queued: list[dict[str, str]] = []
    for cand in found[:enqueue_top]:
        try:
            job = app_.create_job(cand.url, campaign or "")
            queued.append({"url": cand.url, "job_id": job.id})
            app_.enqueue_job(job.id)
        except ConfigError as e:
            _fail(str(e))
        except JobFailed as e:  # InlineQueue выполняет сразу; одна неудача не останавливает
            typer.secho(f"{cand.url}: {e}", fg=typer.colors.YELLOW, err=True)
    if _state["json"]:
        _out({"videos": [c.model_dump() for c in found], "queued": queued})
        return
    if not found:
        typer.echo("Ничего не найдено в заданном диапазоне длительности.")
        return
    for i, c in enumerate(found, 1):
        hm = "" if c.heatmap_points is None else (" 🔥" if c.heatmap_points else " ·")
        mark = " (было)" if c.already_processed else ""
        typer.echo(
            f"{i:>3}. {_fmt_views(c.view_count):>6}  {_fmt_duration(c.duration):>8}{hm}  "
            f"{c.title[:70]}{mark}\n       {c.url}"
        )
    if heatmap:
        typer.echo("\n🔥 — есть кривая «Most replayed», · — нет (останется только звук)")
    for q in queued:
        typer.echo(f"В очереди: {q['job_id']}  {q['url']}")


BLOCKS = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int) -> str:
    """Максимум в каждой из width корзин -> символ высоты 0..8."""
    if not values:
        return ""
    width = min(width, len(values))
    out = []
    for i in range(width):
        a, b = i * len(values) // width, (i + 1) * len(values) // width
        v = max(values[a : max(b, a + 1)])
        out.append(BLOCKS[min(int(round(max(v, 0.0) * 8)), 8)])
    return "".join(out)


@app.command()
def signals(
    job_id: Annotated[str, typer.Argument(help="id job кампании selection: signals")],
    width: Annotated[int, typer.Option("--width", "-w")] = 80,
) -> None:
    """Ряды сигналов select (звук, heatmap, чат) и выбранные клипы на одной шкале."""
    from clipfactory.schemas import Highlights, SignalsTrace

    app_ = _app()
    key = f"jobs/{job_id}/signals.json"
    if not app_.storage.exists(key):
        _fail(f"{key} не найден: job не из кампании selection: signals или select ещё не прошёл")
    with app_.storage.open_read(key) as f:
        tr = SignalsTrace.model_validate_json(f.read())
    hl_key = f"jobs/{job_id}/highlights.json"
    clips = []
    if app_.storage.exists(hl_key):
        with app_.storage.open_read(hl_key) as f:
            clips = Highlights.model_validate_json(f.read()).candidates
    if _state["json"]:
        _out({"trace": tr.model_dump(), "clips": [c.model_dump() for c in clips]})
        return
    total = len(tr.fused) * tr.hop
    cols = min(width, len(tr.fused)) or 1
    rows = [(f"{n} ×{tr.weights.get(n, 0):.1f}", v) for n, v in tr.series.items()]
    rows.append(("итог", tr.fused))
    label_w = max(len(r[0]) for r in rows)
    for name, values in rows:
        typer.echo(f"{name:>{label_w}} │{sparkline(values, cols)}")
    marks = [" "] * cols
    for c in clips:
        for i in range(int(c.start / total * cols), min(int(c.end / total * cols) + 1, cols)):
            marks[i] = "━"
        marks[min(int(c.start / total * cols), cols - 1)] = c.id[-1]
    typer.echo(f"{'клипы':>{label_w}} │{''.join(marks)}")
    typer.echo(f"{'':>{label_w}} └0{_fmt_duration(total):>{cols}}")
    for c in clips:
        typer.echo(
            f"{c.id}  {_fmt_duration(c.start)}–{_fmt_duration(c.end)}  score {c.score:>3}  {c.reason}"
        )


@app.command()
def retry(
    job_id: Annotated[str, typer.Argument(help="id job")],
    force_stage: ForceStage = None,
) -> None:
    """Повторить job с первого невалидного этапа (валидный кеш не пересчитывается)."""
    from clipfactory.db import NotFound

    app_ = _app()
    try:
        app_.retry_job(job_id, force_stage=_stage(force_stage))
    except (NotFound, ValueError) as e:
        _fail(str(e))
    _print_summary(app_, job_id)
    if app_.db.get_job(job_id).status.value == "failed":
        raise typer.Exit(1)


@app.command()
def cancel(job_id: Annotated[str, typer.Argument(help="id job")]) -> None:
    """Отменить job: снять из очереди или остановить на ближайшей точке (ffmpeg прерывается)."""
    from clipfactory.db import NotFound

    app_ = _app()
    try:
        result = app_.cancel_job(job_id)
    except (NotFound, ValueError) as e:
        _fail(str(e))
    human = {
        "dequeued": f"Job {job_id} снят из очереди.",
        "requested": f"Отмена {job_id} запрошена — остановится в течение пары секунд.",
    }[result]
    _out({"job_id": job_id, "result": result}, human)


@app.command()
def worker(
    burst: Annotated[bool, typer.Option("--burst", help="Выйти, когда очередь пуста")] = False,
) -> None:
    """RQ-воркер: восстанавливает потерянные job из SQLite и обрабатывает очередь."""
    from clipfactory.worker import run_worker

    app_ = _app()
    if app_.settings.queue != "rq":
        typer.secho(
            "CF_QUEUE != rq: воркер всё равно слушает Redis", fg=typer.colors.YELLOW, err=True
        )
    try:
        run_worker(app_, burst=burst)
    except Exception as e:  # redis недоступен и т.п.
        _fail(f"worker stopped: {e}")


@app.command()
def status(job_id: Annotated[str, typer.Argument(help="id job")]) -> None:
    """Статус job и его клипов."""
    from clipfactory.db import NotFound

    app_ = _app()
    try:
        _print_summary(app_, job_id)
    except NotFound as e:
        _fail(str(e))


auth_app = typer.Typer(help="OAuth для собственных аккаунтов")
app.add_typer(auth_app, name="auth")


@auth_app.command("youtube")
def auth_youtube(
    account: Annotated[str, typer.Option("--account", "-a", help="id аккаунта из accounts.yaml")],
) -> None:
    """OAuth для своего YouTube-канала: откроет браузер, токен -> data/secrets/{token_ref}.json."""
    from clipfactory.publish.base import PublishError
    from clipfactory.publish.youtube import authorize

    app_ = _app()
    acc = app_.settings.accounts.get(account)
    if acc is None:
        _fail(f"unknown account {account!r}")
    try:
        path = authorize(acc, app_.settings.youtube_client_secrets, app_.settings.secrets_dir)
    except PublishError as e:
        _fail(str(e))
    _out({"account": account, "token_file": str(path)}, f"Готово: токен сохранён в {path} (600)")


@app.command()
def publish(
    job_id: Annotated[str, typer.Argument(help="id job")],
    schedule_only: Annotated[
        bool, typer.Option("--schedule-only", help="Только распределить по слотам, не загружать")
    ] = False,
    retry_failed: Annotated[
        bool, typer.Option("--retry-failed", help="Повторить упавшие публикации одобренных клипов")
    ] = False,
) -> None:
    """Запланировать одобренные клипы по слотам аккаунтов и опубликовать/экспортировать."""
    from clipfactory.pipeline.publish import PublishService, PublishServiceError
    from clipfactory.publish.scheduler import SchedulingError

    app_ = _app()
    svc = PublishService(app_)
    try:
        if retry_failed:
            svc.retry_failed(job_id)
        svc.schedule_job(job_id)
        if not schedule_only:
            svc.publish_job(job_id)
    except (PublishServiceError, SchedulingError) as e:
        _fail(str(e))
    pubs = app_.db.list_publications(job_id=job_id)
    if _state["json"]:
        _out([p.model_dump(mode="json") for p in pubs])
        return
    for p in pubs:
        when = p.scheduled_at.isoformat(timespec="minutes") if p.scheduled_at else "-"
        typer.echo(
            f"{p.clip_id} -> {p.account_id} ({p.platform.value})  {when}  [{p.status.value}]  "
            f"{p.url or p.error or ''}"
        )


@app.command()
def track(
    manual: Annotated[
        str | None, typer.Option("--manual", help="id публикации для ручного ввода")
    ] = None,
    views: Annotated[int | None, typer.Option("--views", min=0)] = None,
    likes: Annotated[int, typer.Option("--likes", min=0)] = 0,
    comments: Annotated[int, typer.Option("--comments", min=0)] = 0,
    every: Annotated[
        str | None, typer.Option("--every", help="Собирать периодически: 30m, 6h, 1d")
    ] = None,
) -> None:
    """Собрать статистику YouTube (или --manual PUB_ID --views N для других платформ)."""
    from clipfactory.db import NotFound
    from clipfactory.track.collector import TrackError, add_manual, collect_youtube

    app_ = _app()
    if manual:
        if views is None:
            _fail("--views is required with --manual")
        try:
            snap = add_manual(app_, manual, views=views, likes=likes, comments=comments)
        except (NotFound, TrackError) as e:
            _fail(str(e))
        _out(snap.model_dump(mode="json"), f"Записано: {manual} — {views} просмотров")
        return
    if every is None:
        snaps = collect_youtube(app_)
        _out([s.model_dump(mode="json") for s in snaps], f"Собрано снимков: {len(snaps)}")
        return
    import time

    interval = parse_interval(every)
    typer.echo(f"Сбор статистики каждые {every}; Ctrl+C — стоп")
    try:
        while True:
            stamp = time.strftime("%Y-%m-%d %H:%M")
            try:
                snaps = collect_youtube(app_)
            except Exception as e:  # сбой одного прохода (DB занята и т.п.) не останавливает сбор
                typer.echo(f"{stamp} ошибка: {e}", err=True)
            else:
                typer.echo(f"{stamp} снимков: {len(snaps)}")
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("Остановлено.")


@app.command()
def report(
    campaign: Annotated[str | None, typer.Option("--campaign", "-c")] = None,
    top: Annotated[int, typer.Option("--top", min=1)] = 10,
    recommendations: Annotated[
        bool, typer.Option("--recommendations", help="Записать файл рекомендаций к промпту")
    ] = False,
) -> None:
    """Доход и статистика по кампаниям, аккаунтам, топ-клипам и хукам."""
    from clipfactory.track.report import build_report, render_text, write_prompt_recommendations

    app_ = _app()
    r = build_report(app_, campaign_id=campaign, top=top)
    path = write_prompt_recommendations(app_, r, campaign_id=campaign) if recommendations else None
    if _state["json"]:
        data = r.model_dump(mode="json")
        data["recommendations_file"] = str(path) if path else None
        _out(data)
        return
    typer.echo(render_text(r))
    if path:
        typer.echo(f"\nРекомендации к промпту: {path}")


@app.command()
def serve(
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
    host: Annotated[
        str, typer.Option("--host", help="Только 127.0.0.1, пока нет TLS")
    ] = "127.0.0.1",
) -> None:
    """Локальный HTTP-API для десктоп-приложения (токен — CF_API_TOKEN, иначе генерируется)."""
    import os
    import secrets

    import uvicorn

    from clipfactory.api.app import create_app

    token = os.environ.get("CF_API_TOKEN") or secrets.token_urlsafe(32)
    generated = "CF_API_TOKEN" not in os.environ
    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(
            f"ВНИМАНИЕ: {host} открывает API по сети без TLS; токен пойдёт открытым текстом",
            fg=typer.colors.YELLOW, err=True,
        )  # fmt: skip
    if generated:
        typer.echo(f"CF_API_TOKEN={token}", err=True)  # единственный раз; клиент читает со stderr
    uvicorn.run(
        create_app(_app(), token), host=host, port=port, access_log=False, log_level="warning"
    )


@app.command()
def bot() -> None:
    """Telegram-бот (control plane). Нужны TELEGRAM_BOT_TOKEN и CF_ADMIN_IDS."""
    from clipfactory.bot.main import BotConfigError, main

    app_ = _app()
    try:
        main(app_)
    except BotConfigError as e:
        _fail(str(e))


@app.command()
def review(
    job_id: Annotated[str, typer.Argument(help="id job")],
    clip_id: Annotated[str, typer.Argument(help="id клипа, например c01")],
    action: Annotated[str, typer.Argument(help="approve | reject | edit | captions | crop")],
    title: Annotated[str | None, typer.Option("--title")] = None,
    description: Annotated[str | None, typer.Option("--description")] = None,
    hashtags: Annotated[str | None, typer.Option("--hashtags", help="через пробел")] = None,
    platform: Annotated[
        str | None, typer.Option("--platform", help="youtube|tiktok|instagram")
    ] = None,
    center: Annotated[
        str | None, typer.Option("--center", help="crop: центр кадра 0–100 (%) или auto")
    ] = None,
    reason: Annotated[str, typer.Option("--reason", help="reject: причина")] = "",
) -> None:
    """Ревью из терминала: одобрить, отклонить, править метаданные, перерендерить."""
    from clipfactory.pipeline.orchestrator import JobFailed
    from clipfactory.pipeline.review import ReviewError, ReviewService
    from clipfactory.schemas import Platform

    app_ = _app()
    svc = ReviewService(app_)
    try:
        if action == "approve":
            svc.approve(job_id, clip_id)
        elif action == "reject":
            svc.reject(job_id, clip_id, reason=reason)
        elif action == "edit":
            if title is None and description is None and hashtags is None:
                _fail("edit needs --title, --description or --hashtags")
            svc.edit_metadata(
                job_id, clip_id, title=title, description=description,
                hashtags=hashtags.split() if hashtags is not None else None,
                platform=Platform(platform) if platform else None,
            )  # fmt: skip
        elif action == "captions":
            svc.rerender_captions(job_id, clip_id)
        elif action == "crop":
            value = None if center in (None, "auto") else float(center) / 100
            svc.rerender_crop(job_id, clip_id, center_x=value)
        else:
            _fail("action must be approve, reject, edit, captions or crop")
    except (ReviewError, ValueError) as e:
        _fail(str(e))
    except JobFailed:
        pass  # статус и ошибка уже в job — покажем ниже
    _print_summary(app_, job_id)


if __name__ == "__main__":  # pragma: no cover
    app()
