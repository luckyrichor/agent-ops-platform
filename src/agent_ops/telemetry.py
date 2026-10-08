"""Safe call-tree telemetry: never export request text, responses or exceptions."""
import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from starlette.requests import ClientDisconnect


class Telemetry:
    """Own exporter threads; callers flush and shut them down at process exit."""

    def __init__(self) -> None:
        ratio = float(os.environ.get("AGENT_OPS_TRACE_SAMPLE_RATIO", "1"))
        if not 0 <= ratio <= 1:
            raise ValueError("trace sampling ratio must be between 0 and 1")
        self.provider = TracerProvider(
            resource=Resource.create({"service.name": "agent-ops-platform"}),
            sampler=ParentBased(TraceIdRatioBased(ratio)),
        )
        mode = os.environ.get("AGENT_OPS_TRACE_EXPORTER", "none")
        if mode == "console":
            self.provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        elif mode == "otlp":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from requests import Session

            session = Session()
            # Local collector requests must not follow shell SOCKS/system proxies.
            session.trust_env = False
            exporter = OTLPSpanExporter(
                endpoint=os.environ.get("AGENT_OPS_OTLP_ENDPOINT",
                                        "http://127.0.0.1:4318/v1/traces"),
                timeout=3, session=session,
            )
            self.provider.add_span_processor(BatchSpanProcessor(
                exporter, max_queue_size=2048, max_export_batch_size=256,
                schedule_delay_millis=1000, export_timeout_millis=3000,
            ))
        elif mode != "none":
            self.provider.shutdown()
            raise ValueError("unknown trace exporter")
        self.tracer = self.provider.get_tracer("agent-ops")

    async def aclose(self) -> None:
        await asyncio.to_thread(self.provider.shutdown)


def configured_tracer() -> Tracer:
    # Compatibility for standalone helpers. Prefer Telemetry ownership in Dispatcher.
    return Telemetry().tracer


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
