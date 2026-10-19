# AI Support Copilot

An AI support agent for a fictional online coffee store, Northwind Coffee Co. It reads customer tickets, answers from the store's policies and order data with citations, and proposes refunds for a human to approve.

It is a portfolio project, built to show production AI engineering end to end: RAG, custom MCP servers, a LangGraph agent, LLM APIs and open models, evals in CI, and LLMOps. Everything runs on open-source tools.

## Status

| Phase | What | State |
| --- | --- | --- |
| 1. Foundations | Knowledge base, seed data generator, refund rules in code, CI | **Done** |
| 2. Thin slice | Zendesk and Shopify MCP servers (fake backends), policy RAG, agent via CLI, evals in CI | Next |
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
tests/                 tests for the rules, the knowledge base and the data
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

## Getting started (Windows, PowerShell)

Requires Python 3.11+ and Docker Desktop.

```powershell
cd "C:\Users\WIN11PC\AI LLM\support-copilot"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

pytest -q                          # run the tests
python -m copilot.seed --check     # confirm the generated data is current
python -m copilot.seed --sample 5  # read a few tickets

copy .env.example .env             # then set POSTGRES_PASSWORD
docker compose up -d               # Postgres + Phoenix (UI at http://localhost:6006)
```

On macOS or Linux, activate with `source .venv/bin/activate` and use `cp` instead of `copy`.

## Design decisions

- **Rules in code, not in prompts.** Refund eligibility lives in `copilot/policy.py`. The model proposes; code decides whether a proposal is allowed.
- **A fixed "today".** The store's clock is pinned to 2026-10-15 so refund windows, and therefore eval labels, never drift.
- **Stale data on purpose.** Outdated policy versions and old tickets quoting them test whether retrieval prefers current sources.
- **Splits by scenario.** Two phrasings of the same scenario always land in the same split, so the held-out test set is really held out.
- **Committed, reproducible data.** CI regenerates the data and fails if the committed files differ.
- **One Postgres.** Knowledge base, approvals, job queue, LangGraph checkpoints and Phoenix all use one Postgres instance, with separate schemas.

## License

MIT
