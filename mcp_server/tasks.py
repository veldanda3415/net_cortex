"""In-memory Task Store for long-running RCA operations.

Tasks follow the MCP 2026-07-28 Tasks extension lifecycle:
  running → completed | failed | cancelled

Single-replica only. For multi-replica, replace with Redis-backed store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class TaskStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskProgress:
    stage: str
    percent: float
    message: str = ""
    updated_at: float = field(default_factory=time.time)


@dataclass
class TaskRecord:
    task_id: str
    incident_id: str
    status: TaskStatus
    created_at: datetime
    progress: TaskProgress | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    completed_at: datetime | None = None


class TaskStore:
    """In-memory task state for RCA runs."""

    def __init__(self, ttl_seconds: int = 3600, max_concurrent: int = 10) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._incident_to_task: dict[str, str] = {}
        self._ttl_seconds = ttl_seconds
        self._max_concurrent = max_concurrent

    def create_task(self, incident_id: str) -> str:
        """Create a new task for an RCA run. Returns task_id."""
        running_count = sum(
            1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING
        )
        if running_count >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. Try again later."
            )

        task_id = f"task_{uuid4().hex[:12]}"
        record = TaskRecord(
            task_id=task_id,
            incident_id=incident_id,
            status=TaskStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
        self._tasks[task_id] = record
        self._incident_to_task[incident_id] = task_id
        return task_id

    def update_progress(self, task_id: str, stage: str, percent: float, message: str = "") -> None:
        """Update task progress."""
        record = self._tasks.get(task_id)
        if record and record.status == TaskStatus.RUNNING:
            record.progress = TaskProgress(stage=stage, percent=percent, message=message)

    def complete_task(self, task_id: str, result: dict[str, Any]) -> None:
        """Mark task as completed with result. No-op if the task already
        reached a terminal state (e.g. cancelled) — a late completion must
        not overwrite that."""
        record = self._tasks.get(task_id)
        if record and record.status == TaskStatus.RUNNING:
            record.status = TaskStatus.COMPLETED
            record.result = result
            record.completed_at = datetime.now(timezone.utc)

    def fail_task(self, task_id: str, error: str) -> None:
        """Mark task as failed. No-op if the task already reached a terminal
        state — see complete_task."""
        record = self._tasks.get(task_id)
        if record and record.status == TaskStatus.RUNNING:
            record.status = TaskStatus.FAILED
            record.error = error
            record.completed_at = datetime.now(timezone.utc)

    def cancel_task(self, task_id: str) -> bool:
        """Attempt to cancel a task. Returns True if cancellation was accepted."""
        record = self._tasks.get(task_id)
        if record and record.status == TaskStatus.RUNNING:
            record.status = TaskStatus.CANCELLED
            record.completed_at = datetime.now(timezone.utc)
            return True
        return False

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Get task by task_id."""
        return self._tasks.get(task_id)

    def get_task_by_incident(self, incident_id: str) -> TaskRecord | None:
        """Get task by incident_id."""
        task_id = self._incident_to_task.get(incident_id)
        if task_id:
            return self._tasks.get(task_id)
        return None

    def cleanup_expired(self) -> int:
        """Remove completed/failed tasks older than TTL. Returns count removed."""
        now = time.time()
        expired = []
        for task_id, record in self._tasks.items():
            if record.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                if record.completed_at:
                    age = now - record.completed_at.timestamp()
                    if age > self._ttl_seconds:
                        expired.append(task_id)
        for task_id in expired:
            record = self._tasks.pop(task_id)
            self._incident_to_task.pop(record.incident_id, None)
        return len(expired)
