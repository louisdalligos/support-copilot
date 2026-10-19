"""Load knowledge-base documents: Markdown files with a small front-matter block.

The front matter is a flat list of `key: value` lines between `---` markers.
It is parsed here without a YAML dependency so the loader runs anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REQUIRED_FIELDS = ("id", "title", "type", "version", "status", "effective_date", "url")
DOC_TYPES = ("policy", "help")
STATUSES = ("current", "superseded")


@dataclass(frozen=True)
class KbDoc:
    id: str
    title: str
    type: str
    version: int
    status: str
    effective_date: str
    url: str
    body: str
    path: Path
    superseded_by: str | None = None

    @property
    def is_current(self) -> bool:
        return self.status == "current"


class KbFormatError(ValueError):
    pass


def parse_doc(text: str, path: Path) -> KbDoc:
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise KbFormatError(f"{path.name}: missing front matter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise KbFormatError(f"{path.name}: front matter is not closed") from exc

    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise KbFormatError(f"{path.name}: bad front matter line {line!r}")
        meta[key.strip()] = value.strip()

    missing = [f for f in REQUIRED_FIELDS if f not in meta]
    if missing:
        raise KbFormatError(f"{path.name}: missing fields {missing}")
    if meta["type"] not in DOC_TYPES:
        raise KbFormatError(f"{path.name}: type must be one of {DOC_TYPES}")
    if meta["status"] not in STATUSES:
        raise KbFormatError(f"{path.name}: status must be one of {STATUSES}")
    if meta["status"] == "superseded" and "superseded_by" not in meta:
        raise KbFormatError(f"{path.name}: superseded docs need superseded_by")

    return KbDoc(
        id=meta["id"],
        title=meta["title"],
        type=meta["type"],
        version=int(meta["version"]),
        status=meta["status"],
        effective_date=meta["effective_date"],
        url=meta["url"],
        body="\n".join(lines[end + 1 :]).strip() + "\n",
        path=path,
        superseded_by=meta.get("superseded_by"),
    )


def load_kb(kb_dir: Path) -> list[KbDoc]:
    docs = [parse_doc(p.read_text(encoding="utf-8"), p) for p in sorted(kb_dir.glob("*.md"))]
    ids = [d.id for d in docs]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise KbFormatError(f"duplicate doc ids: {duplicates}")
    return docs
