"""Remove personal data before it leaves the system of record.

Customer data belongs in Zendesk and Shopify, where staff need it. Everywhere
else (logs, traces, eval reports, and the text sent to the model) it is
replaced with a typed token:

    "Email me at ana.lee@gmail.com"  ->  "Email me at [EMAIL:3f2a9c1d]"

Tokens are keyed hashes (HMAC-SHA256 with COPILOT_REDACTION_KEY): the same value
always gives the same token, so two log lines about one customer can still be
connected, but the value cannot be read back or guessed by hashing candidates
without the key.

What is detected:
- emails (except the store's own addresses), phone numbers, IP addresses
- card numbers (13-19 digits passing the Luhn check) and "ends in 1234" mentions
- street addresses ("12 Harbor Street")
- names: the customer's own name when known, the names of every customer in
  the store's records, and names after greetings ("Hi Elena,")
- in structured data, fields that hold personal data by their key (email,
  phone, address, card_last4, first_name, last_name, a requester's name)

Order numbers, amounts, dates and product names are kept: they are what makes a
log line useful, and they do not identify a person on their own.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

STORE_DOMAINS = ("northwind-coffee.example",)
DEV_KEY = "dev-only-redaction-key"

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
CARD = re.compile(r"(?<![\d#])(?:\d[ -]?){12,18}\d(?!\d)")
LAST4 = re.compile(
    r"(?i)\b(ending in|ending|ends in|ends with|last (?:4|four)(?: digits)?(?: (?:are|is))?)(\s*:?\s*)(\d{4})\b"
)
PHONE = re.compile(
    r"(?<![\w#$.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])\d{3,4}[\s.-]\d{3,4}(?!\w|[.-]\d)"
)
IP = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
STREET = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Way|Court|Ct|Place|Pl|Terrace)\b\.?"
)
GREETING = re.compile(
    r"\b(Hi|Hello|Hey|Dear|Thanks|Thank you|Cheers|Regards|Best),?\s+([A-Z][a-z]{1,20})\b(?=[,.!\s]|$)"
)

PERSONAL_KEYS = {
    "email",
    "requester_email",
    "requester_name",
    "customer_name",
    "phone",
    "address",
    "address1",
    "address2",
    "card_last4",
    "first_name",
    "last_name",
    "ip",
}
NAME_PARENTS = {"requester", "customer", "author_profile"}
NOT_NAMES = {"Northwind", "Coffee", "Support", "Team", "There", "All", "Everyone"}


def _key() -> bytes:
    return os.environ.get("COPILOT_REDACTION_KEY", DEV_KEY).encode()


def token(kind: str, value: str) -> str:
    digest = hmac.new(_key(), f"{kind}:{value.strip().lower()}".encode(), hashlib.sha256).hexdigest()
    return f"[{kind}:{digest[:8]}]"


def luhn_ok(digits: str) -> bool:
    total, double = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if double:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        double = not double
    return total % 10 == 0


@lru_cache(maxsize=1)
def _store_names(path: str | None) -> frozenset[str]:
    """Every customer's first and last name from the seed data, for name detection in free text."""
    if path is None:
        return frozenset()
    import json
    from pathlib import Path

    names: set[str] = set()
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line:
                c = json.loads(line)
                names.update({c["first_name"], c["last_name"]})
    return frozenset(n for n in names if len(n) >= 2)  # two-letter surnames (Xu, Li, Ng) count


def default_customer_names() -> frozenset[str]:
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "data" / "generated" / "customers.jsonl"
    return _store_names(str(path))


@lru_cache(maxsize=256)
def _name_pattern(names: frozenset[str]) -> re.Pattern[str] | None:
    if not names:
        return None
    return re.compile(r"\b(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")\b")


def redact_text(text: str, *, names: Iterable[str] = (), customer_names: bool = True) -> str:
    """Replace personal data in free text with tokens."""
    if not text:
        return text

    def email(m: re.Match[str]) -> str:
        value = m.group()
        if value.lower().endswith(tuple("@" + d for d in STORE_DOMAINS)):
            return value
        return token("EMAIL", value)

    def card(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            return token("CARD", digits)
        return m.group()

    text = EMAIL.sub(email, text)
    text = CARD.sub(card, text)
    text = LAST4.sub(lambda m: f"{m.group(1)}{m.group(2)}{token('CARD_LAST4', m.group(3))}", text)
    text = IP.sub(lambda m: token("IP", m.group()), text)
    text = PHONE.sub(lambda m: token("PHONE", re.sub(r"\D", "", m.group())), text)
    text = STREET.sub(lambda m: token("ADDRESS", m.group()), text)

    known = frozenset(n.strip() for n in names if n and len(n.strip()) > 1)
    if customer_names:
        known |= default_customer_names()
    pattern = _name_pattern(known - NOT_NAMES)
    if pattern is not None:
        text = pattern.sub(lambda m: token("NAME", m.group()), text)
    text = GREETING.sub(
        lambda m: m.group() if m.group(2) in NOT_NAMES else f"{m.group(1)} {token('NAME', m.group(2))}", text
    )
    return text


def _names_in(value: Any, key: str | None = None) -> set[str]:
    """Names held in structured fields anywhere in the value, so they can be found in its free text too."""
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, str) and (
                k in ("first_name", "last_name") or (k == "name" and key in NAME_PARENTS)
            ):
                found.update(part for part in v.split() if len(part) > 1)
            else:
                found |= _names_in(v, k)
    elif isinstance(value, list):
        for v in value:
            found |= _names_in(v, key)
    return found


def redact(value: Any, *, names: Iterable[str] = ()) -> Any:
    """Redact personal data in nested dicts and lists: by key, and in every free-text value."""
    known = tuple(names) + tuple(sorted(_names_in(value)))
    return _redact(value, known, None, None)


def _redact(value: Any, names: tuple[str, ...], key: str | None, parent: str | None) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v, names, k, key) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, names, key, parent) for v in value]
    if isinstance(value, str):
        if key in PERSONAL_KEYS or (key == "name" and parent in NAME_PARENTS):
            return token((key or "VALUE").upper(), value)
        return redact_text(value, names=names)
    return value


def find_personal_data(text: str) -> list[str]:
    """Kinds of personal data present in text (used to flag drafts that repeat it)."""
    kinds = []
    if any(
        not m.group().lower().endswith(tuple("@" + d for d in STORE_DOMAINS)) for m in EMAIL.finditer(text)
    ):
        kinds.append("an email address")
    if any(luhn_ok(re.sub(r"\D", "", m.group())) for m in CARD.finditer(text)):
        kinds.append("a card number")
    if PHONE.search(text):
        kinds.append("a phone number")
    return kinds


class RedactingFilter(logging.Filter):
    """A logging filter that redacts each record's message before any handler writes it."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the format string and each string argument in place, so formatters that
        # read record.args themselves (uvicorn's access log unpacks them) keep working.
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_text(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_text(v) if isinstance(v, str) else v for k, v in record.args.items()}
        # Anything still left (personal data inside a non-string argument): flatten the message.
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def install_log_redaction() -> None:
    """Redact every log line written through the root logger's and uvicorn's handlers."""
    flt = RedactingFilter()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "mcp"):
        logger = logging.getLogger(name)
        logger.addFilter(flt)
        for handler in logger.handlers:
            handler.addFilter(flt)
