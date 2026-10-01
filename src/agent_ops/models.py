from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class CommerceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=1, max_length=2000)
    category: str = Field(min_length=1, max_length=100)
    budget_cent: int = Field(ge=0)
    region: str = Field(min_length=1, max_length=100)
    run_id: UUID = Field(default_factory=uuid4)
    memory_id: UUID | None = None


class Task(BaseModel):
    task_id: str
    agent: str
    depends_on: tuple[str, ...] = ()


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class TaskRecord(BaseModel):
    task: Task
    status: TaskStatus = TaskStatus.PENDING
    result: dict[str, object] | None = None
    error_code: str | None = None


class RunResult(BaseModel):
    request_id: str
    outcome: str
    tasks: list[TaskRecord]
    dispatch_order: list[str]
    recommendation: dict[str, object] | None
    memory_status: str = "not_configured"
    memory_id: UUID | None = None
    memory_hit_count: int = 0
