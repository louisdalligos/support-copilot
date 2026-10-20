# Connecting a real Zendesk account

By default the copilot reads the seed tickets. With `COPILOT_ZENDESK=live` it uses a real Zendesk account instead, through the Zendesk Support API (`copilot/zendesk/live.py`). Nothing else changes: the MCP server, the agent and the approval app see the same ticket shapes. The agent's draft is added to the ticket as an internal note, and when a person approves, the reply is posted on the ticket as a public comment.

With a webhook, the copilot also starts on its own: Zendesk calls the approval app when a ticket is created, and the draft is waiting in the queue a few seconds later.

## 1. An account and an OAuth client

A Zendesk trial lasts 14 days, so finish steps 2 and 3 on your own machine first and keep the trial for the parts that need it.

1. Start a free Zendesk trial. The subdomain you choose is the `acme` in `acme.zendesk.com`.
2. **Admin Center** > **Apps and integrations** > **APIs** > **OAuth clients** > **Add OAuth client**:
   - **Client kind:** Confidential
   - **Redirect URLs:** none needed
   - Save, then copy the **Identifier** and the **Secret**. The secret is shown once.

The copilot signs in with the OAuth client credentials grant: it exchanges the identifier and secret at `https://acme.zendesk.com/oauth/tokens` for a bearer token that lasts 30 minutes, and gets a new one before it expires. Zendesk is retiring API tokens (no new ones from 27 October 2026, and the rest stop working on 30 April 2027), and recommends this grant for server-to-server integrations.

Zendesk records everything done with the client's tokens as the user who created the client. On a trial that is you, so the copilot's internal notes and the approved replies appear under your name. On a real account, create the client from a dedicated agent such as "Support Copilot".

## 2. Settings

In `.env` (git-ignored):

```bash
COPILOT_ZENDESK=live
ZENDESK_SUBDOMAIN=acme
ZENDESK_CLIENT_ID=...
ZENDESK_CLIENT_SECRET=...
# ZENDESK_SCOPES=read write   # the default
```

The demo tickets refer to orders in the Shopify demo store, so use the live Zendesk account together with the live Shopify store ([docs/shopify.md](shopify.md)).

## 3. Demo tickets

```bash
python -m copilot.zendesk.demo_desk setup      # safe to run again; imports only what is missing
python -m copilot.zendesk.demo_desk status
```

This imports the 52 labeled demo tickets (10301 to 10352 in the seed data) with their customers, through Zendesk's ticket import API. Their dates move forward so each ticket keeps its age, like the Shopify orders. Imported tickets do not fire triggers, so importing them does not start 52 copilot runs.

Zendesk numbers tickets itself (1, 2, 3, ...). Each demo ticket keeps its seed id in its external ID (`northwind-10301`) and is tagged `northwind-demo`.

Try one without a webhook:

```bash
python -m copilot.approvals --model claude     # Tickets page > Run copilot on a ticket, then approve it
```

## 4. The webhook

The webhook needs a public HTTPS address, so it runs on the deployed server ([docs/aws.md](aws.md)). The app answers on `/webhooks/zendesk`. Caddy lets this path through without the sign-in, and the app refuses any request that Zendesk did not sign.

1. **Admin Center** > **Apps and integrations** > **Webhooks** > **Create webhook** > **Trigger or automation**.
   - **Endpoint URL:** `https://<your domain>/webhooks/zendesk`
   - **Request method:** POST, **Request format:** JSON, **Authentication:** none (the signature is the authentication)
2. Create it, open it and **Reveal secret**. This is `ZENDESK_WEBHOOK_SECRET`.
3. **Admin Center** > **Objects and rules** > **Triggers** > **Add trigger**:
   - **Conditions (all):** `Ticket` `Is` `Created`
   - **Actions:** `Notify by` > `Active webhook` > the webhook, with the JSON body
     ```json
     {"ticket_id": "{{ticket.id}}"}
     ```

Only "ticket is created" starts a run. The copilot's own internal note updates the ticket, so a trigger on updates would start it again.

What the endpoint does with each call:

- Checks the signature: `base64(HMAC-SHA256(secret, timestamp + body))`, from the `X-Zendesk-Webhook-Signature` and `X-Zendesk-Webhook-Signature-Timestamp` headers. A wrong signature, or a timestamp more than five minutes off, gets 401, so an intercepted request cannot be replayed later.
- Skips a ticket that already has a run. Zendesk retries deliveries, and a retry never makes a second draft.
- Answers 202 at once and runs the copilot in the background, one ticket at a time. A run that fails is logged, and the next delivery for that ticket tries again.
- Without `ZENDESK_WEBHOOK_SECRET` set, the endpoint answers 404.

To see it work, create a ticket as a customer would:

```bash
python -m copilot.zendesk.demo_desk send 10301     # a new ticket with the text of demo ticket 10301
```

The draft appears in the approval app's queue, and as an internal note on the ticket in Zendesk.

## 5. Running it on the server

The server builds its `.env` from the SSM parameters under `/support-copilot/prod`, so live mode is a few parameters and a redeploy. Run these with your own AWS profile; the secrets go in as `SecureString` and never touch the repository:

```bash
p=/support-copilot/prod
aws ssm put-parameter --name $p/COPILOT_ZENDESK --type String --value live --overwrite
aws ssm put-parameter --name $p/ZENDESK_SUBDOMAIN --type String --value acme --overwrite
aws ssm put-parameter --name $p/ZENDESK_CLIENT_ID --type SecureString --value '...' --overwrite
aws ssm put-parameter --name $p/ZENDESK_CLIENT_SECRET --type SecureString --value '...' --overwrite
aws ssm put-parameter --name $p/ZENDESK_WEBHOOK_SECRET --type SecureString --value '...' --overwrite
# and the Shopify settings from docs/shopify.md, with COPILOT_SHOPIFY=live
gh workflow run deploy.yml
```

To go back to the seed data (for example when the trial ends), delete `COPILOT_ZENDESK` and `COPILOT_SHOPIFY` with `aws ssm delete-parameter` and deploy again.

## How it maps to Zendesk

| Copilot | Zendesk |
| --- | --- |
| Ticket, subject, status, priority, tags | Ticket |
| Requester name and email | The requester (a Zendesk user) |
| Conversation | Ticket comments; a comment by the requester is the customer's, any other is an agent's |
| Search by text, status, tag or customer | The search API (`type:ticket`, `status:`, `tags:`, `requester:`) |
| Draft for review | Internal note (a private comment) |
| Approved reply | Public comment, then status solved |
