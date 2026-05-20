# C12 BCL spike — first-time setup (macOS)

This guide walks a fresh laptop through running the **back-channel logout
capture rig** that feeds the Sprint 3 design decision (Option C: hybrid).
The rig answers the question: *does WSO2 IS actually fire BCL POSTs to an
agent application's registered URL when an OBO token issued via CIBA is
revoked?*

Read [`sprint-3-logout-design-brainstorm.md`](sprint-3-logout-design-brainstorm.md)
for the design context and verdict matrix.

## Why this setup looks the way it does

WSO2 IS runs on an AWS VM (`13.60.190.47:9443`). Your demo stack runs on
your laptop in docker-compose. IS cannot reach `localhost` on your laptop
directly. Two options were considered:

- **Public tunnel (ngrok / Cloudflare Tunnel / similar)** — works but
  needs a third-party account, exposes a public URL, and the URL changes
  on every restart unless you pay for a reserved domain.
- **Reverse SSH tunnel from laptop → AWS VM** — uses the SSH access you
  already have to manage the VM. Binds to the VM's loopback so nothing is
  exposed publicly. IS calls `http://localhost:8123/bcl` on its own host
  and the request is forwarded back to the laptop's docker container.

We chose the reverse SSH tunnel. No new credentials, no public surface,
stable URL across restarts, and `autossh` keeps it alive across laptop
sleep/wake.

## Architecture

```
   AWS VM (where IS runs)                       laptop
 ┌──────────────────────────┐               ┌────────────────────────┐
 │ WSO2 IS                   │               │  bcl-listener          │
 │   POST localhost:8123/bcl ────────────►   │  docker container,     │
 │        ▲                   │               │  bound to              │
 │        │ tunnel forwards   │               │  127.0.0.1:8123        │
 │        ▼ via reverse SSH   │               │                        │
 │ sshd (loopback bind)       │  ◄── autossh ────  laptop initiates SSH
 └──────────────────────────┘               └────────────────────────┘
```

The listener prints each captured POST to stdout and appends a JSON record
(decoded `logout_token` header + payload) to
`tools/_bcl_log/bcl_received.log`.

## Prerequisites

- macOS with Homebrew installed.
- Docker Desktop running.
- An AWS user account with SSH access to the IS VM (the same key you use
  to manage the VM).
- The IS VM's `sshd_config` permits TCP forwarding (`AllowTcpForwarding
  yes` — this is the default; if your hardening has disabled it, you need
  to re-enable for this spike).

## One-time setup

```bash
./scripts/spike-bcl-prep-mac.sh
```

The script will:

1. Verify Homebrew is installed.
2. Install `autossh` if absent.
3. Verify Docker Desktop is running.
4. Prompt for `AWS_VM_HOST`, `AWS_VM_USER`, and `AWS_VM_KEY` and write
   them to `.env` at the repo root (existing values are preserved).
   `AWS_VM_KEY` is the absolute path to your `.pem` private key (e.g.
   `/Users/you/keys/dda-poc-key.pem`). Leave blank if the key is already
   loaded into ssh-agent or configured in `~/.ssh/config`.
5. Test SSH connectivity in `BatchMode`. If `AWS_VM_KEY` is set, the
   script chmods it to `600` (SSH refuses to use over-permissive keys)
   and passes `-i $AWS_VM_KEY` through to all SSH calls.
6. Pre-pull `python:3.11-slim` so the first `up` is fast.
7. Create `tools/_bcl_log/` for the listener's capture log.

The script is **idempotent** — re-run it any time. It refuses to continue
if any check fails.

## Daily run

### 1. Start the rig

```bash
./scripts/spike-bcl-up.sh
```

This:

- Starts the `bcl-listener` container (`docker compose --profile spike-bcl
  up -d bcl-listener`), bound to `127.0.0.1:8123` on the laptop.
- Spawns `autossh -M 0 -f -N -R 8123:127.0.0.1:8123 …` in the background.
  The PID is saved to `.spike-bcl-autossh.pid`.
- Smoke-tests the tunnel by `ssh`-ing into the AWS VM and `curl`-ing the
  reverse-forwarded port. The listener's `/healthz` should return
  `BCL listener up`.

### 2. Register the BCL URL in WSO2 IS Console

Open `https://13.60.190.47:9443/console`. For **each agent application
under test** AND the **orchestrator application** (control comparison):

- Application → Protocol → Logout URLs
- Back channel logout URL: `http://localhost:8123/bcl`
- Click Update.

### 3. Trigger the auto-probe to confirm CIBA token claims

```bash
cd idp_capability_test
PROBE_USER_SUB=<employee_user_sub> python3 c12_logout_capability.py
```

