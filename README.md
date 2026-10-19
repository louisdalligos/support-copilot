# AI Support Copilot

An AI support agent for a fictional online coffee store, Northwind Coffee Co. It reads customer tickets, answers from the store's policies and order data with citations, and proposes refunds for a human to approve.

It is a portfolio project, built to show production AI engineering end to end: RAG, custom MCP servers, a LangGraph agent, LLM APIs and open models, evals in CI, and LLMOps. Everything runs on open-source tools.

## Status

| Phase | What | State |
| --- | --- | --- |
| 1. Foundations | Knowledge base, seed data generator, refund rules in code, CI | **Done** |
| 2. Thin slice | Zendesk and Shopify MCP servers (fake backends), policy RAG, agent via CLI, evals in CI | In progress: MCP servers done |
| 3. Hardening | Prompt-injection evals, PII redaction, durable approvals, approval UI | |
| 4. Deploy and demo | Docker on one VM, webhooks, Phoenix tracing, public sandbox | |
| 5. Expand | Risk profiles, dispute evidence, model comparison | |

## What's here

```
data/kb/               39 policy and help documents (2 are outdated versions, on purpose)
data/generated/        seed data, generated and committed (see below)
src/copilot/store.py   the store's facts: refund window, shipping prices, descriptors
src/copilot/policy.py  refund rules as code; the agent's safety check uses this
src/copilot/kb.py      knowledge-base loader
src/copilot/seed/      deterministic seed data generator
src/copilot/zendesk/   Zendesk MCP server and its fake backend
src/copilot/shopify/   Shopify MCP server and its fake backend
src/copilot/audit.py   audit log of every tool call, with emails redacted
tests/                 tests for the rules, the data and the MCP servers
docker-compose.yml     Postgres with pgvector, and Arize Phoenix
```

## The seed data

`python -m copilot.seed` builds a whole fictional store, identical on every run:

| File | Contents |
| --- | --- |
| `products.jsonl` | 135 products: coffee, equipment, merch, subscriptions, gift cards |
| `customers.jsonl` | 343 synthetic customers (all `@example.com`) |
| `orders.jsonl`, `transactions.jsonl`, `refunds.jsonl`, `disputes.jsonl` | 819 orders with payment records, including failed attempts, double captures and refunds |
| `tickets.jsonl` | 300 past, resolved tickets: the "past tickets" source for RAG. Some quote the old 14-day policy |
| `eval_tickets.jsonl` | 52 labeled tickets for agent evals, split into dev and held-out test sets |
| `rag_questions.jsonl` | 48 golden questions for retrieval evals, including 3 the knowledge base cannot answer |

Each eval ticket carries its expected intent, action (`answer`, `propose_refund`, `decline_refund`, `escalate`), refund amount, facts the reply must contain, and the documents it should cite. Refund labels are computed by `copilot.policy`, the same rules the agent's safety check uses, so labels and runtime behavior cannot drift apart.

The eval set deliberately includes messy tickets (typos, shouting, two questions in one) and prompt-injection attempts. No injection ticket may ever produce a refund proposal.

Review samples by hand with `python -m copilot.seed --sample 10`.

## The MCP servers

Two MCP servers, written with the official Python SDK, give the agent its access to the store. Both run on fake backends that serve the seed data, so they work without a Zendesk or Shopify account. A real backend implements the same small interface, and the servers do not change.

| Server | Tools | Can write |
| --- | --- | --- |
| `zendesk` | `search_tickets`, `get_ticket`, `get_customer_tickets`, `add_internal_note`, `add_tags`, `set_priority` | Internal notes, tags and priority only. No tool replies to the customer. |
| `shopify` | `get_order`, `get_customer`, `find_charges`, `check_refund_eligibility`, `propose_refund` | Refund proposals only. No tool issues a refund. |

What keeps them safe:

- **Proposals, not actions.** `propose_refund` files a proposal for a person to approve. Before filing, code checks it: the amount must fit what is still refundable (after refunds and other pending proposals), a duplicate-charge refund needs a real second capture, and an item refund must pass `copilot.policy`. A model can ask for anything; only valid proposals get through.
- **Customer text is marked as untrusted.** Ticket comments from customers come back in a `customer_text` field that the server's instructions and the agent's prompt treat as data, never as instructions.
- **Tool annotations.** Read tools are marked read-only, write tools non-destructive, so MCP clients can show and gate them correctly.
- **Audit log.** Every call writes one JSON line to stderr with the outcome and the arguments, with emails hashed and note text left out.

The tests drive the servers through a real MCP client session, and run every labeled eval ticket through the refund tools: the servers must agree with the labels.

Run a server on its own:

```bash
python -m copilot.zendesk                              # stdio, for clients that start the server
python -m copilot.shopify --transport streamable-http  # HTTP at http://127.0.0.1:8102/mcp
```

Try one interactively with the MCP Inspector (needs Node.js): `npx @modelcontextprotocol/inspector python -m copilot.shopify`

## Getting started (Windows)

Requires Python 3.11+ and Docker Desktop.

In Git Bash:

```bash
cd support-copilot
python -m venv .venv
source .venv/Scripts/activate
pip install -e ".[dev]"

pytest -q                          # run the tests
python -m copilot.seed --check     # confirm the generated data is current
python -m copilot.seed --sample 5  # read a few tickets

cp .env.example .env               # then set POSTGRES_PASSWORD
docker compose up -d               # Postgres + Phoenix (UI at http://localhost:6006)
```

In PowerShell, activate with `.venv\Scripts\Activate.ps1` and use `copy` instead of `cp`. On macOS or Linux, activate with `source .venv/bin/activate`.

## Design decisions

- **Rules in code, not in prompts.** Refund eligibility lives in `copilot/policy.py`. The model proposes; code decides whether a proposal is allowed.
- **A fixed "today".** The store's clock is pinned to 2026-10-15 so refund windows, and therefore eval labels, never drift.
- **Stale data on purpose.** Outdated policy versions and old tickets quoting them test whether retrieval prefers current sources.
- **Splits by scenario.** Two phrasings of the same scenario always land in the same split, so the held-out test set is really held out.
- **Committed, reproducible data.** CI regenerates the data and fails if the committed files differ.
- **One Postgres.** Knowledge base, approvals, job queue, LangGraph checkpoints and Phoenix all use one Postgres instance, with separate schemas.

## License

MIT
