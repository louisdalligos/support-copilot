"""Store policy MCP server: search and read the store's policy and help documents.

Results carry everything the agent needs to cite its sources: document id,
title, section, URL, version status and effective date. Search always returns
the closest passages; whether they actually answer the question is for the
agent to judge (the reranker's score was measured and is not reliable enough
to decide that on its own).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from copilot.audit import AuditLog
from copilot.rag.retriever import Retriever

MAX_K = 10
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

INSTRUCTIONS = """\
Policies and help articles for Northwind Coffee Co.
Use search_policies to find the passages that answer a question, and cite each
passage you rely on by its doc_id and url. Only current policies are returned
unless you pass as_of: for an order delivered before a policy changed, pass the
delivery date to get the version that applied to it.
Search always returns the closest passages, even when none of them answers the question.
If the passages do not answer it, say the policies do not cover it and escalate. Never
answer a policy question from general knowledge."""


def build_server(retriever: Retriever, *, audit: AuditLog | None = None, **settings: Any) -> FastMCP:
    audit = audit or AuditLog("policies")
    mcp = FastMCP("policies", instructions=INSTRUCTIONS, **settings)

    @mcp.tool(title="Search policies", annotations=READ)
    def search_policies(
        query: str, k: int = 5, as_of: str | None = None, include_superseded: bool = False
    ) -> dict[str, Any]:
        """Search the store's policies and help articles. Returns the best-matching passages, best first.

        as_of (YYYY-MM-DD) selects the policy versions in force on that date, for older orders.
        include_superseded returns old versions too, for questions about what changed."""
        args = {"query": query, "k": k, "as_of": as_of, "include_superseded": include_superseded}
        if not query.strip():
            audit.record("search_policies", args, "rejected", "empty query")
            raise ToolError("query must not be empty")
        if not 1 <= k <= MAX_K:
            audit.record("search_policies", args, "rejected", "bad k")
            raise ToolError(f"k must be between 1 and {MAX_K}")
        try:
            day = date.fromisoformat(as_of) if as_of else None
        except ValueError:
            audit.record("search_policies", args, "rejected", "bad as_of")
            raise ToolError("as_of must be a date like 2026-02-14") from None

        result = retriever.search(query, k=k, as_of=day, include_superseded=include_superseded)
        audit.record("search_policies", args, "ok")
        return {
            "relevant": result.relevant,
            "top_score": round(result.top_score, 4) if result.top_score is not None else None,
            "pipeline": result.pipeline,
            "notes": result.notes,
            "results": [
                {
                    "doc_id": h.chunk.doc_id,
                    "title": h.chunk.title,
                    "section": h.chunk.section,
                    "url": h.chunk.url,
                    "status": h.chunk.status,
                    "effective_date": h.chunk.effective_date,
                    "score": round(h.score, 4),
                    "text": h.chunk.text,
                }
                for h in result.hits
            ],
        }

    @mcp.tool(title="Get policy", annotations=READ)
    def get_policy(doc_id: str) -> dict[str, Any]:
        """Get one whole document by its id, for when a passage is not enough."""
        doc = retriever.docs.get(doc_id)
        if doc is None:
            audit.record("get_policy", {"doc_id": doc_id}, "not_found")
            raise ToolError(f"No document with id {doc_id!r}")
        audit.record("get_policy", {"doc_id": doc_id}, "ok")
        return {
            "doc_id": doc.id,
            "title": doc.title,
            "type": doc.type,
            "status": doc.status,
            "superseded_by": doc.superseded_by,
            "effective_date": doc.effective_date,
            "url": doc.url,
            "body": doc.body,
        }

    @mcp.tool(title="List policies", annotations=READ)
    def list_policies() -> list[dict[str, Any]]:
        """List every document: id, title, type (policy or help) and whether it is current."""
        audit.record("list_policies", {}, "ok")
        return [
            {"doc_id": d.id, "title": d.title, "type": d.type, "status": d.status}
            for d in sorted(retriever.docs.values(), key=lambda d: d.id)
        ]

    @mcp.resource("kb://docs/{doc_id}", mime_type="text/markdown")
    def doc_resource(doc_id: str) -> str:
        """One document as Markdown."""
        doc = retriever.docs.get(doc_id)
        if doc is None:
            raise ValueError(f"No document with id {doc_id!r}")
        return doc.body

    return mcp


def default_kb_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "kb"


def build_default_server(
    kb_dir: Path | None = None, *, dense: bool = False, rerank: bool = False, **settings: Any
) -> FastMCP:
    embedder = reranker = None
    if dense:
        from copilot.rag.models import SentenceTransformerEmbedder

        embedder = SentenceTransformerEmbedder()
    if rerank:
        from copilot.rag.models import CrossEncoderReranker

        reranker = CrossEncoderReranker()
    retriever = Retriever.from_kb_dir(kb_dir or default_kb_dir(), embedder=embedder, reranker=reranker)
    return build_server(retriever, **settings)
