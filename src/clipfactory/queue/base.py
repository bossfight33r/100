from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from clipfactory.compute.capabilities import TaskRequirements


class TaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    finished = "finished"
    failed = "failed"
    cancelled = "cancelled"
    unknown = "unknown"


class Task(BaseModel):
    """Единица работы для очереди. ``kind`` — имя обработчика из worker.HANDLERS."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    requirements: TaskRequirements = Field(default_factory=TaskRequirements)


@runtime_checkable
class Queue(Protocol):
    def enqueue(self, task: Task) -> str: ...
    def cancel(self, task_id: str) -> bool: ...
    def status(self, task_id: str) -> TaskStatus: ...
