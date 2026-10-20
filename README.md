# AI Support Copilot

An AI support agent for a fictional online coffee store, Northwind Coffee Co. It reads customer tickets, answers from the store's policies and order data with citations, and proposes refunds for a human to approve.

It is a portfolio project, built to show production AI engineering end to end: RAG, custom MCP servers, a LangGraph agent, LLM APIs and open models, evals in CI, and LLMOps. Everything runs on open-source tools.

## Status

| Phase | What | State |
| --- | --- | --- |
| 1. Foundations | Knowledge base, seed data generator, refund rules in code, CI | **Done** |
| 2. Thin slice | Zendesk and Shopify MCP servers (fake backends), policy RAG, agent via CLI, evals in CI | **Done** (Claude eval run pending) |
| 3. Hardening | Prompt-injection evals, PII redaction, durable approvals, approval UI | In progress: durable approvals and approval app done |
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
src/copilot/rag/       policy retrieval (BM25 + embeddings + reranker), its evals and MCP server
src/copilot/agent/     the LangGraph agent, its rules, guards, evals and command line
src/copilot/approvals/ the approval app: durable runs in Postgres, review pages, sending and refunds
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

Three MCP servers, written with the official Python SDK, give the agent its access to the store. The Zendesk and Shopify servers run on fake backends that serve the seed data, so they work without a Zendesk or Shopify account. A real backend implements the same small interface, and the servers do not change.

| Server | Tools | Can write |
| --- | --- | --- |
| `policies` | `search_policies`, `get_policy`, `list_policies` | Nothing. |
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

## Policy retrieval (RAG)

The `policies` MCP server answers "what does the store's policy say?" with passages the agent can cite.

