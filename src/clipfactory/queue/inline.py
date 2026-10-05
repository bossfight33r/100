from __future__ import annotations

from collections.abc import Callable

from clipfactory.log import get_logger
from clipfactory.queue.base import Task, TaskStatus

log = get_logger(__name__)


class InlineQueue:
    """Выполняет задачу сразу в текущем процессе (dev/test)."""

    def __init__(self, executor: Callable[[Task], object]) -> None:
        self._executor = executor
        self._status: dict[str, TaskStatus] = {}
        self.errors: dict[str, BaseException] = {}

    def enqueue(self, task: Task) -> str:
        self._status[task.id] = TaskStatus.running
        try:
            self._executor(task)
        except Exception as e:  # ошибка уже записана в job; очередь лишь фиксирует статус
            self._status[task.id] = TaskStatus.failed
            self.errors[task.id] = e
            log.warning("queue.inline.task_failed", task_id=task.id, kind=task.kind, error=str(e))
        else:
            self._status[task.id] = TaskStatus.finished
        return task.id

    def cancel(self, task_id: str) -> bool:
        return False

    def status(self, task_id: str) -> TaskStatus:
        return self._status.get(task_id, TaskStatus.unknown)
