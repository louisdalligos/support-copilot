"""HTML templates for the approval app. Jinja2 autoescaping is on: ticket text is never rendered as HTML."""

BASE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Support Copilot{% endblock %}</title>
<style>
  :root { --bg:#f7f6f3; --card:#fff; --ink:#1d1d1b; --muted:#6b6a65; --line:#e3e1db;
          --accent:#5b3a1e; --ok:#1f7a4d; --warn:#9a5b00; --bad:#a3271f; --chip:#efece6; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#161513; --card:#1f1e1b; --ink:#ecebe7; --muted:#a19f98; --line:#34322d;
            --accent:#d9a26b; --ok:#5fc48f; --warn:#e3a54a; --bad:#ef7a70; --chip:#2b2925; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; background:var(--bg); color:var(--ink); }
  header { display:flex; gap:24px; align-items:center; padding:14px 24px; border-bottom:1px solid var(--line); background:var(--card); }
  header strong { color:var(--accent); }
  header a { color:var(--muted); text-decoration:none; } header a:hover { color:var(--ink); }
  main { max-width:1180px; margin:0 auto; padding:24px; }
  h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 10px; }
  .muted { color:var(--muted); } .small { font-size:13px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; margin-bottom:14px; }
  table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  th, td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); font-weight:600; }
  tr:last-child td { border-bottom:none; }
  a { color:var(--accent); }
  .chip { display:inline-block; padding:1px 8px; border-radius:99px; background:var(--chip); font-size:12px; white-space:nowrap; }
  .chip.propose_refund { color:var(--ok); } .chip.decline_refund { color:var(--warn); }
  .chip.escalate { color:var(--bad); } .chip.approved { color:var(--ok); } .chip.rejected { color:var(--bad); }
  .chip.risk-low { color:var(--ok); } .chip.risk-medium { color:var(--warn); } .chip.risk-high { color:var(--bad); }
  .grid { display:grid; grid-template-columns: 1fr 1fr; gap:18px; }
  @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
  .msg { white-space:pre-wrap; border-left:3px solid var(--line); padding:4px 0 4px 12px; margin:8px 0; }
  .msg.customer { border-color:var(--accent); }
  .flag { color:var(--warn); margin:4px 0; }
  .chip.respond, .chip.submitted { color:var(--ok); } .chip.accept, .chip.accepted { color:var(--warn); }
  .due-soon { color:var(--bad); font-weight:600; }
  pre.fields { white-space:pre-wrap; font-size:12.5px; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:10px; overflow-x:auto; }
  .notice { padding:10px 14px; border-radius:8px; margin-bottom:16px; background:var(--chip); }
  .notice.error { color:var(--bad); }
  textarea { width:100%; min-height:260px; font:inherit; padding:10px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--ink); }
  input[type=text] { font:inherit; padding:6px 8px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--ink); }
  button { font:inherit; padding:8px 16px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--ink); cursor:pointer; }
  button.primary { background:var(--accent); border-color:var(--accent); color:var(--card); }
  .actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:12px; }
  dl { display:grid; grid-template-columns: max-content 1fr; gap:4px 14px; margin:0; } dt { color:var(--muted); }
</style>
</head>
<body>
<header>
  <strong>Northwind Support Copilot</strong>
  <a href="/">Queue</a>
  <a href="/tickets">New tickets</a>
  <a href="/disputes">Chargebacks</a>
  <span class="muted small" style="margin-left:auto">storage: {{ storage }} · model: {{ model }}</span>
</header>
<main>
{% if notice %}<div class="notice {{ notice_kind }}">{{ notice }}</div>{% endif %}
{% block content %}{% endblock %}
</main>
</body>
</html>"""

QUEUE = """{% extends "base.html" %}
{% block content %}
<h1>Approval queue</h1>
<p class="muted">Every reply and refund the copilot drafts waits here for a person. Nothing reaches a customer until you approve it.</p>