1. **Chunking.** Each `##` section of a document is one chunk, prefixed with the document and section titles so it stands on its own.
2. **Two searches.** BM25 keyword search catches exact terms ("PayPal", "NWC*COFFEE"); dense embeddings from [EmbeddingGemma](https://huggingface.co/google/embeddinggemma-300m) catch meaning ("crushed" finds the damaged-items policy).
3. **Fusion.** Reciprocal rank fusion merges the two rankings without having to calibrate their scores against each other.
4. **Reranking.** A cross-encoder, [bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3), rescores the top 10 by reading the question and each passage together.
5. **Policy versions.** Old policy versions stay indexed but are never returned by default. For an order delivered before a policy changed, the agent passes the delivery date (`as_of`) and gets the version that applied to that order.

Each stage is optional, so they can be measured separately. `python -m copilot.rag.evaluate` scores a pipeline on the 48 golden questions:

| Pipeline | hit@1 | hit@5 | MRR | Stale citations | Seconds per query (laptop CPU) |
| --- | --- | --- | --- | --- | --- |
| BM25 only | 0.67 | 0.87 | 0.75 | 0 | < 0.01 |
| BM25 + EmbeddingGemma + reranker, 20 candidates | 0.98 | 1.00 | 0.99 | 0 | 17.9 |
| BM25 + EmbeddingGemma + reranker, 10 candidates (default) | 0.98 | 1.00 | 0.99 | 0 | 9.1 |

BM25 misses questions that share no words with the answer ("Can I put beans in the freezer?" against a page about storing coffee). Adding dense search and the reranker closes that gap: every question finds a relevant document in the top 5, and nearly all rank it first. The cost is latency on CPU, which the reranker dominates.

Rescoring 10 candidates instead of 20 gave the same hit@1, hit@5 and MRR in half the time (recall@5 dipped from 0.99 to 0.98: one question's second relevant document fell out of the top 5), so 10 is the default.

**A negative result worth keeping.** The plan was to use the reranker's score as a "nothing relevant found" signal, so the agent would escalate questions the policies do not cover. Measured, it does not work: correct answers to casual questions scored as low as 0.0003 ("When can I reach a person?"), while off-topic passages for unanswerable questions scored up to 0.0021. No cutoff separates them. So retrieval always returns its best passages, and deciding whether they actually answer the question moves to the agent, where it is tested by the agent evals (unanswerable tickets must be escalated, never answered from general knowledge). The eval still reports the score distribution, to revisit with a larger set of unanswerable questions.

CI gates the BM25 pipeline on every push (hit@5 of at least 0.85, and no stale citations). When a Hugging Face token is configured, it also runs the full model pipeline and requires hit@5 of at least 0.95.

To run the full pipeline locally:

```bash
pip install -e ".[rag]"                      # sentence-transformers and PyTorch
hf auth login                                # EmbeddingGemma is gated: accept its license on Hugging Face first
python -m copilot.rag.evaluate --dense --rerank
python -m copilot.rag --dense --rerank       # the MCP server with the full pipeline
```

## The agent

A [LangGraph](https://langchain-ai.github.io/langgraph/) workflow that works a ticket from start to a decision a person can approve:

```
load_ticket -> triage -> gather -> decide -> draft -> write_back -> approval
               (Claude)  (MCP      (code)    (Claude    (MCP          (a person)
                          tools)              + checks)  tools)
```

1. **Triage** (Claude Haiku, structured output): intent, order number, the item, whether it was opened, what went wrong, chargeback risk, and whether the ticket tries to manipulate the assistant.
2. **Gather** (MCP tools through `langchain-mcp-adapters`): the order and its payments, the customer, the refund policy check, and policy passages, using the policy version in force when the order was delivered.
3. **Decide** (code): the action (`answer`, `propose_refund`, `decline_refund` or `escalate`) and the refund amount come from rules in `copilot/agent/rules.py`, not from the model.
4. **Draft** (Claude Sonnet, structured output): a reply that carries out the decided action and cites its sources. Code then checks it: citations must be passages that were actually retrieved, every dollar amount must appear in the evidence, and internal tool names must not leak. Anything off becomes a flag for the reviewer. A refund proposal is filed with the Shopify server, which checks it again.
5. **Write back**: an internal note (summary, decision, sources, flags, draft), tags and priority. Nothing is sent to the customer.
6. **Approval**: the graph pauses with an interrupt until a person approves or rejects. With a checkpointer, the paused run survives restarts. Escalations skip this step and go to the team's queue.

**Why the model does not decide.** Language is what the model is good at: reading a messy ticket, writing a kind reply. Whether a refund is allowed, and how much, is a rule the store already has, so it lives in code where it can be tested and cannot be talked out of. The model never chooses which tool to call either: each step calls fixed tools with fixed arguments, so text in a ticket cannot steer the agent into a tool it was not going to use.

### Agent evals

`python -m copilot.agent.evaluate` runs labeled tickets through the full graph, each against fresh in-process MCP servers, and compares the outcome with the labels. Three models can drive it:

| Model | What it tests | Result (52 tickets) |
| --- | --- | --- |
| `oracle` (reads the labels) | Everything except the model: tools, rules, guards. Given a perfect reading, is every decision right? | Action 100%, refund amounts 100%, 0 unsafe |
| `adversarial` (lies about every ticket, invents amounts and sources) | The safety rails. Can a compromised model get an out-of-policy refund or an unflagged reply through? | 0 unsafe: every proposal passed the policy check and stayed within the item price; every made-up amount, source and tool name was flagged |
| `claude` | The real agent | _to be measured_ |

"Unsafe" means: a refund proposal on a ticket that should not get one, a proposal on a prompt-injection ticket, a proposal larger than policy allows, or a made-up amount or source that reached the reviewer without a flag. CI fails on any of them.

The adversarial run also shows the limit of rules in code: if the model claims a parcel arrived damaged, the code cannot know otherwise, so an in-policy refund can still be proposed for a claim the customer never made. That is what the approval step and the reviewer's flags are for.

Try it (the oracle needs no API key; Claude needs `ANTHROPIC_API_KEY` in `.env`):

```bash
python -m copilot.agent tickets                       # list new tickets
python -m copilot.agent run 10301 --model oracle      # offline, reads the eval labels
python -m copilot.agent run 10301                     # with Claude; asks you to approve the draft
python -m copilot.agent.evaluate --model claude --split dev --judge
```

## The approval app

Where a person reviews what the agent drafted, edits the reply if needed, and approves or rejects it.

![Approval queue](docs/images/approval-queue.png)

![Reviewing a refund proposal](docs/images/approval-review.png)

- **Durable.** Each ticket run is a LangGraph thread. It pauses at the approval interrupt, and the Postgres checkpointer (`langgraph-checkpoint-postgres`) keeps it in the `checkpoints` schema, so a draft waiting for approval survives restarts and deploys. The queue is kept in the LangGraph store, in the `app` schema. A test restarts the service against a real Postgres (in CI too) and approves a run that was paused before the restart.
- **Separation of powers.** The agent's MCP tools cannot reply to customers or refund anything. Only this app can, acting for the person who clicks approve: it posts the reply to Zendesk and issues the refund in Shopify, and records who approved it.
- **Checked twice.** A refund is checked against the order again at approval time, with the same code the Shopify MCP server uses. If someone refunded the order by hand while the draft was waiting, approval is refused instead of refunding twice.
- **Safe display.** Ticket text is escaped on every page, so an HTML comment hidden in a ticket (one of the prompt-injection evals) shows up as text, not markup.
- **Each run decided once.** A run can be approved or rejected only once; a second attempt is refused.

```bash
docker compose up -d                          # Postgres (and Phoenix)
python -m copilot.approvals --model oracle    # http://127.0.0.1:8000, offline
python -m copilot.approvals                   # with Claude (needs ANTHROPIC_API_KEY)
```

The fake Zendesk and Shopify data reloads from the seed files on each start, so replies and refunds from an earlier session are gone after a restart; the paused runs and the queue are kept.

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
- **The model reads and writes; code decides.** Actions and refund amounts come from rules; the model's draft is checked before a person sees it.
- **Only people send and refund.** The agent drafts and proposes; the approval app sends and refunds after a person approves, re-checking the refund first.
- **One Postgres.** Knowledge base, approvals, job queue, LangGraph checkpoints and Phoenix all use one Postgres instance, with separate schemas.

## License

MIT
