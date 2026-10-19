"""Run the Zendesk MCP server.

python -m copilot.zendesk                                # stdio, for MCP clients that spawn it
python -m copilot.zendesk --transport streamable-http    # HTTP at http://127.0.0.1:8101/mcp
"""

from __future__ import annotations

import argparse
from pathlib import Path

from copilot.zendesk.server import build_default_server


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.zendesk",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument(
        "--data", type=Path, default=None, help="folder with tickets.jsonl (default: data/generated)"
    )
    args = parser.parse_args()

    server = build_default_server(args.data, host=args.host, port=args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