{% if chargebacks %}<div class="notice">{{ chargebacks|length }} chargeback{{ "s" if chargebacks|length > 1 }} need{{ "" if chargebacks|length > 1 else "s" }} a response; the next is due in {{ chargebacks[0].days_left }} day{{ "" if chargebacks[0].days_left == 1 else "s" }}. <a href="/disputes">Review the evidence</a></div>{% endif %}

<h2>Waiting for approval ({{ pending|length }})</h2>
{% if pending %}
<table>
  <tr><th>Ticket</th><th>Customer</th><th>Subject</th><th>Proposed</th><th>Checks</th><th>Drafted</th></tr>
  {% for r in pending %}
  <tr>
    <td><a href="/runs/{{ r.thread_id }}">#{{ r.ticket_id }}</a></td>
    <td>{{ r.customer }}</td>
    <td>{{ r.subject }}<div class="muted small">{{ r.summary }}</div></td>
    <td><span class="chip {{ r.action }}">{{ r.action|replace("_", " ") }}</span>
        {% if r.refund_amount %}<div class="small">${{ r.refund_amount }}</div>{% endif %}</td>
    <td>{% for risk in r.risks or [] %}<div class="flag small">{{ risk }}</div>{% endfor %}
        {% if r.flags %}<div class="flag small">{{ r.flags }} to check</div>{% elif not r.risks %}<span class="muted">none</span>{% endif %}</td>
    <td class="muted small">{{ r.created_at|replace("T", " ")|replace("Z", " UTC") }}</td>
  </tr>
  {% endfor %}
</table>
{% else %}<p class="muted">Nothing waiting. Run the copilot on a <a href="/tickets">new ticket</a>.</p>{% endif %}

<h2>Escalated to the team ({{ escalated|length }})</h2>
{% if escalated %}
<table>
  <tr><th>Ticket</th><th>Customer</th><th>Subject</th><th>Why</th></tr>
  {% for r in escalated %}
  <tr><td><a href="/runs/{{ r.thread_id }}">#{{ r.ticket_id }}</a></td><td>{{ r.customer }}</td><td>{{ r.subject }}</td>
      <td class="small">{{ r.reason or r.summary }}</td></tr>
  {% endfor %}
</table>
{% else %}<p class="muted">None.</p>{% endif %}

<h2>Decided</h2>
{% if decided %}
<table>
  <tr><th>Ticket</th><th>Subject</th><th>Decision</th><th>By</th><th>When</th></tr>
  {% for r in decided %}
  <tr><td><a href="/runs/{{ r.thread_id }}">#{{ r.ticket_id }}</a></td><td>{{ r.subject }}</td>
      <td><span class="chip {{ r.status }}">{{ r.status }}</span> <span class="muted small">{{ r.action|replace("_", " ") }}{% if r.reply_edited %}, reply edited{% endif %}</span></td>
      <td>{{ r.reviewer }}</td><td class="muted small">{{ r.decided_at|replace("T", " ")|replace("Z", " UTC") }}</td></tr>
  {% endfor %}
</table>
{% else %}<p class="muted">None yet.</p>{% endif %}
{% endblock %}"""

TICKETS = """{% extends "base.html" %}
{% block content %}
<h1>New tickets</h1>
<p class="muted">Run the copilot on a ticket: it reads it, looks up the order and policies, and drafts a decision for the queue.</p>
<table>
  <tr><th>Ticket</th><th>Subject</th><th>Customer's message</th><th></th></tr>
  {% for t in tickets %}
  <tr>
    <td>#{{ t.id }}</td>
    <td>{{ t.subject }}</td>
    <td class="small">{{ t.preview }}</td>
    <td>{% if t.id in runs %}<a href="/runs/{{ runs[t.id] }}">view run</a>{% else %}
      <form method="post" action="/tickets/{{ t.id }}/run"><button>Run copilot</button></form>{% endif %}</td>
  </tr>
  {% endfor %}
