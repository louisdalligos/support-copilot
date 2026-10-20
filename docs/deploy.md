# Deploying to one VM

For AWS, with Terraform and deploys from GitHub Actions, follow [aws.md](aws.md) instead. This guide works on any VM.

The whole system runs on one small Linux VM with Docker: the approval app, Postgres, Phoenix, Caddy and a backup job. About 2 GB of RAM is enough without the open models; use 4 GB if you build the image with `RAG_MODELS=true`.

```
internet ──443──> Caddy ──> app:8000        (approval app, agent, MCP servers in-process)
                   │  └──> phoenix:6006     (traces)
                   │
            sign-in, HTTPS          app ──> postgres:5432 (checkpoints, queue, Phoenix data)
                                    backup ──> pg_dump every 24 h ──> ./backups
```

Only Caddy listens on the internet. Postgres and Phoenix are bound to `127.0.0.1` on the VM and reachable by the other containers on the Docker network.

## 1. A VM and a domain

- Any VM with Ubuntu 24.04 and a public IP, amd64 or arm64.
- Two DNS `A` records pointing at the VM's IP: `copilot.yourdomain.com` and `phoenix.copilot.yourdomain.com`.
- Open ports 22, 80 and 443 in the VM's firewall. Nothing else.

## 2. Docker and the repository

```bash
ssh you@your-vm
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

git clone https://github.com/louisdalligos/support-copilot.git
cd support-copilot
```

## 3. Settings

```bash
cp .env.example .env
docker run --rm caddy:2.10-alpine caddy hash-password --plaintext 'a long password'
```

Then edit `.env`:

```bash
POSTGRES_PASSWORD=<a long random string>
DOMAIN=copilot.yourdomain.com
ACME_EMAIL=you@example.com             # for Let's Encrypt certificate notices
ADMIN_USER=reviewer                     # recorded as the reviewer on every decision
ADMIN_PASSWORD_HASH='$2a$14$...'        # the hash from above, in single quotes
COPILOT_REDACTION_KEY=<a long random string>
COPILOT_MODEL=claude                    # or oracle for a free demo on the eval tickets
ANTHROPIC_API_KEY=sk-ant-...            # only for claude; set a monthly spend limit on the key
```

The single quotes around the hash matter: it contains `$` characters, which Docker Compose would otherwise read as variables.

## 4. Start

The CI pipeline publishes the image to GitHub Container Registry on every push to `main`. If the package is private, log in first with a GitHub token that can read packages: `echo <token> | docker login ghcr.io -u <user> --password-stdin`. Or build on the VM instead with `--build`.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps     # all healthy
```

Open `https://copilot.yourdomain.com`, sign in, and run the copilot on a ticket. Traces appear at `https://phoenix.copilot.yourdomain.com`.

## Updating

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull app
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Drafts waiting for approval survive the restart: their runs are checkpointed in Postgres.

## Backups and restore

The `backup` service dumps both databases every 24 hours to `./backups` and keeps 7 days (`BACKUP_KEEP_DAYS`, `BACKUP_INTERVAL_SECONDS`). Copy them off the VM regularly, for example with `rsync` from your own machine.

To restore the app database:

```bash
docker compose exec -T postgres pg_restore --clean --if-exists -U copilot -d copilot < backups/copilot-<stamp>.dump
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart app
```

Test a restore at least once before you need it.

## What it costs

- The VM: a few dollars a month for 2 GB.
- Claude: about one cent per ticket with the default models (see the README). With `COPILOT_MODEL=oracle` it costs nothing, but only works on the labeled eval tickets.
- Everything else (Postgres, Phoenix, Caddy, Let's Encrypt) is free and open source.

## Security notes

- One shared sign-in protects both the app and Phoenix. For a team, put an identity provider in front instead (for example Caddy with an OAuth plugin or Cloudflare Access) and keep `COPILOT_TRUSTED_USER_HEADER` pointing at the header it sets.
- The app refuses form posts from other sites, sends a strict Content Security Policy, and records the signed-in user (not a typed name) as the reviewer.
- Secrets live only in `.env` on the VM, never in the image or the repository.
