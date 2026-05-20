# VM Deployment Guide

Deploys the five-container demo stack (orchestrator, hr_agent, it_agent, hr_server,
it_server) onto a fresh AWS EC2 instance. The identity provider is on-prem **WSO2
Identity Server**, which continues to run on its own VM at `13.60.190.47:9443`.
The LLM is **OpenAI** (`gpt-4.1`) reached through the **WSO2 Agent Manager
(embedded WSO2 AI Gateway)** — configured via the `OPENAI_*` vars in
`orchestrator/.env`.

---

## 1. Provision the EC2 Instance

| Setting | Value |
|---|---|
| AMI | Ubuntu 24.04 LTS |
| Instance type | `t3.large` (2 vCPU, 8 GB) minimum · `t3.xlarge` recommended |
| Storage | 30 GB gp3 |
| Key pair | Use the same `dda-poc-key.pem` (or generate a new one) |

### Security Group — Inbound Rules

| Port | Source | Purpose |
|---|---|---|
| 22 | Your IP | SSH management |
| 8090 | 0.0.0.0/0 | SPA + orchestrator API (user-facing) |
| 8123 | IS VM private IP (`13.60.190.47/32`) | Back-channel logout from IS |

> Place both VMs in the **same VPC** so ports 8123 and 9443 can communicate
> over private IPs without internet exposure.

---

## 2. Install Prerequisites on the VM

```bash
ssh -i dda-poc-key.pem ubuntu@<VM_PUBLIC_IP>

# System update
sudo apt-get update && sudo apt-get upgrade -y

# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
newgrp docker                    # apply group without logout

# Docker Compose plugin (bundled with Docker >= 24)
docker compose version           # verify — should print v2.x

# Git
sudo apt-get install -y git
```

---

## 3. Clone the Repository

```bash
git clone https://github.com/ramith/smart-employee-assistant-ai-demo.git
cd smart-employee-assistant-ai-demo
```

---

## 4. Configure Environment Files

Copy the `.env` files from your local machine to the VM, then apply the changes
in the sections below.

```bash
# From your local machine — run once per service
scp -i dda-poc-key.pem orchestrator/.env  ubuntu@<VM_PUBLIC_IP>:~/smart-employee-assistant-ai-demo/orchestrator/.env
scp -i dda-poc-key.pem hr_server/.env     ubuntu@<VM_PUBLIC_IP>:~/smart-employee-assistant-ai-demo/hr_server/.env
scp -i dda-poc-key.pem it_server/.env     ubuntu@<VM_PUBLIC_IP>:~/smart-employee-assistant-ai-demo/it_server/.env
scp -i dda-poc-key.pem hr_agent/.env      ubuntu@<VM_PUBLIC_IP>:~/smart-employee-assistant-ai-demo/hr_agent/.env
scp -i dda-poc-key.pem it_agent/.env      ubuntu@<VM_PUBLIC_IP>:~/smart-employee-assistant-ai-demo/it_agent/.env
```

### 4a. `orchestrator/.env` — items to change

| Variable | Current (laptop) | New value |
|---|---|---|
| `ALLOWED_ORIGINS` | `http://localhost:8090,...` | `http://<VM_PUBLIC_IP>:8090,http://<VM_PUBLIC_DNS>:8090` |
| `ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI` | `http://localhost:8090/agent-callback` | `http://<VM_PUBLIC_IP>:8090/agent-callback` |
| `POST_LOGOUT_REDIRECT_URI` | `http://localhost:8090/` | `http://<VM_PUBLIC_IP>:8090/` |

```bash
# Edit on the VM
VM_PUBLIC_IP=<replace>

sed -i "s|ALLOWED_ORIGINS=.*|ALLOWED_ORIGINS=http://${VM_PUBLIC_IP}:8090|" orchestrator/.env
sed -i "s|ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI=.*|ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI=http://${VM_PUBLIC_IP}:8090/agent-callback|" orchestrator/.env
sed -i "s|POST_LOGOUT_REDIRECT_URI=.*|POST_LOGOUT_REDIRECT_URI=http://${VM_PUBLIC_IP}:8090/|" orchestrator/.env
```

> **OpenAI / WSO2 AI Gateway:** the `OPENAI_*` vars in `orchestrator/.env`
> (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, …) point at the WSO2
> Agent Manager gateway and are the same on laptop and VM — no rewrite needed.

> **Observability (optional):** `orchestrator/.env`, `hr_agent/.env`, and
> `it_agent/.env` may carry `AMP_OTEL_ENDPOINT` + `AMP_AGENT_API_KEY` for
> OpenTelemetry export to WSO2 Agent Manager. These are read by the
> `amp-instrument` launch wrapper, not by the service config. If you export
> traces to a collector reachable only from the VM, update `AMP_OTEL_ENDPOINT`
> in those files; otherwise leave them as-is.

### 4b. `hr_server/.env` and `it_server/.env` — ALLOWED_ORIGINS

```bash
sed -i "s|ALLOWED_ORIGINS=.*|ALLOWED_ORIGINS=http://${VM_PUBLIC_IP}:8090|" hr_server/.env
sed -i "s|ALLOWED_ORIGINS=.*|ALLOWED_ORIGINS=http://${VM_PUBLIC_IP}:8090|" it_server/.env
```

### 4c. `hr_agent/.env` and `it_agent/.env` — observability endpoint (optional)

