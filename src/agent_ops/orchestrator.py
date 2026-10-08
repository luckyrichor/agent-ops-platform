import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from opentelemetry.trace import Status, StatusCode, Tracer

from agent_ops.models import CommerceRequest, RunResult, Task, TaskRecord, TaskStatus
from agent_ops.telemetry import Telemetry, hop

AgentHandler = Callable[[CommerceRequest, dict[str, dict[str, object]]],
                        Awaitable[dict[str, object]]]


class CommercePlanner:
    """Versioned deterministic plan, so orchestration evaluation needs no online LLM."""
    def plan(self, request: CommerceRequest) -> list[Task]:
        return [
            Task(task_id="catalog", agent="catalog"),
            Task(task_id="pricing", agent="pricing", depends_on=("catalog",)),
            Task(task_id="shipping", agent="shipping", depends_on=("catalog",)),
            Task(task_id="recommendation", agent="recommendation",
                 depends_on=("pricing", "shipping")),
        ]


def validate_plan(tasks: list[Task]) -> None:
    names = {t.task_id for t in tasks}
    if not tasks or len(names) != len(tasks):
        raise ValueError("empty or duplicate task IDs")
    if any(set(t.depends_on) - names or t.task_id in t.depends_on for t in tasks):
        raise ValueError("unknown or self dependency")
    visited: set[str] = set()
    while len(visited) < len(names):
        ready = {t.task_id for t in tasks if t.task_id not in visited and
                 set(t.depends_on) <= visited}
        if not ready:
            raise ValueError("cyclic dependencies")
        visited |= ready


class Dispatcher:
    def __init__(self, handlers: dict[str, AgentHandler], tracer: Tracer | None = None) -> None:
        self.handlers = handlers
        if tracer is None:
            self.telemetry: Telemetry | None = Telemetry()
            self.tracer = self.telemetry.tracer
        else:
            self.telemetry = None
            self.tracer = tracer

    async def aclose(self) -> None:
        if self.telemetry is not None:
            await self.telemetry.aclose()

    async def run(self, request: CommerceRequest, tasks: list[Task]) -> RunResult:
        validate_plan(tasks)
        records = {t.task_id: TaskRecord(task=t) for t in tasks}
        order: list[str] = []

        async def execute_role(record: TaskRecord) -> None:
            record.status = TaskStatus.RUNNING
            order.append(record.task.task_id)
            handler = self.handlers.get(record.task.agent)
            if handler is None:
                record.status, record.error_code = TaskStatus.FAILED, "AGENT_NOT_REGISTERED"
                return
            inputs = {name: dict(records[name].result or {}) for name in record.task.depends_on}
            try:
                # Bound each role; CancelledError propagates to the whole run.
                with hop(self.tracer, "tool." + record.task.agent):
                    record.result = await asyncio.wait_for(handler(request, inputs), timeout=5)
                record.status = TaskStatus.SUCCEEDED
            except TimeoutError:
                record.status, record.error_code = TaskStatus.FAILED, "AGENT_TIMEOUT"
            except Exception:  # noqa: BLE001 - role isolation, redact tool exception content
                record.status, record.error_code = TaskStatus.FAILED, "AGENT_EXECUTION_FAILED"

        async def execute(record: TaskRecord) -> None:
            with hop(self.tracer, "agent." + record.task.agent) as current:
                current.set_attribute("task_id", record.task.task_id)
                await execute_role(record)
                current.set_attribute("outcome", record.status.value)
                if record.error_code:
                    current.set_status(Status(StatusCode.ERROR))
                    current.set_attribute("reason_code", record.error_code)

        while any(r.status is TaskStatus.PENDING for r in records.values()):
            for record in records.values():
                if record.status is TaskStatus.PENDING and any(
                    records[d].status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
                    for d in record.task.depends_on):
                    record.status, record.error_code = TaskStatus.BLOCKED, "DEPENDENCY_FAILED"
                    with hop(self.tracer, "agent." + record.task.agent) as current:
                        current.set_attribute("task_id", record.task.task_id)
                        current.set_attribute("outcome", "blocked")
                        current.set_attribute("reason_code", "DEPENDENCY_FAILED")
                        current.set_attribute("blocked_by", [d for d in record.task.depends_on
                            if records[d].status in {TaskStatus.FAILED, TaskStatus.BLOCKED}])
            ready = [r for r in records.values() if r.status is TaskStatus.PENDING and all(
                records[d].status is TaskStatus.SUCCEEDED for d in r.task.depends_on)]
            if ready:
                # W8 maintenance: structured lifetime joins every sibling on cancellation.
                async with asyncio.TaskGroup() as group:
                    for record in ready:
                        group.create_task(execute(record))
        final = records.get("recommendation")
        ok = all(r.status is TaskStatus.SUCCEEDED for r in records.values())
        return RunResult(request_id=str(uuid4()), outcome="succeeded" if ok else "failed",
            tasks=list(records.values()), dispatch_order=order,
            recommendation=final.result if final and final.status is TaskStatus.SUCCEEDED else None)