</table>
{% endblock %}"""

RUN = """{% extends "base.html" %}
{% block title %}Ticket #{{ record.ticket_id }} · Support Copilot{% endblock %}
{% block content %}
<p><a href="/">&larr; Queue</a></p>
<h1>#{{ record.ticket_id }} · {{ record.subject }}</h1>
<p class="muted">{{ record.customer }} · {{ state.ticket.requester.email }} ·
  <span class="chip {{ record.status if record.status in ('approved','rejected') else state.action }}">{{ record.status }}</span></p>

<div class="grid">
<div>
  <div class="card">
    <h2 style="margin-top:0">Conversation</h2>
    {% for c in state.ticket.comments %}
      {% if c.customer_text is defined %}
        <div class="msg customer">{{ c.customer_text }}</div>
      {% elif c.public %}
        <div class="msg"><span class="muted small">{{ c.author }}</span><br>{{ c.body }}</div>
      {% endif %}
    {% endfor %}
  </div>

  <div class="card">
    <h2 style="margin-top:0">What the copilot found</h2>
    <dl>
      <dt>Intent</dt><dd>{{ state.triage.intent|replace("_", " ") }}</dd>
      <dt>Order</dt><dd>{{ state.triage.order_name or "none given" }}</dd>
      {% set risk = (state.evidence.customer or {}).risk %}
      {% if risk %}<dt>Customer</dt><dd><span class="chip risk-{{ risk.level }}">{{ risk.level }} risk</span>
        <span class="muted small">{{ risk.orders }} orders · {{ risk.refunds }} refunds · {{ risk.disputes }} chargebacks</span>
        {% for r in risk.reasons %}<div class="small">{{ r }}</div>{% endfor %}</dd>{% endif %}
      {% if state.triage.chargeback_risk %}<dt>Risk</dt><dd class="flag">mentions their bank or a chargeback</dd>{% endif %}
      {% if state.triage.injection %}<dt>Warning</dt><dd class="flag">the ticket tries to instruct the assistant</dd>{% endif %}
      <dt>Decision</dt><dd><span class="chip {{ state.action }}">{{ state.action|replace("_", " ") }}</span><br>{{ state.decision.reason }}</dd>
    </dl>
    {% set order = state.evidence.order %}
    {% if order %}
    <h2>Order {{ order.name }}</h2>
    <dl>
      {% for i in order.line_items %}<dt>{{ i.quantity }} ×</dt><dd>{{ i.title }} · ${{ i.price }}</dd>{% endfor %}
      <dt>Total</dt><dd>${{ order.total }} · {{ order.financial_status|replace("_", " ") }}</dd>
      <dt>Shipped</dt><dd>{{ order.shipped_at or "not yet" }}{% if order.delivered_at %}, delivered {{ order.delivered_at }}{% endif %}</dd>
      <dt>Charges</dt><dd>{{ order.payments.successful_charges }} collected{% if order.payments.failed_attempts %}, {{ order.payments.failed_attempts }} failed{% endif %}{% if order.payments.duplicate_capture %} · <span class="flag">captured twice</span>{% endif %}</dd>
      <dt>Refunded</dt><dd>${{ order.payments.refunded }}</dd>
    </dl>
    {% endif %}
    {% if sources %}
    <h2>Sources cited</h2>
    {% for s in sources %}
      <div class="small"><a href="{{ s.url }}">{{ s.title }}</a>{% if s.sections %} <span class="muted">· {{ s.sections|join(", ") }}</span>{% endif %}</div>
    {% endfor %}
    {% endif %}
  </div>
</div>

