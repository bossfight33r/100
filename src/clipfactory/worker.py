"""Исполнение задач очереди и восстановление после падения воркера/Redis."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from clipfactory.compute.capabilities import TaskRequirements
from clipfactory.log import get_logger
from clipfactory.queue.base import Queue, Task, TaskStatus
from clipfactory.schemas import JobStatus

log = get_logger(__name__)

ACTIVE_STATUSES = [
    JobStatus.ingesting,
    JobStatus.transcribing,
    JobStatus.selecting,
    JobStatus.reframing,
    JobStatus.captioning,
    JobStatus.rendering,
]


def run_job_task_id(job_id: str) -> str:
    return f"run-{job_id}"


def make_run_task(
    job_id: str,
    *,
    force_stage: str | None = None,
    no_cache: bool = False,
    requirements: TaskRequirements | None = None,
) -> Task:
    return Task(
        id=run_job_task_id(job_id),
        kind="run_job",
        payload={"job_id": job_id, "force_stage": force_stage, "no_cache": no_cache},
        requirements=requirements or TaskRequirements(),
    )


def handle_run_job(app: Any, payload: dict[str, Any]) -> None:
    from clipfactory.schemas import StageName

    force = payload.get("force_stage")
    app.run_job(
        payload["job_id"],
        force_stage=StageName(force) if force else None,
        no_cache=bool(payload.get("no_cache")),
    )


HANDLERS: dict[str, Callable[[Any, dict[str, Any]], None]] = {"run_job": handle_run_job}


def dispatch(app: Any, task: Task) -> None:
    try:
        handler = HANDLERS[task.kind]
    except KeyError:
        raise ValueError(f"unknown task kind {task.kind!r}") from None
    log.info("task.start", task_id=task.id, kind=task.kind)
    handler(app, task.payload)


def execute_task(task_data: dict[str, Any]) -> None:
    """Точка входа RQ: строит App из окружения воркера и выполняет задачу."""
    from clipfactory.config import Settings
    from clipfactory.services import App

    dispatch(App(Settings()), Task.model_validate(task_data))


def recover(app: Any, queue: Queue) -> list[str]:
    """Перепоставить job, потерянные из-за падения воркера или Redis.

    Источник правды — SQLite: job в активном статусе или queued, для которого нет
    живой задачи в очереди, ставится снова. Этапы продолжат с первого невалидного
    манифеста, поэтому повтор безопасен.
    """
    requeued = []
    for job in app.db.list_jobs([JobStatus.queued, *ACTIVE_STATUSES], limit=1000):
        task_id = run_job_task_id(job.id)
        if queue.status(task_id) in (TaskStatus.queued, TaskStatus.running):
            continue  # задача жива (мёртвые started-задачи RQ чистит cleanup() до recover)
        app.db.abandon_stage_runs(job.id)
        queue.enqueue(make_run_task(job.id, requirements=app.job_requirements(job.id)))
        requeued.append(job.id)
        log.warning("worker.recovered_job", job_id=job.id, previous_status=job.status.value)
    return requeued


def run_worker(app: Any, *, burst: bool = False) -> None:  # pragma: no cover - нужен Redis
    """Запустить RQ-воркер (SimpleWorker: без fork — безопасно на macOS)."""
    from rq import SimpleWorker
    from rq.registry import StartedJobRegistry

    from clipfactory.compute.capabilities import detect
    from clipfactory.compute.routing import (
        MAX_TAGS,
        Heartbeat,
        derive_tags,
        queues_for_worker,
        worker_name,
    )
    from clipfactory.queue.rq import RQQueue

    queue = RQQueue(app.settings.redis_url)
    caps = detect()
    # heartbeat и очереди — из одного списка тегов, иначе eligible_workers видит воркер
    # (например, по os:/arch:), а очередь job никто не слушает
    tags = sorted({*caps.tags, *derive_tags(caps, app.settings.worker_tags)})
    if len(tags) > MAX_TAGS:
        log.warning("worker.tags_truncated", dropped=tags[MAX_TAGS:])
        tags = tags[:MAX_TAGS]
    caps.tags = tags
    names = queues_for_worker(tags, queue.base)
    rq_queues = [queue.queue(n) for n in names]
    for q in rq_queues:
        StartedJobRegistry(queue=q).cleanup()
    recovered = recover(app, queue)
    log.info(
        "worker.start",
        recovered=len(recovered),
        queues=names,
        redis=app.settings.redis_url.split("@")[-1],
    )
    with Heartbeat(queue.connection, worker_name(), caps):
        SimpleWorker(rq_queues, connection=queue.connection).work(burst=burst, with_scheduler=False)
