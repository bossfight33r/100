from __future__ import annotations

from typing import Any

from clipfactory.queue.base import Task, TaskStatus

QUEUE_NAME = "clipfactory"
EXECUTOR = "clipfactory.worker.execute_task"

_STATUS = {
    "queued": TaskStatus.queued,
    "deferred": TaskStatus.queued,
    "scheduled": TaskStatus.queued,
    "started": TaskStatus.running,
    "finished": TaskStatus.finished,
    "failed": TaskStatus.failed,
    "stopped": TaskStatus.cancelled,
    "canceled": TaskStatus.cancelled,
}


def _status_value(job: Any) -> str:
    status = job.get_status()
    return str(getattr(status, "value", status))


class RQQueue:
    """Очередь на Redis/RQ. Redis — не source of truth: состояние восстанавливается из SQLite."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        *,
        connection: Any = None,
        name: str = QUEUE_NAME,
        job_timeout: int = 6 * 3600,
    ) -> None:
        import redis
        from rq import Queue as _RQ

        self.connection = connection or redis.Redis.from_url(redis_url)
        self._q = _RQ(name, connection=self.connection)
        self.job_timeout = job_timeout

    @property
    def rq_queue(self) -> Any:
        return self._q

    def enqueue(self, task: Task) -> str:
        self._q.enqueue(
            EXECUTOR,
            task.model_dump(mode="json"),
            job_id=task.id,
            job_timeout=self.job_timeout,
            result_ttl=24 * 3600,
            failure_ttl=7 * 24 * 3600,
        )
        return task.id

    def _job(self, task_id: str) -> Any:
        from rq.exceptions import NoSuchJobError
        from rq.job import Job

        try:
            return Job.fetch(task_id, connection=self.connection)
        except NoSuchJobError:
            return None

    def cancel(self, task_id: str) -> bool:
        job = self._job(task_id)
        if job is None or _status_value(job) not in ("queued", "deferred", "scheduled"):
            return False
        job.cancel()
        return True

    def status(self, task_id: str) -> TaskStatus:
        job = self._job(task_id)
        if job is None:
            return TaskStatus.unknown
        return _STATUS.get(_status_value(job), TaskStatus.unknown)
