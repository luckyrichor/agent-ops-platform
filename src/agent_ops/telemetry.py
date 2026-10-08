"""Safe call-tree telemetry: never export request text, responses or exceptions."""
import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from starlette.requests import ClientDisconnect


def configured_tracer() -> Tracer:
    provider = TracerProvider()
    mode = os.environ.get("AGENT_OPS_TRACE_EXPORTER", "none")
    if mode == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif mode != "none":
        raise ValueError("unknown trace exporter")
    return provider.get_tracer("agent-ops")


@contextmanager
def hop(tracer: Tracer, name: str, *, kind: SpanKind = SpanKind.INTERNAL) -> Iterator[Span]:
    with tracer.start_as_current_span(
        name, kind=kind, record_exception=False, set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except (asyncio.CancelledError, GeneratorExit, ClientDisconnect):
            current.set_attribute("outcome", "cancelled")
            current.set_attribute("reason_code", "REQUEST_CANCELLED")
            raise
        except BaseException:
            current.set_status(Status(StatusCode.ERROR))
            current.set_attribute("reason_code", "HOP_FAILED")
            raise
