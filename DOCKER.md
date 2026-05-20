# Docker Deployment

Run the demo stack with Docker Compose. The compose topology is **five
services** (see `docker-compose.yml`):

| Service        | Container port | Host publish                       | Role |
|----------------|----------------|------------------------------------|------|
| `orchestrator` | 8080           | `8090:8080` + `127.0.0.1:8123:8080`| Browser SPA host (static files from `client/`) + BFF + A2A client + LLM router/composer + back-channel-logout receiver. `8090` is user-facing; `8123` (loopback) is where WSO2 IS posts `/backchannel-logout` (reached via a reverse-SSH tunnel on the laptop). |
| `hr_agent`     | 8001           | `127.0.0.1:8001:8001`              | HR specialist agent; per-agent CIBA → `hr_server` MCP tools. |
| `it_agent`     | 8002           | `127.0.0.1:8002:8002`              | IT specialist agent; per-agent CIBA → `it_server` MCP tools. |
| `hr_server`    | 8000           | `127.0.0.1:8000:8000`              | HR MCP tools + REST (leave, cubicles, reports). Loopback-only. |
| `it_server`    | 8004           | `127.0.0.1:8004:8004`              | IT MCP tools + REST (assets, reports). Loopback-only. |

> The pre-v4 standalone `agent` (5001) and `client` (3001) services were
> removed. There is no separate SPA or agent container, and no ports 3001/5001.
> The SPA is bundled into the orchestrator image and served at
> **http://localhost:8090**.

External dependencies (not containerised here):

- **Identity provider** — on-prem **WSO2 Identity Server** (dev:
  `https://13.60.190.47:9443`). All five services validate/mint tokens against it.
- **LLM** — **OpenAI** (`gpt-4.1`) reached through the **WSO2 Agent Manager
  (embedded WSO2 AI Gateway)**, which is OpenAI-compatible. Configured via the
  `OPENAI_*` vars in `orchestrator/.env`.

## Prerequisites

- Docker + Docker Compose v2 (`docker compose version` → v2.x)
- Python 3 (for the smoke/preflight scripts)
- A populated `.env` in each service directory. Copy from the matching
  `.env.example` and fill in the IS-issued client IDs / secrets:
  - `orchestrator/.env`
  - `hr_agent/.env`
  - `it_agent/.env`
  - `hr_server/.env`
  - `it_server/.env`

  For where each value comes from (IS Console paths, agent credentials, scopes),
  see **README.md**, **`docs/wso2-is-setup.md`**, and
  **`docs/wso2-is-rebuild-runbook.md`** — not duplicated here.

## Quick Start

The reliable entry point is the bring-up script (build → start → healthz smoke):

```bash
./scripts/demo-up.sh            # build with cached layers, start, smoke
./scripts/demo-up.sh --clean    # down + no-cache rebuild of all images, then start + smoke
./scripts/demo-up.sh --no-build # start existing images, no rebuild
./scripts/demo-down.sh          # stop + remove containers
```

Equivalent `make` targets exist: `make demo-up`, `make demo-down`, `make demo-smoke`.

Once up, open **http://localhost:8090** and sign in with a demo user
(`employee@example.com` / `hradmin@example.com`, password `NewsMax@1234`).

> **Shared secret:** all five services read `INTERNAL_REVOKE_SHARED_SECRET`
> (revocation fan-out auth). Set it once in your shell or `.env` and bring the
> whole fleet up together — a partial restart can split containers across stale
> vs current values and cause `401 invalid_secret` on the fan-out legs.

## Verifying

```bash
# Healthz smoke across all five containers
python3 scripts/demo-smoke.py

# Full IS preflight (run this first if anything 401s)
./scripts/check-is-config.py
```

`demo-up.sh` already runs the healthz smoke (`--skip-chat-test`) at the end of
bring-up.

## Viewing Logs

```bash
docker compose ps                       # running containers + health
docker compose logs -f orchestrator     # follow one service
docker compose logs --tail=50 hr_agent  # last 50 lines

# All five at once
docker compose logs -f orchestrator hr_agent it_agent hr_server it_server
```

### What to look for

- **MCP servers** (`hr_server` / `it_server`) log token enforcement on startup,
  including the `expected_aud` they will accept on inbound token-B — useful for
  catching an audience misconfiguration without decoding a token.
- **Orchestrator** logs `orchestrator_config_loaded` (IS URL, agent URLs, LLM
  mode/model) on startup, and `auth_exchange_success` after a successful
  Pattern C sign-in.

## Common Operations

```bash
# Rebuild + restart a single service
docker compose build orchestrator
docker compose up -d orchestrator

# Restart a single service
docker compose restart hr_agent

# Pull latest code and redeploy
git pull
docker compose build --no-cache
docker compose up -d
```

## Networking

Inside the `demo-net` bridge, services reach each other by compose service name:

- `orchestrator` → `http://hr_agent:8001`, `http://it_agent:8002`,
  `http://hr_server:8000`, `http://it_server:8004`
- agents → their MCP backends (`http://hr_server:8000`, `http://it_server:8004`)

The browser runs on the host, so it uses `http://localhost:8090`. These
in-cluster URLs are set in `docker-compose.yml`; your `.env` files don't need
to repeat them.

## Troubleshooting

| Problem | Check |
|---------|-------|
| Sign-in shows "temporarily unavailable" | An agent's 4-value credential is stale. Run `./scripts/check-is-config.py` (Section 4d); regenerate the secret in IS Console and update the service `.env`, then re-run `./scripts/demo-up.sh --no-build`. |
| Fan-out legs return `401 invalid_secret` | `INTERNAL_REVOKE_SHARED_SECRET` drifted across containers. Re-run `./scripts/demo-up.sh --clean`. |
| Browser CORS errors | `ALLOWED_ORIGINS` must include the origin you load the SPA from (default `http://localhost:8090`). It's set per service in `docker-compose.yml`; override via the `ALLOWED_ORIGINS` env var. |
| Token validation / 401s | Run `./scripts/check-is-config.py`. Confirm `WSO2_IS_BASE_URL` / JWKS are reachable from inside the container and the OAuth apps are subscribed to their API scopes. |
| RP-initiated logout rejected | `POST_LOGOUT_REDIRECT_URI` must be registered on `orchestrator-mcp-client`'s callback URLs in IS. See `scripts/set-bcl-url.sh` and `check-is-config.py` §4b. |
