"""Tracing to Arize Phoenix, with personal data removed before export.

Every agent run becomes one trace: the LangGraph nodes, each model call (with
its prompt, output and token counts), each MCP tool call, and each policy
retrieval with the passages and scores it returned. OpenInference's LangChain
instrumentation produces the spans; they go to Phoenix over OTLP.

Spans pass through RedactingSpanExporter on the way out: every string attribute
and event is run through copilot.privacy, so traces carry tokens like
[EMAIL:3f2a9c1d], never a customer's email, name or card digits.

Configuration (environment):
    PHOENIX_COLLECTOR_ENDPOINT  where Phoenix listens (default http://127.0.0.1:6006/v1/traces)
    COPILOT_TRACING=off         turn tracing off
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from copilot import privacy

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://127.0.0.1:6006/v1/traces"
PROJECT = "support-copilot"
_provider: Any = None


def redact_attribute(value: Any) -> Any:
    """Redact one span attribute value. JSON strings (OpenInference input.value, output.value)
    are parsed, redacted field by field, and re-serialized so they stay valid JSON."""
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped[:1] in ("{", "["):
            try:
                return json.dumps(privacy.redact(json.loads(value)), ensure_ascii=False)
            except ValueError:
                pass
        return privacy.redact_text(value)
    if isinstance(value, (list, tuple)):
        return type(value)(redact_attribute(v) for v in value)
    return value


def redact_attributes(attributes: Any) -> dict[str, Any]:
    return {key: redact_attribute(value) for key, value in dict(attributes or {}).items()}


def redact_span(span: Any) -> Any:
    """A copy of a finished span with its attributes and events redacted."""
    from opentelemetry.sdk.trace import Event, ReadableSpan

    events = [Event(e.name, redact_attributes(e.attributes), e.timestamp) for e in span.events]
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=redact_attributes(span.attributes),
        events=events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def redacting_exporter(inner: Any) -> Any:
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class RedactingSpanExporter(SpanExporter):
        """Wraps an exporter; nothing reaches it unredacted."""

        def __init__(self, wrapped: Any) -> None:
            self.wrapped = wrapped

        def export(self, spans: Sequence[Any]) -> SpanExportResult:
            return self.wrapped.export([redact_span(s) for s in spans])

        def shutdown(self) -> None:
            self.wrapped.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return self.wrapped.force_flush(timeout_millis)

    return RedactingSpanExporter(inner)


def setup_tracing(service: str, *, exporter: Any = None, batch: bool = True) -> Any:
    """Send traces to Phoenix. Returns the tracer provider, or None if tracing is off or unavailable.

    `exporter` replaces the OTLP exporter (tests use an in-memory one); it is still wrapped
    by the redacting exporter.
    """
    global _provider
    if os.environ.get("COPILOT_TRACING", "").lower() in ("off", "0", "false"):
        return None
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:  # pragma: no cover - depends on the environment
        log.warning("Tracing packages are not installed; tracing is off.")
        return None

    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", DEFAULT_ENDPOINT))

    provider = TracerProvider(
        resource=Resource.create({"service.name": service, "openinference.project.name": PROJECT})
    )
    processor = BatchSpanProcessor if batch else SimpleSpanProcessor
    provider.add_span_processor(processor(redacting_exporter(exporter)))

    instrumentor = LangChainInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    # skip_dep_check: the check reads package metadata, which some installs (vendored, zipped) lack.
    instrumentor.instrument(tracer_provider=provider, skip_dep_check=True)
    _provider = provider
    return provider


def shutdown_tracing() -> None:
    """Flush and stop. Call before the process exits so the last spans are sent."""
    global _provider
    if _provider is None:
        return
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor

        instrumentor = LangChainInstrumentor()
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    except ImportError:  # pragma: no cover
        pass
    _provider.shutdown()
    _provider = None


def tracer(name: str) -> Any:
    """A tracer from the provider set up here, or a no-op tracer when tracing is off."""
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover
        return None
    return (_provider or trace).get_tracer(name)


@contextmanager
def run_context(ticket_id: int, thread_id: str | None = None, **metadata: Any) -> Iterator[None]:
    """Tag every span in this block with the ticket and run, so Phoenix can group them."""
    try:
        from openinference.instrumentation import using_attributes
    except ImportError:  # pragma: no cover
        yield
        return
    meta = {"ticket_id": ticket_id, **metadata}
    with using_attributes(
        session_id=thread_id or f"ticket-{ticket_id}", metadata=meta, tags=["support-copilot"]
    ):
        yield


@contextmanager
def retrieval_span(query: str, pipeline: str) -> Iterator[Any]:
    """An OpenInference RETRIEVER span around a policy search; call `record(hits)` with the results."""
    t = tracer("copilot.rag")
    if t is None:
        yield lambda hits: None
        return
    parent, context_attributes = None, []
    try:
        from openinference.instrumentation import get_attributes_from_context
        from openinference.instrumentation.langchain import get_current_span
        from opentelemetry import trace

        current = get_current_span()  # the LangGraph node this search runs in
        if current is not None:
            parent = trace.set_span_in_context(current)
        context_attributes = list(get_attributes_from_context())  # session id, metadata, tags
    except ImportError:  # pragma: no cover
        pass
    with t.start_as_current_span("policy_search", context=parent) as span:
        for key, value in context_attributes:
            span.set_attribute(key, value)
        span.set_attribute("openinference.span.kind", "RETRIEVER")
        span.set_attribute("input.value", query)
        span.set_attribute("metadata", json.dumps({"pipeline": pipeline}))

        def record(hits: list[dict[str, Any]]) -> None:
            for i, h in enumerate(hits):
                prefix = f"retrieval.documents.{i}.document"
                span.set_attribute(
                    f"{prefix}.id", h["doc_id"] + (f"#{h['section']}" if h.get("section") else "")
                )
                span.set_attribute(f"{prefix}.content", h["text"])
                span.set_attribute(f"{prefix}.score", float(h["score"]))
                span.set_attribute(f"{prefix}.metadata", json.dumps({"url": h["url"], "status": h["status"]}))

        yield record


def check() -> int:
    """Send one test span to Phoenix and say whether it arrived: `python -m copilot.tracing`."""
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", DEFAULT_ENDPOINT)
    print(f"endpoint       {endpoint}")
    print(f"COPILOT_TRACING {os.environ.get('COPILOT_TRACING', '(not set: tracing on)')}")
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError as e:
        print(f'FAIL: tracing packages are not installed ({e}). Run: pip install -e ".[dev,rag]"')
        return 1
    try:
        import openinference.instrumentation.langchain  # noqa: F401
    except ImportError as e:
        print(f"FAIL: openinference-instrumentation-langchain is not installed ({e}).")
        return 1

    results: list[Any] = []

    class Recording(OTLPSpanExporter):
        def export(self, spans: Sequence[Any]) -> Any:
            result = super().export(spans)
            results.append(result)
            return result

    provider = TracerProvider(
        resource=Resource.create({"service.name": "tracing-check", "openinference.project.name": PROJECT})
    )
    provider.add_span_processor(SimpleSpanProcessor(redacting_exporter(Recording(endpoint=endpoint))))
    with provider.get_tracer("copilot.check").start_as_current_span("tracing check") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("input.value", "hello from support-copilot")
    provider.shutdown()

    if results and results[-1].name == "SUCCESS":
        print(
            f"OK: Phoenix accepted the span. Open http://localhost:6006 and look for the '{PROJECT}' project."
        )
        return 0
    print(
        "FAIL: Phoenix did not accept the span. Is it running (docker compose ps) and is the endpoint right?"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(check())
