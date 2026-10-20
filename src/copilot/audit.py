"""Audit log for tool calls.

Every MCP tool call writes one JSON line: when, which server and tool, the
arguments with personal data redacted (see copilot.privacy), and whether it
succeeded. Note text is not logged at all, only its length. Lines go to stderr
by default; never to stdout, which carries the MCP protocol over stdio.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from copilot import privacy


def redact(value: Any) -> Any:
    """Arguments as logged: note bodies reduced to their length, everything else through copilot.privacy."""
    if isinstance(value, dict) and "body" in value and isinstance(value["body"], str):
        value = {**value, "body": f"<{len(value['body'])} chars>"}
    return privacy.redact(value)


class AuditLog:
    def __init__(self, server: str, stream: TextIO | None = None) -> None:
        self.server = server
        self.stream = stream or sys.stderr
        self.records: list[dict[str, Any]] = []  # kept in memory too, for tests and debugging

    def record(self, tool: str, args: dict[str, Any], outcome: str, detail: str = "") -> None:
        entry = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "server": self.server,
            "tool": tool,
            "args": redact(args),
            "outcome": outcome,
        }
        if detail:
            entry["detail"] = privacy.redact_text(detail)
        self.records.append(entry)
        self.stream.write(json.dumps(entry, sort_keys=True) + "\n")
        self.stream.flush()
