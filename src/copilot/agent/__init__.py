"""The support agent: a LangGraph workflow over the Zendesk, Shopify and policy MCP servers.

Language work goes to the model: reading the ticket, and drafting a reply that
cites policy. Decisions that involve money stay in code: which action to take,
whether a refund is allowed, and how much. Every customer-visible reply and
every refund waits for a person to approve it.
"""
