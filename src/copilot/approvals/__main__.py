"""Run the approval app.

    python -m copilot.approvals                    # http://127.0.0.1:8000, Claude, Postgres from .env
    python -m copilot.approvals --model oracle     # offline: reads the eval labels instead of calling Claude
    python -m copilot.approvals --memory           # keep runs in memory instead of Postgres

With Postgres (docker compose up -d, POSTGRES_PASSWORD in .env), paused runs and
the queue survive restarts. The fake Zendesk and Shopify data is reloaded from
the seed files on every start, so replies and refunds sent in an earlier session
are not shown after a restart.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    from copilot.agent.__main__ import _models, load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="python -m copilot.approvals",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model", choices=["claude", "oracle"], default=os.environ.get("COPILOT_MODEL", "claude")
    )
    parser.add_argument("--memory", action="store_true", help="do not use Postgres")
    parser.add_argument("--rag-models", action="store_true", help="dense search and reranker for policies")
    parser.add_argument("--no-tracing", action="store_true", help="do not send traces to Phoenix")
    parser.add_argument("--host", default=os.environ.get("COPILOT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("COPILOT_PORT", "8000")))
    args = parser.parse_args()

    import uvicorn

    from copilot.approvals.service import ApprovalService, Backends
    from copilot.approvals.storage import database_url, open_storage
    from copilot.approvals.web import create_app
    from copilot.kb import load_kb
    from copilot.rag.retriever import Retriever
    from copilot.shopify.backend import connect as connect_shopify
    from copilot.zendesk.backend import FakeZendesk

    triage_m, draft_m = _models(args.model)
    url = None if args.memory else database_url()
    if url is None and not args.memory:
        print("No database configured (POSTGRES_PASSWORD or COPILOT_DATABASE_URL); keeping runs in memory.")

    def retriever() -> Retriever:
        docs = load_kb(ROOT / "data" / "kb")
        if not args.rag_models:
            return Retriever(docs)
        from copilot.rag.models import CrossEncoderReranker, SentenceTransformerEmbedder

        return Retriever(docs, embedder=SentenceTransformerEmbedder(), reranker=CrossEncoderReranker())

    @asynccontextmanager
    async def open_service() -> AsyncIterator[tuple[ApprovalService, str]]:
        data = ROOT / "data" / "generated"
        backends = Backends(FakeZendesk.from_generated(data), connect_shopify(data), retriever())
        async with open_storage(url) as storage:
            yield (
                ApprovalService(backends, storage.checkpointer, storage.store, triage_m, draft_m),
                storage.kind,
            )

    from copilot.privacy import install_log_redaction

    app = create_app(open_service=open_service, model_name=args.model)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        proxy_headers=True,
        # Behind Caddy in Docker, trust its X-Forwarded-* headers; on localhost, trust nobody else.
        forwarded_allow_ips=os.environ.get("COPILOT_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
    install_log_redaction()  # after uvicorn has set up its loggers, so its handlers get the filter too
    from copilot.tracing import setup_tracing, shutdown_tracing

    if args.no_tracing:
        print("Tracing is off (--no-tracing).")
    elif setup_tracing("approval-app"):
        print(
            "Tracing to Phoenix (COPILOT_TRACING=off to stop). Check the connection: python -m copilot.tracing"
        )
    else:
        print("Tracing is off: COPILOT_TRACING is set to off, or the tracing packages are not installed.")
    server = uvicorn.Server(config)
    print(f"Approval app on http://{args.host}:{args.port}")
    # psycopg's async driver cannot use Windows' default event loop, so pick the selector loop there.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        asyncio.run(server.serve(), loop_factory=loop_factory)
    finally:
        shutdown_tracing()
    return 0


if __name__ == "__main__":
    sys.exit(main())
