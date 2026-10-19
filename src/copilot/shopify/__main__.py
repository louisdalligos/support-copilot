"""Run the Shopify MCP server.

python -m copilot.shopify                                # stdio, for MCP clients that spawn it
python -m copilot.shopify --transport streamable-http    # HTTP at http://127.0.0.1:8102/mcp
"""

from __future__ import annotations

import argparse
from pathlib import Path

from copilot.shopify.server import build_default_server


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.shopify",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument(
        "--data", type=Path, default=None, help="folder with orders.jsonl (default: data/generated)"
    )
    args = parser.parse_args()

    server = build_default_server(args.data, host=args.host, port=args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
