"""Safe call-tree telemetry: never export request text, responses or exceptions."""
import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode, Tracer


def configured_tracer() -> Tracer:
    provider = TracerProvider()
    mode = os.environ.get("AGENT_OPS_TRACE_EXPORTER", "none")
    if mode == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif mode != "none":
        raise ValueError("unknown trace exporter")
    return provider.get_tracer("agent-ops")


@contextmanager
def hop(tracer: Tracer, name: str) -> Iterator[Span]:
    with tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException:
            current.set_status(Status(StatusCode.ERROR))
            current.set_attribute("reason_code", "HOP_FAILED")
            raise
