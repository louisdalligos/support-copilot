"""Run the store policy MCP server.

python -m copilot.rag                                   # stdio, keyword search only
python -m copilot.rag --dense --rerank                  # full hybrid pipeline (needs the rag extra)
python -m copilot.rag --transport streamable-http       # HTTP at http://127.0.0.1:8103/mcp
"""

from __future__ import annotations

import argparse
from pathlib import Path

from copilot.rag.server import build_default_server


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.rag",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--kb", type=Path, default=None, help="knowledge-base folder (default: data/kb)")
    parser.add_argument("--dense", action="store_true", help="add dense embeddings")
    parser.add_argument("--rerank", action="store_true", help="add the cross-encoder reranker")
    args = parser.parse_args()

    server = build_default_server(
        args.kb, dense=args.dense, rerank=args.rerank, host=args.host, port=args.port
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