Only needed if you export OpenTelemetry traces to a collector whose address
differs on the VM. Otherwise skip this step.

```bash
OTEL_ENDPOINT=<replace>   # e.g. https://opentelemetry.obs.dp.cloud.wso2.com/v1/traces
sed -i "s|AMP_OTEL_ENDPOINT=.*|AMP_OTEL_ENDPOINT=${OTEL_ENDPOINT}|" hr_agent/.env
sed -i "s|AMP_OTEL_ENDPOINT=.*|AMP_OTEL_ENDPOINT=${OTEL_ENDPOINT}|" it_agent/.env
```

---

## 5. docker-compose.yml — BCL Port Binding

The laptop setup bound port 8123 to loopback (`127.0.0.1`) and used a reverse SSH
tunnel to let IS reach it. On the VM, IS can reach it directly over the VPC.

Change line in `docker-compose.yml`:

```yaml
# Before (laptop — loopback only, needs SSH tunnel)
- "127.0.0.1:8123:8080"

# After (VM — IS reaches this via VPC private IP)
- "8123:8080"
```

```bash
sed -i 's|"127.0.0.1:8123:8080"|"8123:8080"|' docker-compose.yml
```

---

## 6. Update IS Configuration

These scripts talk to IS on `13.60.190.47:9443` from wherever you run them. Run
from your local machine (with the VM's private IP in hand) or directly from the VM.

### 6a. Back-Channel Logout URL

```bash
IS_ADMIN_USER=admin \
IS_ADMIN_PASS=NewsMax@1234 \
BCL_URL=http://<VM_PRIVATE_IP>:8123/backchannel-logout \
POST_LOGOUT_URI=http://<VM_PUBLIC_IP>:8090/ \
ORCHESTRATOR_MCP_CLIENT_ID=8SDRXOI_4zOrNBgV4KUUfDPs3Tsa \
./scripts/set-bcl-url.sh
```

### 6b. Subject Claim (S5.12) — run once per app if not already set

Sets `sub = emailaddress` so the username derivation in `_AuthContext` works.

```bash
for CLIENT_ID in \
  8SDRXOI_4zOrNBgV4KUUfDPs3Tsa \   # orchestrator-mcp-client
  vIM_Zl1N5qa41EL_NYU_kfAmazMa \   # hr-agent
  ZjTvH_RzPjHCqGWbhS02tMeGk6Ma;   # it-agent
do
  IS_ADMIN_USER=admin IS_ADMIN_PASS=NewsMax@1234 \
  APP_CLIENT_ID=$CLIENT_ID \
  ./scripts/set-subject-claim.sh
done
```

### 6c. Add the VM callback URL to IS

The IS app's `callbackURLs` must include the new redirect URI. The `set-bcl-url.sh`
above already handles this via `POST_LOGOUT_URI`, but verify with:

```bash
./scripts/check-is-config.py
```

---

## 7. Start the Stack

```bash
cd ~/smart-employee-assistant-ai-demo

# First run — build images
docker compose build

# Start all services
docker compose up -d

# Watch startup logs
docker compose logs -f
```

Healthy output looks like:

```
orchestrator  | INFO  Application startup complete.
hr_agent      | INFO  Application startup complete.
it_agent      | INFO  Application startup complete.
hr_server     | INFO  Application startup complete.
it_server     | INFO  Application startup complete.
```

---

## 8. Verify

```bash
# Smoke test all five containers
./scripts/demo-smoke.py

# Full IS config audit
./scripts/check-is-config.py

# Manual browser check
open http://<VM_PUBLIC_IP>:8090
```

---

## 9. Ongoing Operations

```bash
# Tail logs
docker compose logs -f orchestrator
docker compose logs -f hr_agent

# Restart a single service
docker compose restart orchestrator

# Pull latest code and redeploy
git pull
docker compose build --no-cache
docker compose up -d

# Stop everything
docker compose down
```

---

## 10. No SSH Tunnel Needed

Unlike the laptop setup, **no reverse SSH tunnel is required** for BCL — IS posts
directly to `http://<VM_PRIVATE_IP>:8123/backchannel-logout` over the VPC.

The `amp-tunnel.sh` script is still useful if you need to reach the WSO2 Agent
Manager gateway or other services on a separate VM from your laptop during
development:

```bash
VM_KEY=~/.ssh/dda-poc-key.pem VM_HOST=<VM_PUBLIC_IP> ./scripts/amp-tunnel.sh
```

---

## Quick Reference — What Changes vs Laptop

| Item | Laptop | VM |
|---|---|---|
| `ALLOWED_ORIGINS` | `localhost:8090` | `<VM_PUBLIC_IP>:8090` |
| `ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI` | `localhost:8090/agent-callback` | `<VM_PUBLIC_IP>:8090/agent-callback` |
| `POST_LOGOUT_REDIRECT_URI` | `localhost:8090/` | `<VM_PUBLIC_IP>:8090/` |
| `AMP_OTEL_ENDPOINT` (optional) | collector URL | collector URL (update if VM-only) |
| docker-compose port 8123 | `127.0.0.1:8123:8080` | `8123:8080` |
| BCL URL in IS | `localhost:8123/backchannel-logout` | `<VM_PRIVATE_IP>:8123/backchannel-logout` |
| SSH tunnel for BCL | Required | Not needed |
