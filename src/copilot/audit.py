"""Audit log for tool calls.

Every MCP tool call writes one JSON line: when, which server and tool, the
arguments with personal data redacted, and whether it succeeded. Lines go to
stderr by default; never to stdout, which carries the MCP protocol over stdio.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
REDACTED_KEYS = {"email", "requester_email", "body"}


def redact_email(text: str) -> str:
    """Replace each email with a short stable hash, so log lines can still be correlated."""
    return EMAIL.sub(lambda m: "email:" + hashlib.sha256(m.group().lower().encode()).hexdigest()[:10], text)


def redact(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        if key == "body":
            return f"<{len(value)} chars>"
        return redact_email(value)
    return value


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
            entry["detail"] = redact_email(detail)
        self.records.append(entry)
        self.stream.write(json.dumps(entry, sort_keys=True) + "\n")
        self.stream.flush()
