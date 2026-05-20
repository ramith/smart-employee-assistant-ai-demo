# Browser SPA

Single-page chat UI. No build step, no npm — vanilla HTML + CSS + JS
(`index.html`, `app.js`, `styles.css`).

## How it's served

The SPA is **served by the orchestrator**, not by a standalone web server.
The orchestrator Docker image copies `index.html`, `app.js`, and `styles.css`
into `/app/client_static/` and mounts them at `/` (see
`orchestrator/Dockerfile` and `orchestrator/main.py`). Serving the SPA
same-origin with the BFF avoids cross-origin cookie problems for the
`orch_sid` session cookie.

So there is **no separate client container and no port 3001** — bring up the
stack and open the orchestrator:

```bash
./scripts/demo-up.sh
# then open:
open http://localhost:8090
```

All `/auth/*`, `/api/*`, and `/events/*` calls go to the same origin
(`localhost:8090`), which is the orchestrator.

## Editing the SPA

Because the files are baked into the orchestrator image at build time, rebuild
the orchestrator after changing them:

```bash
docker compose build orchestrator
docker compose up -d orchestrator
```

(For tight iteration you can instead run the orchestrator from source so it
serves the live `client/` files; see the orchestrator README / `main.py`.)

> **`serve.py` is legacy.** The `client/serve.py` standalone dev server is a
> pre-v4 leftover still wired to the old Asgardeo SPA flow and the removed
> `agent:5001` backend. It is **not** part of the current architecture and is
> not used by the demo. Use the orchestrator-served path above.

## Auth flow (Pattern C)

1. User clicks **Sign in** → SPA redirects to `/auth/login?next=/`.
2. Orchestrator runs PKCE + actor-token exchange with WSO2 IS.
3. IS redirects back to `/agent-callback` → orchestrator POSTs `/auth/exchange`,
   sets the `orch_sid` HttpOnly cookie, and returns
   `{session_id, user_display_name}`.
4. SPA opens the SSE stream at `/events/{session_id}`.

## Dev tips

- First hit to WSO2 IS (`https://13.60.190.47:9443`) shows a self-signed cert
  warning. Click **Advanced → Proceed** once per browser session. The
  orchestrator itself runs over plain HTTP (`http://localhost:8090`), so no
  cert warning there.
- Inspect SSE events: DevTools → Network → EventStream tab on the
  `/events/{session_id}` request.
- `localStorage` holds `orch_session_id` and `orch_user_name` for page-reload
  resumption. Clear them (or Sign out) to start fresh.