<div>
  {% if state.proposal %}
  <div class="card">
    <h2 style="margin-top:0">Refund proposal</h2>
    <dl>
      <dt>Amount</dt><dd><strong>${{ state.proposal.amount }}</strong></dd>
      <dt>Reason</dt><dd>{{ state.proposal.reason|replace("_", " ") }}</dd>
      <dt>Rule</dt><dd>{{ state.proposal.policy_reason or state.decision.reason }}</dd>
    </dl>
    <p class="muted small">Checked against the order again when you approve.</p>
  </div>
  {% endif %}

  {% if state.flags %}
  <div class="card">
    <h2 style="margin-top:0">Check before approving</h2>
    {% for f in state.flags %}<div class="flag">⚠ {{ f }}</div>{% endfor %}
  </div>
  {% endif %}

  <div class="card">
  {% if waiting and record.status == "pending" %}
    <h2 style="margin-top:0">Draft reply</h2>
    <form method="post" action="/runs/{{ record.thread_id }}/decide">
      <textarea name="reply">{{ state.draft.reply }}</textarea>
      <div class="actions">
        {% if reviewer_locked %}<span class="small muted">Signed in as <strong>{{ reviewer }}</strong></span>{% else %}<label class="small muted">Your name <input type="text" name="reviewer" value="{{ reviewer }}" required></label>{% endif %}
        <button class="primary" name="decision" value="approve">Approve and send{% if state.proposal %} · refund ${{ state.proposal.amount }}{% endif %}</button>
        <button name="decision" value="reject">Reject</button>
      </div>
    </form>
  {% elif record.status == "escalated" %}
    <h2 style="margin-top:0">Escalated</h2>
    <p>The copilot did not draft a reply. This ticket is in the team's queue with an internal note and high priority.</p>
  {% else %}
    <h2 style="margin-top:0">{{ record.status|capitalize }} by {{ record.reviewer }}</h2>
    <p class="muted small">{{ record.decided_at|replace("T", " ")|replace("Z", "") }}</p>
    {% if state.approval and state.approval.approved %}<div class="msg">{{ state.approval.reply }}</div>{% endif %}
  {% endif %}
  </div>
</div>
</div>
{% endblock %}"""

DISPUTES = """{% extends "base.html" %}
{% block content %}
<h1>Chargebacks</h1>
<p class="muted">Open disputes, soonest deadline first. For each one the copilot assembles the evidence from the store's records and recommends whether to respond or accept. A person submits it; nothing goes to the bank from here.</p>
{% if disputes %}
<table>
  <tr><th>Dispute</th><th>Order</th><th>Reason</th><th>Amount</th><th>Evidence due</th><th>Recommended</th><th>Status</th></tr>
  {% for d in disputes %}
  <tr>
    <td><a href="/disputes/{{ d.id }}">#{{ d.id }}</a></td>
    <td>{{ d.order_name }}</td>
    <td>{{ d.reason_label }}</td>
    <td>${{ d.amount }}</td>
    <td>{{ d.evidence_due_by }} <span class="small {{ 'due-soon' if d.days_left is not none and d.days_left <= 3 else 'muted' }}">{% if d.days_left is not none %}{% if d.days_left < 0 %}overdue{% else %}{{ d.days_left }} day{{ "" if d.days_left == 1 else "s" }} left{% endif %}{% endif %}</span></td>
    <td><span class="chip {{ d.recommendation }}">{{ d.recommendation }}</span></td>
    <td>{% if d.decision %}<span class="chip {{ d.decision.decision }}">{{ d.decision.decision }}</span> <span class="muted small">by {{ d.decision.reviewer }}</span>{% else %}<span class="muted">needs a response</span>{% endif %}</td>
  </tr>
  {% endfor %}
</table>
{% else %}<p class="muted">No open chargebacks.</p>{% endif %}
{% endblock %}"""

DISPUTE = """{% extends "base.html" %}
{% block content %}
{% set d = packet.dispute %}{% set o = packet.order %}{% set rec = packet.recommendation %}
<p><a href="/disputes">&larr; Chargebacks</a></p>
<h1>Chargeback #{{ d.id }} · {{ d.reason_label }} · ${{ d.amount }}</h1>
<p class="muted">Order {{ o.name }} · {{ packet.customer.name }} · opened {{ d.opened }} ·
  evidence due {{ d.due }}{% if d.days_left is not none %} <span class="{{ 'due-soon' if d.days_left <= 3 else '' }}">({% if d.days_left < 0 %}overdue{% else %}{{ d.days_left }} day{{ "" if d.days_left == 1 else "s" }} left{% endif %})</span>{% endif %}</p>

