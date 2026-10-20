"""Tracing: one trace per agent run, the right span kinds, and no personal data in what is exported."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from copilot import tracing
from copilot.agent import evaluate as agent_eval
from copilot.agent.graph import AgentContext, build_graph
from copilot.agent.scripted import OracleModel, load_eval_cases
from copilot.agent.tools import in_memory_tools
from copilot.kb import load_kb
from copilot.rag.retriever import Retriever

DATA = Path(__file__).resolve().parents[1] / "data" / "generated"
CASES = load_eval_cases(DATA / "eval_tickets.jsonl")
CUSTOMERS = [json.loads(line) for line in (DATA / "customers.jsonl").read_text(encoding="utf-8").splitlines()]
DUPLICATE = 10301  # eval-001, Dmitri Eriksen's duplicate charge


def run_ticket(ticket_id: int, thread_id: str) -> None:
    model = OracleModel(CASES)

    async def main() -> None:
        async with in_memory_tools(agent_eval._servers(Retriever(load_kb(agent_eval.KB_DIR)))) as tools:
            with tracing.run_context(ticket_id, thread_id):
                await build_graph().ainvoke(
                    {"ticket_id": ticket_id}, context=AgentContext(tools, model, model)
                )

    asyncio.run(main())


def leaks(text: str) -> list[str]:
    found = [c["email"] for c in CUSTOMERS if c["email"] in text]
    return found + [c["last_name"] for c in CUSTOMERS if f" {c['last_name']}" in text]


def test_a_run_is_one_trace_with_nodes_tools_and_retrieval():
    memory = InMemorySpanExporter()
    tracing.setup_tracing("test", exporter=memory, batch=False)
    try:
        run_ticket(DUPLICATE, "thread-a")
    finally:
        tracing.shutdown_tracing()
    spans = memory.get_finished_spans()

    assert len({s.context.trace_id for s in spans}) == 1  # everything in one trace
    roots = [s for s in spans if s.parent is None]
    assert [r.name for r in roots] == ["LangGraph"]
    names = {s.name for s in spans}
    assert {"triage", "gather", "decide", "draft", "write_back"} <= names
    assert {"get_ticket", "get_order", "propose_refund", "add_internal_note"} <= names  # MCP tool calls
    kinds = {s.attributes.get("openinference.span.kind") for s in spans}
    assert {"CHAIN", "TOOL", "RETRIEVER"} <= kinds
    assert all(s.attributes.get("session.id") == "thread-a" for s in spans)

    retrieval = next(s for s in spans if s.name == "policy_search")
    assert retrieval.attributes["retrieval.documents.0.document.id"].startswith(
        "pending-and-duplicate-charges"
    )
    assert isinstance(retrieval.attributes["retrieval.documents.0.document.score"], float)


def test_exported_spans_carry_no_personal_data():
    memory = InMemorySpanExporter()
    tracing.setup_tracing("test", exporter=memory, batch=False)
    try:
        for case in CASES[:8]:
            run_ticket(case["ticket"]["id"], case["case_id"])
    finally:
        tracing.shutdown_tracing()
    exported = json.dumps(
        [
            {**dict(s.attributes), "events": [dict(e.attributes or {}) for e in s.events]}
            for s in memory.get_finished_spans()
        ]
    )
    assert "[EMAIL:" in exported  # personal data was there, and was replaced
    assert not leaks(exported)


def test_json_attributes_stay_valid_json():
    value = json.dumps({"requester": {"name": "Ana Lee", "email": "ana@x.com"}, "order": "#1792"})
    out = tracing.redact_attribute(value)
    parsed = json.loads(out)
    assert parsed["order"] == "#1792" and "ana@x.com" not in out and "Ana" not in out


def test_spans_reach_an_otlp_collector_redacted():
    """The real exporter, over HTTP, to a local stand-in for Phoenix's /v1/traces endpoint."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    received: list[bytes] = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *args) -> None:  # keep test output quiet
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
    tracing.setup_tracing("test", exporter=OTLPSpanExporter(endpoint=endpoint), batch=False)
    try:
        run_ticket(DUPLICATE, "thread-otlp")
    finally:
        tracing.shutdown_tracing()
        server.shutdown()

    spans = []
    for body in received:
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        for resource_spans in request.resource_spans:
            service = {a.key: a.value.string_value for a in resource_spans.resource.attributes}
            assert service["openinference.project.name"] == "support-copilot"
            for scope in resource_spans.scope_spans:
                spans.extend(scope.spans)
    assert any(s.name == "LangGraph" for s in spans)
    wire = b"".join(received).decode("utf-8", errors="ignore")
    assert "dmitri.eriksen.2301@example.com" not in wire and "Eriksen" not in wire


def test_tracing_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("COPILOT_TRACING", "off")
    assert tracing.setup_tracing("test", exporter=InMemorySpanExporter()) is None
