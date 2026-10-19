"""Split knowledge-base documents into chunks for retrieval.

The documents are short, so a chunk is one `##` section, prefixed with the
document and section titles so it still makes sense on its own. A document
without `##` sections is one chunk. Text before the first `##` (the
introduction) becomes its own chunk when it says more than the title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from copilot.kb import KbDoc

MIN_INTRO_WORDS = 8


@dataclass(frozen=True)
class Chunk:
    id: str  # "<doc id>#<section slug>"
    doc_id: str
    title: str  # document title
    section: str | None
    text: str  # what is embedded and searched: titles plus section body
    url: str
    status: str
    effective_date: str
    superseded_by: str | None

    @property
    def is_current(self) -> bool:
        return self.status == "current"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _strip_h1(body: str) -> str:
    lines = body.strip().split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def chunk_doc(doc: KbDoc) -> list[Chunk]:
    body = _strip_h1(doc.body)
    parts = re.split(r"^## +(.+)$", body, flags=re.MULTILINE)
    intro, sections = parts[0].strip(), list(zip(parts[1::2], parts[2::2], strict=True))

    def make(section: str | None, text: str) -> Chunk:
        heading = doc.title if section is None else f"{doc.title} > {section.strip()}"
        return Chunk(
            id=f"{doc.id}#{slug(section) if section else 'intro'}",
            doc_id=doc.id,
            title=doc.title,
            section=section.strip() if section else None,
            text=f"{heading}\n\n{text.strip()}",
            url=doc.url,
            status=doc.status,
            effective_date=doc.effective_date,
            superseded_by=doc.superseded_by,
        )

    if not sections:
        return [make(None, intro)]
    chunks = []
    if len(intro.split()) >= MIN_INTRO_WORDS:
        chunks.append(make(None, intro))
    chunks.extend(make(heading, text) for heading, text in sections)
    return chunks


def chunk_docs(docs: dict[str, KbDoc] | list[KbDoc]) -> list[Chunk]:
    values = docs.values() if isinstance(docs, dict) else docs
    return [c for doc in sorted(values, key=lambda d: d.id) for c in chunk_doc(doc)]