<div class="grid">
<div>
  <div class="card">
    <h2 style="margin-top:0">Recommendation: <span class="chip {{ rec.action }}">{{ rec.action }}</span></h2>
    <p>{{ rec.why }}</p>
    {% if packet.rebuttal %}<h2>Rebuttal</h2><div class="msg">{{ packet.rebuttal }}</div>{% endif %}
  </div>
  <div class="card">
    <h2 style="margin-top:0">Timeline</h2>
    <dl>{% for e in packet.timeline %}<dt>{{ e.date }}</dt><dd>{{ e.event }}</dd>{% endfor %}</dl>
  </div>
  {% for key, ev in packet.evidence.items() %}
  <div class="card">
    <h2 style="margin-top:0">{{ ev.title }}</h2>
    <p class="small">{{ ev.summary }}</p>
    {% if ev.rows %}<table>{% for r in ev.rows %}<tr>{% for c in r %}<td class="small">{{ c }}</td>{% endfor %}</tr>{% endfor %}</table>{% endif %}
    {% for m in ev.messages or [] %}<div class="msg {{ 'customer' if m.from == 'Customer' else '' }}"><span class="muted small">{{ m.date }} · {{ m.from }}</span><br>{{ m.text }}</div>{% endfor %}
  </div>
  {% endfor %}
  {% if packet.policy %}<div class="card"><h2 style="margin-top:0">{{ packet.policy.title }}</h2><p class="small">{{ packet.policy.text }}</p></div>{% endif %}
</div>

<div>
  <div class="card">
    <h2 style="margin-top:0">Evidence file</h2>
    <p class="small">One PDF, grouped by evidence type, within the card networks' limits (4.5 MB, 19 pages).</p>
    <p><a href="/disputes/{{ d.id }}/evidence.pdf"><button type="button">Download evidence PDF</button></a></p>
  </div>
  <div class="card">
    <h2 style="margin-top:0">Shopify evidence fields</h2>
    <p class="small muted">The values for Shopify's dispute evidence form (ShopifyPaymentsDisputeEvidenceUpdateInput). Upload the PDF as the uncategorized file. Submit stays off: you submit.</p>
    <pre class="fields">{{ fields_json }}</pre>
  </div>
  <div class="card">
  {% if decision %}
    <h2 style="margin-top:0">Marked {{ decision.decision }} by {{ decision.reviewer }}</h2>
    <p class="muted small">{{ decision.decided_at|replace("T", " ")|replace("Z", " UTC") }}{% if decision.decision == "submitted" and decision.recommended == "accept" or decision.decision == "accepted" and decision.recommended == "respond" %} · differs from the recommendation ({{ decision.recommended }}){% endif %}</p>
  {% elif d.open %}
    <h2 style="margin-top:0">After you act in Shopify</h2>
    <p class="small">Submit the evidence in the Shopify admin (the order's chargeback page), or accept the chargeback there, then record it here.</p>
    <form method="post" action="/disputes/{{ d.id }}/decide">
      <div class="actions">
        {% if reviewer_locked %}<span class="small muted">Signed in as <strong>{{ reviewer }}</strong></span>{% else %}<label class="small muted">Your name <input type="text" name="reviewer" value="{{ reviewer }}" required></label>{% endif %}
        <button class="{{ 'primary' if rec.action == 'respond' else '' }}" name="decision" value="submitted">Evidence submitted</button>
        <button class="{{ 'primary' if rec.action == 'accept' else '' }}" name="decision" value="accepted">Accepted</button>
      </div>
    </form>
  {% else %}
    <h2 style="margin-top:0">Closed</h2><p class="muted small">Status: {{ d.status|replace("_", " ") }}</p>
  {% endif %}
  </div>
</div>
</div>
{% endblock %}"""

TEMPLATES = {
    "base.html": BASE,
    "queue.html": QUEUE,
    "tickets.html": TICKETS,
    "run.html": RUN,
    "disputes.html": DISPUTES,
    "dispute.html": DISPUTE,
}
