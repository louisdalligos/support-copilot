"""Fill a Zendesk account with the Northwind demo tickets.

    python -m copilot.zendesk.demo_desk setup              # import what is missing (safe to run again)
    python -m copilot.zendesk.demo_desk status             # what is there
    python -m copilot.zendesk.demo_desk send 10301         # a new ticket, as if the customer just wrote it

setup imports the labeled demo tickets (10301-10352 in the seed data) with their
customers, through Zendesk's ticket import API. Imported tickets keep their dates
(moved forward to today, like the Shopify demo orders) and do not fire triggers, so
setup does not start 52 copilot runs. Run the copilot on them from the approval
app's Tickets page.

send creates a fresh copy of one demo ticket through the normal API, so the
"ticket is created" trigger fires and the webhook runs the copilot on it: the way a
real ticket arrives.

Zendesk gives tickets its own ids. Each demo ticket carries its seed id in
external_id ("northwind-10301") and the tag northwind-demo.

Needs ZENDESK_SUBDOMAIN, ZENDESK_CLIENT_ID and ZENDESK_CLIENT_SECRET (see docs/zendesk.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from copilot.store import TODAY
from copilot.zendesk.live import ZendeskClient, ZendeskError

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data" / "generated"
DEMO_TAG = "northwind-demo"
LIVE_TAG = "northwind-live"


def demo_tickets() -> list[dict[str, Any]]:
    rows = (DATA / "eval_tickets.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["ticket"] for line in rows if line]


def external_id(ticket: dict[str, Any]) -> str:
    return f"northwind-{ticket['id']}"


def shift(stamp: str, days: int, now: datetime | None = None) -> str:
    """Move a seed timestamp forward by `days`, but keep it at least ten minutes in the past:
    Zendesk refuses imported dates in the future, and clocks differ a little."""
    moved = datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(days=days)
    latest = (now or datetime.now(UTC)) - timedelta(minutes=10)
    return min(moved, latest).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class DemoDesk:
    def __init__(self, client: ZendeskClient, today: date | None = None) -> None:
        self.client = client
        self.days = ((today or date.today()) - TODAY).days
        self._user_ids: dict[str, int] = {}
        self._me: int | None = None

    def me(self) -> int:
        """The user who owns the OAuth client; agent comments are imported as theirs."""
        if self._me is None:
            self._me = self.client.request("GET", "/users/me.json")["user"]["id"]
        return self._me

    def user_id(self, name: str, email: str) -> int:
        key = email.lower()
        if key not in self._user_ids:
            body = {"user": {"name": name, "email": key, "role": "end-user", "verified": True}}
            user = self.client.request("POST", "/users/create_or_update.json", json=body)["user"]
            self._user_ids[key] = user["id"]
        return self._user_ids[key]

    def existing(self, ext: str) -> dict[str, Any] | None:
        found = self.client.request("GET", "/tickets.json", params={"external_id": ext}).get("tickets") or []
        return found[0] if found else None

    def _import_body(self, t: dict[str, Any]) -> dict[str, Any]:
        uid = self.user_id(t["requester_name"], t["requester_email"])
        return {
            "requester_id": uid,
            "submitter_id": uid,
            "subject": t["subject"],
            "status": t.get("status") or "new",
            "tags": [DEMO_TAG],
            "external_id": external_id(t),
            "created_at": shift(t["created_at"], self.days),
            "comments": [
                {
                    "author_id": uid if c["author"] == "customer" else self.me(),
                    "value": c["body"],
                    "public": c.get("public", True),
                    "created_at": shift(c["created_at"], self.days),
                }
                for c in t["comments"]
            ],
        }

    def setup(self, out: Any = sys.stdout) -> int:
        made = 0
        for t in demo_tickets():
            ext = external_id(t)
            if found := self.existing(ext):
                print(f"  {ext}: already there (ticket {found['id']})", file=out)
                continue
            body = {"ticket": self._import_body(t)}
            ticket = self.client.request("POST", "/imports/tickets.json", json=body)["ticket"]
            print(f"  {ext}: imported as ticket {ticket['id']} ({t['subject']})", file=out)
            made += 1
        return made

    def status(self, out: Any = sys.stdout) -> int:
        tickets = demo_tickets()
        there = 0
        for t in tickets:
            found = self.existing(external_id(t))
            there += bool(found)
            where = f"ticket {found['id']}, {found['status']}" if found else "missing"
            print(f"  {external_id(t)}: {where}", file=out)
        print(f"{there}/{len(tickets)} demo tickets in Zendesk", file=out)
        return there

    def send(self, seed_id: int) -> dict[str, Any]:
        """A new ticket with the text of one demo ticket, created now. Fires the triggers."""
        match = [t for t in demo_tickets() if t["id"] == seed_id]
        if not match:
            raise SystemExit(f"No demo ticket {seed_id}; the demo tickets are 10301-10352.")
        t = match[0]
        uid = self.user_id(t["requester_name"], t["requester_email"])
        first = t["comments"][0]
        body = {
            "ticket": {
                "requester_id": uid,
                "submitter_id": uid,
                "subject": t["subject"],
                "tags": [LIVE_TAG],
                "comment": {"body": first["body"], "author_id": uid, "public": True},
            }
        }
        return self.client.request("POST", "/tickets.json", json=body)["ticket"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.zendesk.demo_desk", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="import the demo tickets that are missing")
    sub.add_parser("status", help="list the demo tickets in Zendesk")
    send = sub.add_parser("send", help="create a new ticket from one demo ticket (fires the webhook)")
    send.add_argument("seed_id", type=int, help="seed ticket id, 10301-10352")
    args = parser.parse_args(argv)

    from copilot.agent.__main__ import load_dotenv

    load_dotenv()
    desk = DemoDesk(ZendeskClient.from_env())
    try:
        if args.command == "setup":
            made = desk.setup()
            print(f"Imported {made} ticket(s).")
        elif args.command == "status":
            desk.status()
        else:
            ticket = desk.send(args.seed_id)
            print(f"Created ticket {ticket['id']}: {ticket['subject']}")
    except ZendeskError as e:
        raise SystemExit(str(e)) from None


if __name__ == "__main__":
    main()
