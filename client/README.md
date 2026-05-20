# Smart Employee Agent — Sprint 1 SPA

Single-page chat app. No build step, no npm. Vanilla HTML + CSS + JS.

## Prerequisites

- Orchestrator running at `http://localhost:8090` (handles auth, SSE, CIBA).
- The SPA is served by the orchestrator's static-file route, **or** by the
  included `serve.py` dev server for local iteration.

## Running the SPA (two options)

### Option A — via the orchestrator (recommended)

The orchestrator at `localhost:8090` serves the `client/` directory as static
files. No separate process needed. Just start the orchestrator:

```bash
cd /path/to/smart-employee-agent
python -m orchestrator.main   # or however your orchestrator starts
```

Then open: `http://localhost:8090`

### Option B — standalone dev server (for SPA-only iteration)

```bash
cd client/
python serve.py          # starts on port 3001 by default
```

Open: `http://localhost:3001`

The `serve.py` server proxies `/auth/*`, `/api/*`, and `/events/*` to the
orchestrator. If your orchestrator is on a different port, set:

```bash
ORCHESTRATOR_URL=http://localhost:8090 python serve.py
```

### Option C — Python built-in (no proxy, file:// only for quick visual checks)

```bash
cd client/
python -m http.server 3001
```

This will not work for the auth or SSE flows (those require the orchestrator
backend). Use only for layout/style inspection.

## Auth flow

1. User clicks **Sign in** -> SPA redirects to `/auth/login?next=/`.
2. Orchestrator performs PKCE + actor_token exchange with WSO2 IS.
3. IS redirects to `/auth/callback` -> orchestrator POSTs `/auth/exchange` ->
   sets `orch_sid` HttpOnly cookie + returns `{session_id, user_display_name}`.
4. SPA stores `session_id` in `localStorage`, opens SSE stream at
   `/events/{session_id}`.

## Dev tips

- The first time you hit the IS (WSO2 IS at `https://13.60.190.47:9443`), your
  browser will show a self-signed certificate warning. Click "Advanced" then
  "Proceed" once; subsequent visits within the same browser session are clean.
- The orchestrator runs over plain HTTP (`http://localhost:8090`). No cert
  warning for orchestrator traffic.
- To inspect SSE events in the browser: DevTools -> Network -> EventStream tab
  on the `/events/{session_id}` request.
- `localStorage` holds `orch_session_id` and `orch_user_name` for page-reload
  resumption. Clear them (or call Sign out) to start fresh.