You'll click **Approve** once on the IS consent page during the run. The
probe prints whether the issued tokens carry `sid` and `aud` claims that
allow IS to target a per-session logout.

### 4. Drive a logout — try BOTH paths

**RP-initiated** (what users will trigger via the SPA's Sign Out button):

```
https://13.60.190.47:9443/oidc/logout
   ?id_token_hint=<orch-id-token>
   &post_logout_redirect_uri=http://localhost:8090/
   &client_id=<orchestrator-client-id>
```

(Get the `id_token_hint` from the orchestrator session — easiest path is
to dump it from a fresh login's logs.)

**Admin terminate**:

- IS Console → User Management → probe user → Active Sessions → Terminate.

### 5. Read the capture

```bash
docker compose --profile spike-bcl logs -f bcl-listener
cat tools/_bcl_log/bcl_received.log | tail -200
```

Each capture is a pretty-printed JSON record like:

```json
{
  "ts": "2026-05-09T12:34:56Z",
  "method": "POST",
  "path": "/bcl",
  "client": "127.0.0.1:54321",
  "headers": { "Content-Type": "application/x-www-form-urlencoded", "..." : "..."},
  "body_raw_len": 1234,
  "logout_token_present": true,
  "logout_token_header": { "alg": "RS256", "kid": "...", "typ": "logout+jwt" },
  "logout_token_payload": {
    "iss": "https://13.60.190.47:9443/oauth2/token",
    "aud": "<which-app-this-targets>",
    "sub": "<user-sub>",
    "sid": "<session-id-or-absent>",
    "iat": 1716000000,
    "jti": "...",
    "events": { "http://schemas.openid.net/event/backchannel-logout": {} }
  }
}
```

### 6. Tear down

```bash
./scripts/spike-bcl-down.sh
```

This kills the autossh process, stops the bcl-listener container, and
preserves `tools/_bcl_log/bcl_received.log` for the design review.

## Verdict matrix (drives Sprint 3 Stage 1)

| Listener captured POST for… | Conclusion | Sprint 3 implication |
|---|---|---|
| **orchestrator URL only** | IS treats agent apps as machine-clients without sessions for BCL purposes. | Option C **degrades to Option A**. Skip agent-side BCL receivers; orchestrator-driven cache-bust is the only path. |
| **orchestrator + agent URLs** | IS fans BCL out to agent apps with CIBA-issued tokens. | Option C is fully viable. Wire agent BCL receivers in Sprint 3B as defense-in-depth. |
| **neither** | Misconfig somewhere — likely the BCL URL didn't save in IS Console, the agent app isn't enrolled in the user's session, or sshd is blocking the forward. | Recheck IS Console + IS audit log; recheck `ssh -O check` on the autossh control socket. |

## Troubleshooting

- **`spike-bcl-up.sh` says smoke test failed** — manually try the SSH-VM
  curl: `ssh ec2-user@13.60.190.47 curl -v http://127.0.0.1:8123/healthz`.
  If that fails, the autossh tunnel did not establish. Check
  `.spike-bcl-autossh.log`.
- **`autossh` exits with `Address already in use`** — a previous tunnel is
  still bound to AWS VM:8123. Kill it: `ssh ec2-user@13.60.190.47 'pkill
  -f "sshd.*:8123"'`.
- **Listener never receives POSTs** — it's almost always one of:
  1. BCL URL in IS Console is wrong (typo, http vs https).
  2. The user did not sign in via the orchestrator app *with* the agent
     app having minted a CIBA token first.
  3. WSO2 IS is configured to skip BCL when the client did not initiate
     login via the front channel — try the C12 manual recipe with
     authorization_code login instead of CIBA-only to confirm.
- **`http://localhost:8123/bcl` rejected by IS** — some hardened IS
  installs require `https`. Quick fix: install Caddy on the AWS VM with
  a self-signed cert that proxies HTTPS:9444 → http:8123.

## Files of record

| File | Role |
|---|---|
| `scripts/spike-bcl-prep-mac.sh` | macOS first-time setup (idempotent). |
| `scripts/spike-bcl-up.sh` | Brings up listener + autossh tunnel; smoke-tests. |
| `scripts/spike-bcl-down.sh` | Tears down autossh + container. |
| `tools/bcl_listener.py` | Python HTTP listener; decodes `logout_token` JWTs. |
| `docker-compose.yml` (`bcl-listener` service under profile `spike-bcl`) | Container definition; bound to `127.0.0.1:8123`. |
| `idp_capability_test/c12_logout_capability.py` | Auto-probe: inspects CIBA-issued token claims. |
| `tools/_bcl_log/bcl_received.log` | Captured BCL POSTs (gitignored). |
| `.spike-bcl-autossh.pid` / `.log` | Tunnel PID + autossh diagnostic log (gitignored). |
