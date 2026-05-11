/*
 * Smart Employee Agent — Sprint 1 SPA
 *
 * Auth:    Pattern C (orchestrator cookie session). No tokens in the browser.
 * Events:  SSE stream from GET /events/{session_id}.
 * Widget:  Consent Widget state machine per consent-widget-spec.md §3.
 * Copy:    All user-facing strings from docs/ux/copy-deck.md — no hardcoded strings.
 *
 * Stack: vanilla JS (ES2022), no build step, no npm.
 */

"use strict";

// ─── Copy deck (canonical source: docs/ux/copy-deck.md) ─────────────────────

const COPY = {
  // §1 Login surface
  signinTitle: "Smart Employee Assistant",
  signinSubtitle: "Sign in to ask about your leave, equipment, and team.",
  signinCta: "Sign in",
  signinHelper: "You will be redirected to your identity provider.",
  signinCertHint: 'First time? Your browser may show a certificate warning for the development identity server. Choose "Advanced" then "Proceed".',
  sessionExpired: "Your session has expired. Sign in again to continue.",
  signedOut: "Signed out. Agent sessions cleared.",  // 3A.4: confirms the cascade ran without listing receivers / jtis
  adminTerminated: "Your session was ended by your administrator. Sign in again to continue.",  // 3B.2 / copy-deck 8.11
  consentDeniedAtIs: "You did not approve the delegation. Sign in and approve to continue, or contact your administrator.",
  stateMismatch: "The sign-in flow could not be completed. Please try again.",
  configError: "Sign-in is temporarily unavailable. Please contact your administrator.",
  isUnreachable: "The identity server is not responding. Please try again in a moment.",
  tryAgain: "Try again",

  // §2 App chrome
  productName: "Smart Employee Assistant",
  connHealthy: "Connected",
  connHealthyAriaLabel: "Connected to the assistant.",
  connReconnecting: "Reconnecting…",
  connReconnectingAriaLabel: "Reconnecting to the assistant.",
  connLost: "Disconnected",
  connLostAriaLabel: "Disconnected from the assistant.",
  signOut: "Sign out",
  staySignedIn: "Stay signed in",

  // §3 Chat surface
  composerPlaceholder: "Ask about your leave, equipment, or team…",
  composerDisabledPlaceholder: "Waiting for the current request to finish…",
  composerHint: "Enter to send · Shift+Enter for a new line",
  emptyHeading: "What can I help you with?",
  emptyBody: "I can check your leave balance, look up available equipment, and answer routine HR and IT questions. Each request will ask you to approve the agent that handles it.",
  chip1: "What is my leave balance?",
  chip2: "What laptops are available?",
  chip3: "Show me my leave balance and what laptops are available.",
  charLimit: "{n}/2000",
  charLimitToast: "Messages are limited to 2000 characters.",

  // §4 Routing notifications
  routingSingle: "Routing to {agent_label}…",
  routingFirst: "Routing to {agent_label} first…",
  routingSecond: "Now routing to {agent_label}…",
  routingComposing: "Composing your answer…",
  routingThinking: "I’m thinking…",

  // §5 Consent Widget — AWAITING_APPROVAL
  cwTitle: "Action requires your approval",
  cwWantsTo: "Wants to:",
  cwBindingLabel: "binding code: {code}",
  cwApprove: "Approve",
  cwDeny: "Deny",
  cwCountdown: "⏱ expires {mm:ss}",
  cwCountdownAmber: "⏱ expires {mm:ss} — almost out of time",
  cwFooter: "Each agent asks for its own approval. Your identity provider records every consent.",

  // §5 Consent Widget — state transitions
  cwVerifying: "Verifying with your identity provider…",
  cwCancel: "Cancel",
  cwWorking: "{agent_label} is {action_gerund}…",
  cwDone: "✓ {agent_label} — completed",
  cwDenied: "⊘ Declined — {agent_label} will not run for this request",
  cwExpiredTitle: "Approval window expired",
  cwExpiredBody: "You did not approve {agent_label} in time. You can ask again.",
  cwRetry: "Ask again",
  cwDismiss: "Dismiss",
  cwErrorTitle: "Something went wrong",
  cwErrorGeneric: "{agent_label} could not complete this request. Reference: {short_id}.",
  cwErrorBackend: "The system {agent_label} relies on is not responding. Please try again in a moment.",
  cwErrorMisconfig: "{agent_label} is not configured correctly. Please contact your administrator.",
  cwErrorAud: "{agent_label} could not authorize this action. Please contact your administrator.",
  cwErrorRetry: "Try again",
  cwErrorCancel: "Cancel",

  // §6 Session Refresh (UC-06)
  cwRefreshTitle: "Session refresh",
  cwRefreshBanner: "↻ {agent_label}’s previous access has expired",
  cwRefreshPrior: "You approved this {duration} ago.",
  cwRefreshWantsTo: "Wants to:",
  cwReApprove: "Re-approve",
  cwSkip: "Skip",
  cwRefreshFooter: "Approving this gives {agent_label} access for another hour.",
  cwResuming: "Resuming previous request — {mm:ss} left to approve.",

  // §8 Sign-out
  signoutDialogTitle: "Sign out?",
  signoutDialogBody: "You will be signed out of the assistant and any agents that acted on your behalf will lose their access.",
  signoutDialogBodyCiba: "An approval is in progress. Sign out anyway?",
  signoutPrimary: "Sign out",
  signoutPrimaryCiba: "Sign out and cancel approval",
  signoutProgress: "Revoking access for all agents…",                       // copy-deck 8.5 (Stage 4 FIX-14)
  signoutRedirecting: "Redirecting to complete sign-out at your identity provider…",  // copy-deck 8.9 (BLOCK-E)
  signoutError: "Sign-out could not be completed right now. Close your browser to end your session, or try again.",  // copy-deck 8.10 (FIX-16)
  signedOutPartial: "You have been signed out of this application. Note: your sign-in at the identity provider may still be active. To fully sign out everywhere, visit your organization's sign-out page or close your browser.",  // copy-deck 1.13 (BLOCK-E)

  // §9 Toasts and banners
  toastConnLost: "Connection lost. Trying to reconnect…",
  toastReconnected: "Reconnected.",
  toastReconnectFail: "Could not reconnect. Refresh the page to try again.",
  bannerServiceDegraded: "The identity service is responding slowly. Some actions may take longer than usual.",
  bannerServiceDown: "The identity service is unavailable. New requests cannot be processed right now.",
  toastOfflineSend: "You appear to be offline. Your message will be sent when the connection is restored.",
};

// ─── Scope → action map (copy-deck §5.A) ────────────────────────────────────

const SCOPE_ACTION_MAP = {
  "hr.read":              "View your leave balance",
  "hr.approve":           "Approve a leave request on your behalf",
  "hr.write":             "Submit a leave request on your behalf",
  "hr_assets_write_rest": "Assign cubicle",
  "it.read":              "Look up available laptops",
  "it.assign":            "Assign a laptop to you",
  "directory.read":       "Look up team contact information",
};

// ─── Scope → gerund map (copy-deck §5.C) ────────────────────────────────────

const SCOPE_GERUND_MAP = {
  "hr.read":        "checking your leave balance",
  "hr.approve":     "approving the leave request",
  "hr.write":       "submitting your leave request",
  "it.read":        "looking up available laptops",
  "it.assign":      "assigning your laptop",
  "directory.read": "looking up team contacts",
};

// ─── Agent display config (copy-deck §11) ───────────────────────────────────

const AGENT_CONFIG = {
  "hr-agent": { label: "HR Agent",    color: "#14b8a6", icon: "clipboard" },
  "it-agent": { label: "IT Agent",    color: "#a855f7", icon: "laptop"    },
  "orchestrator-agent": { label: "Orchestrator Agent", color: "#64748b", icon: "bubble" },
};

// ─── DOM helpers ─────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

function esc(str) {
  if (str == null) return "";
  const d = document.createElement("div");
  d.textContent = String(str);
  return d.innerHTML;
}

function announce(text, urgency = "polite") {
  const el = urgency === "assertive" ? $("a11y-assertive") : $("a11y-polite");
  if (!el) return;
  el.textContent = "";
  // Force re-announcement even with the same text by clearing first
  requestAnimationFrame(() => { el.textContent = text; });
}

// ─── Diagnostic logger ───────────────────────────────────────────────────────
// All client-side console output flows through `log(tag, ...)` so each line
// carries an ISO-8601 timestamp comparable against `docker compose logs`.
function log(tag, ...args)  { console.log(new Date().toISOString(), tag, ...args); }
function logWarn(tag, ...args) { console.warn(new Date().toISOString(), tag, ...args); }
function logErr(tag, ...args)  { console.error(new Date().toISOString(), tag, ...args); }

// ─── Trace store (D2.4 in-app debug panel) ───────────────────────────────────
// Captures the SSE timeline for each chat request, keyed by X-Request-ID.
// Bounded to TRACES_MAX entries (oldest dropped) so the panel stays bounded.
const TRACES_MAX = 50;
const traces = [];   // newest-first; each = {rid, message, startedAt, status, agents:Set, events:[]}

function recordTraceStart(rid, message) {
  const trace = {
    rid,
    message,
    startedAt: new Date(),
    status: "in-flight",
    agents: new Set(),
    events: [{ at: new Date(), type: "chat_request", summary: `len=${message.length}` }],
  };
  traces.unshift(trace);
  while (traces.length > TRACES_MAX) traces.pop();
  renderTracePanel();
}

function recordTraceEvent(rid, type, summary, opts = {}) {
  if (!rid) return;
  const trace = traces.find((t) => t.rid === rid);
  if (!trace) return;
  trace.events.push({ at: new Date(), type, summary });
  if (opts.agentId) trace.agents.add(opts.agentId);
  if (opts.status) trace.status = opts.status;
  renderTracePanel();
}

// ─── State ───────────────────────────────────────────────────────────────────

let sessionId = null;          // from /auth/exchange response
let userDisplayName = "";
let userScopes = [];           // Sprint 4: from /auth/exchange response
let sseSource = null;          // EventSource
let sseRetryCount = 0;
const SSE_MAX_RETRIES = 3;
let requestInFlight = false;   // true while a chat request is live
let pendingUserMessage = null; // saved for Retry / Re-approve
let pendingRequestId = null;   // X-Request-ID for the in-flight chat call
let cibaState = null;          // current widget state object

// cibaState shape:
// {
//   requestId, agentId, agentLabel, authUrl, bindingCode,
//   expiresIn, scope, isRefresh, priorConsentAt,
//   authReqId,           // populated when we have it (from SSE auth_req_id field if present)
//   widgetState,         // "AWAITING_APPROVAL"|"VERIFYING"|"WORKING"|"DONE"|"DENIED"|"EXPIRED"|"ERROR"
//   countdownInterval,   // setInterval handle
//   expiresAt,           // Date when auth_req_id expires
// }

// ─── Routing state ───────────────────────────────────────────────────────────
// Track how many routing events for the current request, for "first of two" etc.
let routingCount = 0;

// ─── Initialization ──────────────────────────────────────────────────────────

async function init() {
  wireStaticUI();

  const params = new URLSearchParams(window.location.search);

  // Detect sign-in callback: code + state in URL
  if (params.has("code") && params.has("state")) {
    await completeLogin(params.get("code"), params.get("state"));
    return;
  }

  // Detect error callbacks (IS returned error=access_denied etc.)
  if (params.has("error")) {
    const error = params.get("error");
    window.history.replaceState({}, "", "/");
    showSigninPage();
    if (error === "access_denied") {
      showSigninError(COPY.consentDeniedAtIs, true);
    } else {
      showSigninError(COPY.configError, false);
    }
    return;
  }

  // Detect sign-in notices from query params
  if (params.has("reason")) {
    const reason = params.get("reason");
    window.history.replaceState({}, "", "/");
    if (reason === "session_expired") {
      showSigninNotice(COPY.sessionExpired);
    } else if (reason === "signed_out") {
      showSigninNotice(COPY.signedOut, true);
    } else if (reason === "signed_out_partial") {
      // 3A.1 BLOCK-E: user cancelled at IS consent screen. Orchestrator state
      // is already cleaned but IS SSO may still be active.
      showSigninNotice(COPY.signedOutPartial, false);
    } else if (reason === "admin_terminated") {
      // 3B.2: admin clicked Terminate in IS Console; UC-10 cascade fired.
      // Sticky banner (no auto-dismiss) so the audience reads why.
      showSigninNotice(COPY.adminTerminated, false);
    }
  } else if (sessionStorage.getItem("orch_just_admin_terminated") === "1") {
    // 3B.2: SSE session_terminated handler (below) sets this flag before
    // the SPA navigates to "/". Surfaces the banner without depending on
    // a ?reason= round-trip through IS.
    sessionStorage.removeItem("orch_just_admin_terminated");
    showSigninNotice(COPY.adminTerminated, false);
  } else if (sessionStorage.getItem("orch_just_signed_out") === "1") {
    // 3A.2.2 (live-walk fix 2026-05-09): WSO2 IS rejects post_logout_redirect_uri
    // with query strings (exact-match against registered callback URLs). We
    // remember the just-signed-out state in sessionStorage instead of relying
    // on a ?reason= param. Cleared after first read so reload doesn't re-show.
    sessionStorage.removeItem("orch_just_signed_out");
    showSigninNotice(COPY.signedOut, true);
  }

  // Try to resume session via cookie (check if orchestrator has our session)
  // The session_id must be in localStorage because the cookie is HttpOnly.
  const savedSessionId = localStorage.getItem("orch_session_id");
  const savedUserName = localStorage.getItem("orch_user_name");
  if (savedSessionId) {
    // Verify session is still valid by opening SSE stream
    sessionId = savedSessionId;
    userDisplayName = savedUserName || "";
    try {
      const savedScopes = localStorage.getItem("orch_scopes");
      userScopes = savedScopes ? JSON.parse(savedScopes) : [];
    } catch {
      userScopes = [];
    }
    showAppShell();
    connectSse(sessionId);
  } else {
    showSigninPage();
  }
}

function wireStaticUI() {
  // Sign-in button
  $("signin-btn").addEventListener("click", signIn);

  // Sprint 4: clicking the product name returns to the main (chat) view —
  // the universal "go home" affordance. No-op when already on the home view.
  const logoBtn = $("product-name-home");
  if (logoBtn) {
    logoBtn.addEventListener("click", () => {
      const reportsView = $("reports-view");
      if (reportsView && !reportsView.hidden) showHomeView();
    });
  }

  // Sign-out button — show confirmation dialog
  $("signout-btn").addEventListener("click", () => {
    const hasCiba = cibaState && ["AWAITING_APPROVAL", "VERIFYING", "WORKING"].includes(cibaState.widgetState);
    $("signout-dialog-body").textContent = hasCiba
      ? COPY.signoutDialogBodyCiba
      : COPY.signoutDialogBody;
    $("signout-confirm-btn").textContent = hasCiba
      ? COPY.signoutPrimaryCiba
      : COPY.signoutPrimary;
    $("signout-dialog").hidden = false;
  });

  $("signout-confirm-btn").addEventListener("click", performSignOut);
  $("signout-cancel-btn").addEventListener("click", () => {
    $("signout-dialog").hidden = true;
  });

  // Chat form
  $("chat-form").addEventListener("submit", onChatSubmit);

  // Textarea: auto-resize, Enter-to-send
  const input = $("msg-input");
  input.addEventListener("input", onInputChange);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      onChatSubmit(e);
    }
  });

  // Example chips
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      input.value = chip.dataset.msg;
      onInputChange();
      input.focus();
    });
  });

  // Trace panel toggle
  const traceBtn = $("trace-toggle-btn");
  if (traceBtn) traceBtn.addEventListener("click", toggleTracePanel);
  const traceClose = $("trace-panel-close");
  if (traceClose) traceClose.addEventListener("click", () => setTracePanelOpen(false));

  // Consent widget buttons
  $("cw-approve-btn").addEventListener("click", onApproveClick);
  $("cw-deny-btn").addEventListener("click", onDenyClick);
  $("cw-cancel-btn").addEventListener("click", onCancelClick);
  $("cw-retry-btn").addEventListener("click", onWidgetRetry);
  $("cw-dismiss-btn").addEventListener("click", onWidgetDismiss);
  $("cw-error-retry-btn").addEventListener("click", onWidgetRetry);
  $("cw-error-cancel-btn").addEventListener("click", onWidgetDismiss);

  // Service banner dismiss
  $("service-banner-dismiss").addEventListener("click", () => {
    $("service-banner").hidden = true;
  });
}

// ─── Auth: Sign-in ───────────────────────────────────────────────────────────

function signIn() {
  // Redirect to orchestrator's login endpoint; orchestrator does PKCE + actor_token
  window.location.href = "/auth/login?next=/";
}

// ─── Auth: Complete login (code+state callback) ──────────────────────────────

async function completeLogin(code, state) {
  // The orchestrator's /auth/callback redirects to /auth/exchange-landing which
  // POSTs /auth/exchange. But per the contract we also support direct SPA callback.
  // We POST /auth/exchange with {code, state}. The orchestrator holds the verifier.
  try {
    const exchangeRid = (crypto.randomUUID && crypto.randomUUID()) ||
                        (Date.now().toString(16) + "-" + Math.random().toString(16).slice(2));
    const resp = await fetch("/auth/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Request-ID": exchangeRid },
      credentials: "include",
      body: JSON.stringify({ code, state }),
    });

    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      window.history.replaceState({}, "", "/");
      showSigninPage();
      if (resp.status === 400) {
        showSigninError(COPY.stateMismatch, true);
      } else {
        showSigninError(COPY.configError, false);
      }
      return;
    }

    const data = await resp.json();
    // data: { session_id, user_display_name, scopes, expires_at }
    sessionId = data.session_id;
    userDisplayName = data.user_display_name || "";
    userScopes = Array.isArray(data.scopes) ? data.scopes : [];

    // Persist for page reload resumption
    localStorage.setItem("orch_session_id", sessionId);
    localStorage.setItem("orch_user_name", userDisplayName);
    try { localStorage.setItem("orch_scopes", JSON.stringify(userScopes)); } catch {}

    window.history.replaceState({}, "", "/");
    showAppShell();
    connectSse(sessionId);

  } catch (e) {
    logErr("[auth]", "completeLogin error:", e);
    window.history.replaceState({}, "", "/");
    showSigninPage();
    showSigninError(COPY.stateMismatch, true);
  }
}

// ─── Auth: Sign-out ──────────────────────────────────────────────────────────

async function performSignOut() {
  $("signout-dialog").hidden = true;
  const progressEl = $("signout-progress");
  progressEl.querySelector("span").textContent = COPY.signoutProgress;  // phase 1 (FIX-14)
  progressEl.hidden = false;

  // Teardown SSE early so the cascade doesn't have to drain it.
  if (sseSource) {
    sseSource.close();
    sseSource = null;
  }

  // Clear local state.
  clearWidgetState();
  localStorage.removeItem("orch_session_id");
  localStorage.removeItem("orch_user_name");
  sessionId = null;

  // 3A.1 FIX-9: server requires X-Request-ID. SPA mints a fresh rid.
  const logoutRid = "logout-" + Math.random().toString(36).slice(2, 10) + "-"
    + Date.now().toString(36);

  // 3A.1 FIX-16: 10-second client-side timeout; on timeout/5xx, show error
  // banner instead of spinning forever.
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 10000);

  let redirectUrl = null;
  try {
    const resp = await fetch("/auth/logout", {
      method: "POST",
      credentials: "include",
      headers: { "X-Request-ID": logoutRid },
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    if (resp.ok) {
      const body = await resp.json().catch(() => ({}));
      redirectUrl = body.redirect_url || null;
    } else {
      console.warn("logout server error", resp.status);
    }
  } catch (err) {
    clearTimeout(timeoutId);
    console.warn("logout request failed", err);
  }

  if (!redirectUrl) {
    // 3A.1 FIX-16 / EX-6: orchestrator unreachable or 5xx. Show error banner;
    // the local cookie is already cleared client-side via teardown above.
    progressEl.hidden = true;
    showSigninPage();
    showSigninError(COPY.signoutError, true);
    return;
  }

  // 3A.1 BLOCK-E: phase 2 spinner before IS redirect.
  progressEl.querySelector("span").textContent = COPY.signoutRedirecting;
  // Brief delay so the user sees the phase-2 copy.
  await new Promise((r) => setTimeout(r, 200));

  // 3A.2.2 (live-walk fix): IS will redirect to the registered post-logout URL
  // (no query string), so we can't pass ?reason=signed_out through. Remember
  // the just-signed-out state via sessionStorage so init() can show the banner.
  try { sessionStorage.setItem("orch_just_signed_out", "1"); } catch (_) {}

  window.location.href = redirectUrl;
}

// ─── Page visibility ─────────────────────────────────────────────────────────

function showSigninPage() {
  $("signin-page").hidden = false;
  $("app-shell").hidden = true;
}

function showAppShell() {
  $("signin-page").hidden = true;
  $("app-shell").hidden = false;
  $("user-display-name").textContent = userDisplayName.slice(0, 24) + (userDisplayName.length > 24 ? "…" : "");
  setComposerEnabled(true);
  // Sprint 4 S4.3: My Leaves panel — wire sort handlers once, then fetch.
  wireMyLeavesPanel();
  fetchMyLeaves();
  // Sprint 4 S4.4: Reports nav, gated on hr_approve_rest scope (canonical
  // HR-Admin probe — Employee role never holds it per docs/scope-policy.md).
  renderReportsNav();
  wireReportsView();
  // Default to home view on app shell entry.
  showHomeView();
}

function isHrAdmin() {
  // Sprint 4 amendment: hr_approve_rest is HR-Admin-exclusive (Employee
  // never holds it). Single canonical probe; the server-side per-endpoint
  // scope check remains authoritative.
  return Array.isArray(userScopes) && userScopes.includes("hr_approve_rest");
}

function renderReportsNav() {
  const reportsBtn = $("reports-nav-btn");
  const homeBtn = $("home-nav-btn");
  if (!reportsBtn || !homeBtn) return;
  if (isHrAdmin()) {
    reportsBtn.hidden = false;
    // home-nav-btn visibility is toggled by the active view.
  } else {
    reportsBtn.hidden = true;
    homeBtn.hidden = true;
  }
}

function showHomeView() {
  const main = document.querySelector("main:not(.reports-view)");
  if (main) main.hidden = false;
  $("reports-view").hidden = true;
  const homeBtn = $("home-nav-btn");
  const reportsBtn = $("reports-nav-btn");
  if (homeBtn) homeBtn.hidden = true;
  if (reportsBtn && isHrAdmin()) reportsBtn.hidden = false;
}

function showReportsView() {
  const main = document.querySelector("main:not(.reports-view)");
  if (main) main.hidden = true;
  $("reports-view").hidden = false;
  const homeBtn = $("home-nav-btn");
  const reportsBtn = $("reports-nav-btn");
  if (homeBtn) homeBtn.hidden = false;
  if (reportsBtn) reportsBtn.hidden = true;
  // Default to Pending Leaves tab; load now.
  selectReportsTab("pending-leaves");
  loadPendingLeaves();
}

function selectReportsTab(name) {
  const tabs = ["pending-leaves", "cubicles", "devices"];
  for (const t of tabs) {
    const btn = $("tab-" + t);
    const panel = $("tabpanel-" + t);
    if (!btn || !panel) continue;
    const active = (t === name);
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", String(active));
    panel.hidden = !active;
  }
}

function _ridGen() {
  return (crypto.randomUUID && crypto.randomUUID()) ||
         (Date.now().toString(16) + "-" + Math.random().toString(16).slice(2));
}

let _pendingLeavesRows = [];

async function loadPendingLeaves() {
  const status = $("pending-leaves-status");
  const empty = $("pending-leaves-empty");
  const table = $("pending-leaves-table");
  const tbody = $("pending-leaves-tbody");
  if (!table || !tbody) return;
  if (status) { status.textContent = "Loading…"; }
  try {
    const resp = await fetch("/api/reports/leave-requests?status=Pending", {
      credentials: "include",
    });
    if (resp.status === 401) {
      if (status) status.textContent = "Sign in to view reports.";
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    if (resp.status === 403) {
      if (status) status.textContent = "You do not have permission to view this report.";
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    if (!resp.ok) {
      if (status) status.textContent = "Could not load report.";
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    const body = await resp.json();
    _pendingLeavesRows = Array.isArray(body.data) ? body.data : [];
    if (status) status.textContent = "";
    if (_pendingLeavesRows.length === 0) {
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    empty.hidden = true;
    table.hidden = false;
    tbody.innerHTML = "";
    for (const row of _pendingLeavesRows) {
      const tr = document.createElement("tr");
      const cells = [
        row.request_id, row.employee_username, row.employee_email,
        row.leave_type, String(row.days_requested), row.start_date,
      ];
      for (const c of cells) {
        const td = document.createElement("td");
        td.textContent = c == null ? "" : String(c);
        tr.appendChild(td);
      }
      const td = document.createElement("td");
      const approveBtn = document.createElement("button");
      approveBtn.className = "btn-action btn-approve";
      approveBtn.textContent = "Approve";
      approveBtn.addEventListener("click", () => onApproveClick(row));
      const rejectBtn = document.createElement("button");
      rejectBtn.className = "btn-action btn-reject";
      rejectBtn.textContent = "Reject";
      rejectBtn.addEventListener("click", () => onRejectClick(row));
      td.appendChild(approveBtn);
      td.appendChild(rejectBtn);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  } catch (e) {
    logErr("[reports]", "loadPendingLeaves error:", e);
    if (status) status.textContent = "Could not load report.";
  }
}

async function onApproveClick(row) {
  const rid = _ridGen();
  log("[reports]", "approve_clicked", { request_id: row.request_id, rid });
  try {
    const resp = await fetch(
      `/api/reports/leave-requests/${encodeURIComponent(row.request_id)}/approve`,
      {
        method: "POST",
        credentials: "include",
        headers: { "X-Request-ID": rid },
      }
    );
    if (!resp.ok) {
      logErr("[reports]", "approve failed", resp.status);
      const status = $("pending-leaves-status");
      if (status) status.textContent = `Approve failed (${resp.status}).`;
      return;
    }
    const status = $("pending-leaves-status");
    if (status) status.textContent = `Approve dispatched — approve the consent in the popup, then this list will refresh.`;
  } catch (e) {
    logErr("[reports]", "approve error:", e);
  }
}

let _pendingRejectRow = null;

function onRejectClick(row) {
  _pendingRejectRow = row;
  const dlg = $("reject-reason-dialog");
  const inp = $("reject-reason-input");
  if (inp) inp.value = "";
  if (dlg) dlg.hidden = false;
  if (inp) inp.focus();
}

async function _submitReject() {
  if (!_pendingRejectRow) return;
  const inp = $("reject-reason-input");
  const reason = (inp && inp.value || "").trim();
  if (!reason) {
    if (inp) inp.focus();
    return;
  }
  const row = _pendingRejectRow;
  _pendingRejectRow = null;
  const dlg = $("reject-reason-dialog");
  if (dlg) dlg.hidden = true;
  const rid = _ridGen();
  try {
    const resp = await fetch(
      `/api/reports/leave-requests/${encodeURIComponent(row.request_id)}/reject`,
      {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-Request-ID": rid,
        },
        body: JSON.stringify({ reason }),
      }
    );
    const status = $("pending-leaves-status");
    if (!resp.ok) {
      if (status) status.textContent = `Reject failed (${resp.status}).`;
      return;
    }
    if (status) status.textContent = `Reject dispatched — approve the consent in the popup, then this list will refresh.`;
  } catch (e) {
    logErr("[reports]", "reject error:", e);
  }
}

function _cancelReject() {
  _pendingRejectRow = null;
  const dlg = $("reject-reason-dialog");
  if (dlg) dlg.hidden = true;
}

function wireReportsView() {
  const reportsBtn = $("reports-nav-btn");
  const homeBtn = $("home-nav-btn");
  if (reportsBtn) reportsBtn.addEventListener("click", showReportsView);
  if (homeBtn) homeBtn.addEventListener("click", showHomeView);

  const tabPL = $("tab-pending-leaves");
  const tabCu = $("tab-cubicles");
  const tabDe = $("tab-devices");
  if (tabPL) tabPL.addEventListener("click", () => { selectReportsTab("pending-leaves"); loadPendingLeaves(); });
  if (tabCu) tabCu.addEventListener("click", () => { selectReportsTab("cubicles"); loadCubicleAssignments(); });
  if (tabDe) tabDe.addEventListener("click", () => { selectReportsTab("devices"); loadDeviceAssignments(); });

  const refresh = $("pending-leaves-refresh");
  if (refresh) refresh.addEventListener("click", loadPendingLeaves);

  const cuRefresh = $("cubicles-refresh");
  if (cuRefresh) cuRefresh.addEventListener("click", loadCubicleAssignments);

  const deRefresh = $("devices-refresh");
  if (deRefresh) deRefresh.addEventListener("click", loadDeviceAssignments);

  const deFilter = $("devices-type-filter");
  if (deFilter) deFilter.addEventListener("change", _renderDevicesTable);

  const confirmBtn = $("reject-reason-confirm");
  const cancelBtn = $("reject-reason-cancel");
  if (confirmBtn) confirmBtn.addEventListener("click", _submitReject);
  if (cancelBtn) cancelBtn.addEventListener("click", _cancelReject);
}

// ─── UC-16 / S4.5 — Cubicles tab ─────────────────────────────────────────────

let _cubicleRows = [];

async function loadCubicleAssignments() {
  const status = $("cubicles-status");
  const empty = $("cubicles-empty");
  const table = $("cubicles-table");
  const tbody = $("cubicles-tbody");
  if (!table || !tbody) return;
  if (status) { status.textContent = "Loading…"; }
  try {
    const resp = await fetch("/api/reports/cubicle-assignments", {
      credentials: "include",
    });
    if (resp.status === 401) {
      if (status) status.textContent = "Sign in to view reports.";
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    if (resp.status === 403) {
      if (status) status.textContent = "You do not have permission to view this report.";
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    if (!resp.ok) {
      if (status) status.textContent = "Could not load report.";
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    const body = await resp.json();
    _cubicleRows = Array.isArray(body.data) ? body.data : [];
    if (status) status.textContent = "";
    if (_cubicleRows.length === 0) {
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    empty.hidden = true;
    table.hidden = false;
    tbody.innerHTML = "";
    for (const row of _cubicleRows) {
      const tr = document.createElement("tr");
      const cells = [
        row.username, row.email, row.cubicle_id,
        row.floor == null ? "" : String(row.floor),
        row.assigned_at,
      ];
      for (const c of cells) {
        const td = document.createElement("td");
        td.textContent = c == null ? "" : String(c);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  } catch (e) {
    logErr("[reports]", "loadCubicleAssignments error:", e);
    if (status) status.textContent = "Could not load report.";
  }
}

// ─── UC-16 / S4.5 — Devices tab (filter + drilldown) ─────────────────────────

let _deviceRows = [];
let _expandedDeviceUsernames = new Set();

async function loadDeviceAssignments() {
  const status = $("devices-status");
  const empty = $("devices-empty");
  const table = $("devices-table");
  const tbody = $("devices-tbody");
  if (!table || !tbody) return;
  if (status) { status.textContent = "Loading…"; }
  try {
    const resp = await fetch("/api/reports/device-assignments", {
      credentials: "include",
    });
    if (resp.status === 401) {
      if (status) status.textContent = "Sign in to view reports.";
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    if (resp.status === 403) {
      if (status) status.textContent = "You do not have permission to view this report.";
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    if (!resp.ok) {
      if (status) status.textContent = "Could not load report.";
      empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
      return;
    }
    const body = await resp.json();
    _deviceRows = Array.isArray(body.data) ? body.data : [];
    _expandedDeviceUsernames = new Set();
    if (status) status.textContent = "";
    _populateDeviceTypeFilter();
    _renderDevicesTable();
  } catch (e) {
    logErr("[reports]", "loadDeviceAssignments error:", e);
    if (status) status.textContent = "Could not load report.";
  }
}

function _populateDeviceTypeFilter() {
  const sel = $("devices-type-filter");
  if (!sel) return;
  const prev = sel.value || "";
  const types = Array.from(new Set(_deviceRows.map(r => r.type).filter(Boolean))).sort();
  // Rebuild option list: keep "All" plus distinct types.
  sel.innerHTML = "";
  const allOpt = document.createElement("option");
  allOpt.value = "";
  allOpt.textContent = "All";
  sel.appendChild(allOpt);
  for (const t of types) {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    sel.appendChild(opt);
  }
  // Restore previous selection if still valid.
  if (prev && types.includes(prev)) {
    sel.value = prev;
  } else {
    sel.value = "";
  }
}

function _renderDevicesTable() {
  const empty = $("devices-empty");
  const table = $("devices-table");
  const tbody = $("devices-tbody");
  const sel = $("devices-type-filter");
  if (!table || !tbody) return;
  const filterType = (sel && sel.value) || "";
  const filtered = filterType
    ? _deviceRows.filter(r => r.type === filterType)
    : _deviceRows.slice();
  if (filtered.length === 0) {
    empty.hidden = false; table.hidden = true; tbody.innerHTML = "";
    return;
  }
  empty.hidden = true;
  table.hidden = false;
  tbody.innerHTML = "";
  for (const row of filtered) {
    const tr = document.createElement("tr");
    tr.dataset.username = row.username || "";
    const cells = [
      { key: "username", value: row.username, clickable: true },
      { key: "email", value: row.email },
      { key: "asset_id", value: row.asset_id },
      { key: "type", value: row.type },
      { key: "model", value: row.model },
      { key: "status", value: row.status },
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c.value == null ? "" : String(c.value);
      if (c.clickable && row.username) {
        td.className = "devices-username-cell";
        td.addEventListener("click", () => _toggleDrilldown(row.username));
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
    if (row.username && _expandedDeviceUsernames.has(row.username)) {
      const drill = _buildDrilldownRow(row.username);
      // Only insert one drilldown per username — guard against duplicates
      // when the same username has multiple rows in the filtered set.
      if (!tbody.querySelector(`tr.drilldown-row[data-for="${cssEscape(row.username)}"]`)) {
        tbody.appendChild(drill);
      }
    }
  }
}

function cssEscape(s) {
  // Minimal CSS attribute selector escape — usernames are tame
  // (alphanumerics + dot + underscore) per the demo seed.
  return String(s).replace(/["\\]/g, "\\$&");
}

function _toggleDrilldown(username) {
  if (!username) return;
  if (_expandedDeviceUsernames.has(username)) {
    _expandedDeviceUsernames.delete(username);
  } else {
    _expandedDeviceUsernames.add(username);
  }
  _renderDevicesTable();
}

function _buildDrilldownRow(username) {
  const tr = document.createElement("tr");
  tr.className = "drilldown-row";
  tr.setAttribute("data-for", username);
  const td = document.createElement("td");
  td.colSpan = 6;
  const wrap = document.createElement("div");
  wrap.className = "drilldown-wrap";
  const heading = document.createElement("div");
  heading.className = "drilldown-heading";
  heading.textContent = `All assets for ${username}:`;
  wrap.appendChild(heading);
  const nested = document.createElement("table");
  nested.className = "drilldown-table";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>Asset ID</th><th>Type</th><th>Model</th><th>Status</th></tr>";
  nested.appendChild(thead);
  const nb = document.createElement("tbody");
  const rows = _deviceRows.filter(r => r.username === username);
  for (const r of rows) {
    const ntr = document.createElement("tr");
    for (const v of [r.asset_id, r.type, r.model, r.status]) {
      const ntd = document.createElement("td");
      ntd.textContent = v == null ? "" : String(v);
      ntr.appendChild(ntd);
    }
    nb.appendChild(ntr);
  }
  nested.appendChild(nb);
  wrap.appendChild(nested);
  td.appendChild(wrap);
  tr.appendChild(td);
  return tr;
}

function showSigninNotice(text, autoDismiss = false) {
  const el = $("signin-notice");
  el.textContent = text;
  el.hidden = false;
  // 3A.4 NIT-8: announce on the polite live region so screen readers pick up
  // sign-out / session-expired banners that otherwise appear silently.
  announce(text, "polite");
  if (autoDismiss) {
    setTimeout(() => { el.hidden = true; }, 5000);
  }
}

function showSigninError(text, showRetry = false) {
  const el = $("signin-error");
  el.hidden = false;
  if (showRetry) {
    el.innerHTML = `${esc(text)} <button class="btn-link retry-btn" onclick="app.signIn()">${esc(COPY.tryAgain)}</button>`;
  } else {
    el.textContent = text;
  }
}

// ─── SSE connection ──────────────────────────────────────────────────────────

function connectSse(sid) {
  if (sseSource) {
    sseSource.close();
  }

  const url = "/events/" + encodeURIComponent(sid);
  sseSource = new EventSource(url, { withCredentials: true });

  sseSource.onopen = () => {
    sseRetryCount = 0;
    setConnStatus("healthy");
  };

  sseSource.onmessage = (e) => {
    let event;
    try {
      event = JSON.parse(e.data);
    } catch (err) {
      logWarn("[sse]", "could not parse event:", e.data);
      return;
    }
    handleSseEvent(event);
  };

  sseSource.onerror = () => {
    logWarn("[sse]", "error event", { retry: sseRetryCount });
    setConnStatus("reconnecting");
    toast(COPY.toastConnLost, "warn", 0); // persist until reconnected

    // EventSource will auto-retry. Track retry count to show failure after threshold.
    sseRetryCount++;
    if (sseRetryCount >= SSE_MAX_RETRIES) {
      setConnStatus("lost");
      toast(COPY.toastReconnectFail, "error", 0);
      // Treat as session lost — redirect to sign-in
      localStorage.removeItem("orch_session_id");
      localStorage.removeItem("orch_user_name");
    }
  };

  // Named event types emitted by the orchestrator
  // The orchestrator may emit named events or generic `message` events.
  // Handle named events for explicitness:
  ["routing", "ciba_url", "ciba_state_change", "chat_message", "error", "session_ready", "session_terminated"].forEach((type) => {
    sseSource.addEventListener(type, (e) => {
      let event;
      try { event = JSON.parse(e.data); }
      catch { return; }
      handleSseEvent(event);
    });
  });
}

// ─── SSE event handler ───────────────────────────────────────────────────────

function handleSseEvent(event) {
  const { type } = event;

  switch (type) {
    case "session_ready":
      // SSE stream confirmed healthy; update connection status
      setConnStatus("healthy");
      dismissToastsByClass("conn-lost");
      toast(COPY.toastReconnected, "success", 3000);
      break;

    case "routing":
      onRoutingEvent(event);
      break;

    case "ciba_url":
      onCibaUrlEvent(event);
      break;

    case "ciba_state_change":
      onCibaStateChangeEvent(event);
      break;

    case "chat_message":
      onChatMessageEvent(event);
      break;

    case "error":
      onSseErrorEvent(event);
      break;

    case "session_terminated":
      onSessionTerminatedEvent(event);
      break;

    default:
      log("[sse]", "unknown event type:", type, event);
  }
}

// ─── SSE: session_terminated (3B.2 / UC-10) ──────────────────────────────────
//
// Pushed by the orchestrator's logout cascade BEFORE the session is dropped
// (BLOCK-H ordering). Two variants by reason:
//   - "admin_terminated": IS Console terminate fired BCL → orchestrator ran
//     the cascade. SPA must clear local state and surface the right banner.
//   - "user_signed_out": multi-browser case. Tab A signed out; tab B (same
//     user) gets this push so it doesn't sit on stale UI.
function onSessionTerminatedEvent(event) {
  log("[sse]", "session_terminated", { reason: event.reason, rid: event.request_id });
  // Stash the reason; the post-reload code path (initialize) reads this
  // sessionStorage flag and shows the banner. Do this BEFORE wiping
  // localStorage so a navigation race doesn't leak a half-cleared state.
  if (event.reason === "admin_terminated") {
    try { sessionStorage.setItem("orch_just_admin_terminated", "1"); } catch (_) {}
  } else if (event.reason === "user_signed_out") {
    try { sessionStorage.setItem("orch_just_signed_out", "1"); } catch (_) {}
  }
  // Drop client-side session state and navigate back to "/", which
  // triggers the sign-in page render and reads the sessionStorage flag.
  localStorage.removeItem("orch_session_id");
  localStorage.removeItem("orch_user_name");
  if (sseSource) { try { sseSource.close(); } catch (_) {} }
  window.location.assign("/");
}

// ─── SSE: routing event ──────────────────────────────────────────────────────

function onRoutingEvent(event) {
  routingCount++;
  const label = event.agent_label || agentLabel(event.agent_id);
  recordTraceEvent(event.request_id, "routing", `→ ${label}`, {
    agentId: event.agent_id,
  });

  // A-3: orchestrator tells us the total fan-out size and our 0-based
  // index, so the SPA can pick natural copy without having to guess on
  // the first event.
  const totalTools = Number.isInteger(event.total_tools) ? event.total_tools : 1;
  const toolIndex = Number.isInteger(event.tool_index) ? event.tool_index : 0;
  let text;
  if (totalTools <= 1) {
    text = COPY.routingSingle.replace("{agent_label}", label);
  } else if (toolIndex === 0) {
    text = COPY.routingFirst.replace("{agent_label}", label);
  } else {
    text = COPY.routingSecond.replace("{agent_label}", label);
  }

  showRoutingLine(text);
  announce("Routing your request to " + label + ".", "polite");
}

function showRoutingLine(text) {
  const el = $("routing-line");
  el.textContent = text;
  el.hidden = false;
}

function hideRoutingLine() {
  $("routing-line").hidden = true;
}

// ─── SSE: ciba_url event — render consent widget ─────────────────────────────

function onCibaUrlEvent(event) {
  hideRoutingLine();
  hideEmptyState();

  const {
    request_id: requestId,
    agent_id: agentId,
    agent_label: agentLabelRaw,
    auth_url: authUrl,
    binding_code: bindingCode,
    expires_in: expiresIn,
    scope,
    is_refresh: isRefresh,
    prior_consent_at: priorConsentAt,
  } = event;

  log("[ciba]", "ciba_url received", {
    rid: requestId,
    agentId,
    expiresIn,
    bindingCode: bindingCode ? bindingCode.slice(0, 8) : null,
  });
  recordTraceEvent(requestId || pendingRequestId, "ciba_url", `${agentId} expires=${expiresIn}s`, {
    agentId,
  });

  const label = agentLabelRaw || agentLabel(agentId);
  // Sprint 4 S4.1: prefer the server-rendered action_text (parameterised
  // for write-tier scopes like "Assign cubicle C-027 to jane.doe") over
  // the static SCOPE_ACTION_MAP lookup.
  const actionText = event.action_text || scopeToAction(scope);
  const expiresAt = new Date(Date.now() + expiresIn * 1000);

  cibaState = {
    requestId,
    agentId,
    agentLabel: label,
    authUrl,
    bindingCode,
    expiresIn,
    scope,
    isRefresh: !!isRefresh,
    priorConsentAt: priorConsentAt || null,
    authReqId: event.auth_req_id || null,
    widgetState: "AWAITING_APPROVAL",
    countdownInterval: null,
    expiresAt,
    actionText,
    bindingMessage: event.binding_message || null,
  };

  renderWidget();
  setComposerEnabled(false);

  // Announce for screen readers
  announce(
    "An approval is required. " + label + " wants to " + actionText + ". Approve or deny.",
    "assertive"
  );
}

// ─── SSE: ciba_state_change event ────────────────────────────────────────────

function onCibaStateChangeEvent(event) {
  if (!cibaState) return;

  const newState = event.state;
  cibaState.widgetState = newState;
  recordTraceEvent(event.request_id || pendingRequestId, "ciba_state", `${cibaState.agentId}: ${newState}`, {
    agentId: cibaState.agentId,
  });

  switch (newState) {
    case "VERIFYING":
      transitionWidgetToVerifying();
      break;
    case "WORKING":
      transitionWidgetToWorking();
      break;
    case "DONE":
      transitionWidgetToDone();
      break;
    case "DENIED":
      transitionWidgetToDenied();
      break;
    case "EXPIRED":
      transitionWidgetToExpired();
      break;
    case "ERROR":
      transitionWidgetToError(event);
      break;
    default:
      logWarn("[widget]", "unknown ciba_state_change state:", newState);
  }
}

// ─── SSE: chat_message event ─────────────────────────────────────────────────

function onChatMessageEvent(event) {
  hideRoutingLine();
  appendAssistantMessage(event.content);
  recordTraceEvent(event.request_id || pendingRequestId, "chat_message", "assistant reply", {
    status: "done",
  });
  requestInFlight = false;
  routingCount = 0;
  setComposerEnabled(true);
  // Sprint 4 S4.3: refresh My Leaves so chat-applied leaves appear without
  // a manual reload. Conservative: re-fetch on EVERY settled chat_message
  // (demo scale; performance not a concern).
  fetchMyLeaves();
  // Sprint 4 S4.4: if the admin is on the Reports view, refresh Pending
  // Leaves so a successful Approve/Reject (which dispatches CIBA and
  // settles via this SSE event) updates the table without manual reload.
  const reportsView = $("reports-view");
  if (reportsView && !reportsView.hidden) {
    loadPendingLeaves();
  }
}

// ─── SSE: error event ────────────────────────────────────────────────────────

function onSseErrorEvent(event) {
  const code = event.code || "";
  const message = event.message || "Something went wrong handling your request.";
  recordTraceEvent(event.request_id || pendingRequestId, "error", `${code || "ERR"}: ${message}`, {
    status: "error",
  });

  if (code.startsWith("ERR-AUTH-")) {
    // Session-level auth error — redirect to sign-in
    localStorage.removeItem("orch_session_id");
    localStorage.removeItem("orch_user_name");
    window.location.href = "/?reason=session_expired";
    return;
  }

  if (code.startsWith("ERR-INFRA-")) {
    showServiceBanner(message, true);
    return;
  }

  // Inline chat error
  appendErrorMessage(message);
  requestInFlight = false;
  setComposerEnabled(true);
}

// ─── Consent Widget state machine ────────────────────────────────────────────

function renderWidget() {
  if (!cibaState) return;

  const { isRefresh, priorConsentAt, agentId, agentLabel: label, actionText, bindingCode, expiresAt, scope } = cibaState;
  const cfg = AGENT_CONFIG[agentId] || { color: "#64748b", icon: "bubble" };

  // Card title
  $("cw-card-title").textContent = isRefresh ? COPY.cwRefreshTitle : COPY.cwTitle;

  // Refresh banner
  if (isRefresh) {
    $("cw-refresh-banner").hidden = false;
    $("cw-refresh-banner-text").textContent = COPY.cwRefreshBanner.replace("{agent_label}", label);

    if (priorConsentAt) {
      const diffSec = Math.floor((Date.now() - new Date(priorConsentAt).getTime()) / 1000);
      const duration = humanizeDuration(diffSec);
      $("cw-prior-consent").hidden = false;
      $("cw-prior-consent").textContent = COPY.cwRefreshPrior.replace("{duration}", duration);
    }

    // Re-approve / Skip buttons
    $("cw-approve-btn").textContent = COPY.cwReApprove;
    $("cw-deny-btn").textContent = COPY.cwSkip;

    // Footer
    $("cw-footer-text").textContent = COPY.cwRefreshFooter.replace("{agent_label}", label);
  } else {
    $("cw-refresh-banner").hidden = true;
    $("cw-prior-consent").hidden = true;
    $("cw-approve-btn").textContent = COPY.cwApprove;
    $("cw-deny-btn").textContent = COPY.cwDeny;
    $("cw-footer-text").textContent = COPY.cwFooter;
  }

  // Agent icon and label
  const iconEl = $("cw-agent-icon");
  iconEl.textContent = agentIconGlyph(cfg.icon);
  iconEl.style.color = cfg.color;
  iconEl.setAttribute("data-agent", agentId);
  $("cw-agent-label").textContent = label;
  $("cw-agent-label").style.color = cfg.color;

  // Action text
  $("cw-action-text").textContent = actionText;

  // Binding code (first 8 chars)
  const shortCode = (bindingCode || "").slice(0, 8);
  $("cw-binding-code").textContent = COPY.cwBindingLabel.replace("{code}", shortCode);

  // 3B.2 FIX-17: reason-aware binding_message inline (WSO2 IS may not
  // surface this on its consent screen). Only render when present and
  // when the reason actually adds information beyond the routine FRESH
  // copy — i.e. the message contains a "previous session" phrase.
  const bindingMessage = cibaState.bindingMessage;
  const messageRow = $("cw-binding-message-row");
  const messageText = $("cw-binding-message-text");
  if (bindingMessage && /previous session/i.test(bindingMessage)) {
    messageText.textContent = bindingMessage;
    messageRow.hidden = false;
  } else {
    messageRow.hidden = true;
    messageText.textContent = "";
  }

  // Show card, hide slim
  $("cw-card").hidden = false;
  $("cw-slim").hidden = true;

  // Show AWAITING_APPROVAL buttons; hide others
  $("cw-approve-btn").hidden = false;
  $("cw-deny-btn").hidden = false;
  $("cw-retry-btn").hidden = true;
  $("cw-dismiss-btn").hidden = true;
  $("cw-error-retry-btn").hidden = true;
  $("cw-error-cancel-btn").hidden = true;
  $("cw-expired-body").hidden = true;
  $("cw-error-body").hidden = true;

  // Remove state classes; tint amber for write-tier scopes (UC-11 admin
  // delegation visual convention).
  const widget = $("consent-widget");
  let widgetClass = "consent-widget consent-widget--awaiting";
  if (scope && scope.indexOf("hr_assets_write_rest") !== -1) {
    widgetClass += " consent-widget--write";
  }
  widget.className = widgetClass;
  widget.hidden = false;

  // Slide in animation
  requestAnimationFrame(() => widget.classList.add("consent-widget--visible"));

  // Start countdown
  startCountdown(expiresAt);
}

function startCountdown(expiresAt) {
  if (cibaState && cibaState.countdownInterval) {
    clearInterval(cibaState.countdownInterval);
  }

  function tick() {
    const remaining = Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
    const mm = String(Math.floor(remaining / 60)).padStart(2, "0");
    const ss = String(remaining % 60).padStart(2, "0");

    const countdownEl = $("cw-countdown");
    if (!countdownEl) return;

    if (remaining <= 60) {
      countdownEl.textContent = COPY.cwCountdownAmber.replace("{mm:ss}", `${mm}:${ss}`);
      countdownEl.classList.add("countdown-amber");
    } else {
      countdownEl.textContent = COPY.cwCountdown.replace("{mm:ss}", `${mm}:${ss}`);
      countdownEl.classList.remove("countdown-amber");
    }

    if (remaining === 0) {
      clearInterval(cibaState.countdownInterval);
      // Auto-expire only if still in AWAITING_APPROVAL
      if (cibaState && cibaState.widgetState === "AWAITING_APPROVAL") {
        transitionWidgetToExpired();
      }
    }
  }

  tick();
  if (cibaState) {
    cibaState.countdownInterval = setInterval(tick, 1000);
  }
}

// Widget transitions ─────────────────────────────────────────────────────────

function transitionWidgetToVerifying() {
  stopCountdown();
  $("cw-card").hidden = true;
  $("cw-slim").hidden = false;

  const cfg = AGENT_CONFIG[cibaState.agentId] || { color: "#64748b" };
  $("cw-slim-icon").style.color = cfg.color;
  $("cw-slim-icon").textContent = agentIconGlyph((AGENT_CONFIG[cibaState.agentId] || {}).icon);
  $("cw-slim-dots").hidden = false;
  $("cw-slim-text").textContent = COPY.cwVerifying;
  $("cw-cancel-btn").hidden = false;

  $("consent-widget").className = "consent-widget consent-widget--slim consent-widget--verifying consent-widget--visible";
  announce(COPY.cwVerifying.replace("…", "."), "polite");
}

function transitionWidgetToWorking() {
  stopCountdown();
  $("cw-card").hidden = true;
  $("cw-slim").hidden = false;

  const { agentLabel: label, scope } = cibaState;
  const gerund = scopeToGerund(scope);
  const cfg = AGENT_CONFIG[cibaState.agentId] || { color: "#64748b" };

  $("cw-slim-icon").style.color = cfg.color;
  $("cw-slim-icon").textContent = agentIconGlyph((AGENT_CONFIG[cibaState.agentId] || {}).icon);
  $("cw-slim-dots").hidden = true;
  $("cw-slim-text").textContent = COPY.cwWorking
    .replace("{agent_label}", label)
    .replace("{action_gerund}", gerund);
  $("cw-cancel-btn").hidden = false;

  $("consent-widget").className = "consent-widget consent-widget--slim consent-widget--working consent-widget--visible";
  announce(label + " is " + gerund + ".", "polite");
}

function transitionWidgetToDone() {
  stopCountdown();
  const { agentLabel: label } = cibaState;

  // Collapse into a transcript line and remove the widget
  const line = COPY.cwDone.replace("{agent_label}", label);
  appendStatusLine(line, "status-done");

  dismissWidget();
  setComposerEnabled(true);
  announce(label + " completed.", "polite");
}

function transitionWidgetToDenied() {
  stopCountdown();
  const { agentLabel: label } = cibaState;

  const line = COPY.cwDenied.replace("{agent_label}", label);
  appendStatusLine(line, "status-denied");

  dismissWidget();
  setComposerEnabled(true);
  announce(label + " declined. The request will continue without it.", "polite");
}

function transitionWidgetToExpired() {
  stopCountdown();
  if (!cibaState) return;

  const { agentLabel: label } = cibaState;
  cibaState.widgetState = "EXPIRED";

  // Keep card, amber outline
  $("cw-card").hidden = false;
  $("cw-slim").hidden = true;
  $("cw-card-title").textContent = COPY.cwExpiredTitle;
  $("cw-expired-body").hidden = false;
  $("cw-expired-body-text").textContent = COPY.cwExpiredBody.replace("{agent_label}", label);
  $("cw-approve-btn").hidden = true;
  $("cw-deny-btn").hidden = true;
  $("cw-retry-btn").hidden = false;
  $("cw-dismiss-btn").hidden = false;
  $("cw-countdown").textContent = "";
  $("cw-footer-text").hidden = true;

  $("consent-widget").className = "consent-widget consent-widget--expired consent-widget--visible";
  announce(COPY.cwExpiredTitle + ".", "assertive");
}

function transitionWidgetToError(event) {
  stopCountdown();
  if (!cibaState) return;

  const { agentLabel: label, authReqId } = cibaState;
  const shortId = (authReqId || cibaState.requestId || "").slice(0, 8);
  cibaState.widgetState = "ERROR";

  // Determine error body by reason code
  const reason = event.message || "";
  let errorBody;
  if (reason.includes("backend_unavailable") || reason.includes("ERR-MCP-004") || reason.includes("ERR-MCP-005")) {
    errorBody = COPY.cwErrorBackend.replace("{agent_label}", label);
  } else if (reason.includes("unauthorized_client") || reason.includes("invalid_request") || reason.includes("ERR-CIBA-001") || reason.includes("ERR-CIBA-002") || reason.includes("ERR-CIBA-003") || reason.includes("ERR-CIBA-004")) {
    errorBody = COPY.cwErrorMisconfig.replace("{agent_label}", label);
  } else if (reason.includes("aud") || reason.includes("act.sub") || reason.includes("ERR-CIBA-007") || reason.includes("ERR-CIBA-008")) {
    errorBody = COPY.cwErrorAud.replace("{agent_label}", label);
  } else {
    errorBody = COPY.cwErrorGeneric
      .replace("{agent_label}", label)
      .replace("{short_id}", shortId);
  }

  $("cw-card").hidden = false;
  $("cw-slim").hidden = true;
  $("cw-card-title").textContent = COPY.cwErrorTitle;
  $("cw-error-body").hidden = false;
  $("cw-error-body-text").textContent = errorBody;
  $("cw-approve-btn").hidden = true;
  $("cw-deny-btn").hidden = true;
  $("cw-error-retry-btn").hidden = false;
  $("cw-error-cancel-btn").hidden = false;
  $("cw-countdown").textContent = "";
  $("cw-footer-text").hidden = true;

  $("consent-widget").className = "consent-widget consent-widget--error consent-widget--visible";
  announce("An error occurred. Reference " + shortId + ".", "assertive");
}

// Widget action handlers ──────────────────────────────────────────────────────

function onApproveClick() {
  if (!cibaState) return;
  log("[ciba]", "approve clicked, opening auth_url", {
    rid: cibaState.requestId,
    agentId: cibaState.agentId,
  });
  // Open IS consent URL in a new tab
  window.open(cibaState.authUrl, "_blank", "noopener,noreferrer");
  // Transition to VERIFYING visually — actual confirmation comes via SSE
  cibaState.widgetState = "VERIFYING";
  transitionWidgetToVerifying();
}

async function onDenyClick() {
  if (!cibaState) return;
  await cancelCiba();
  transitionWidgetToDenied();
}

async function onCancelClick() {
  if (!cibaState) return;
  await cancelCiba();
  transitionWidgetToDenied();
}

function onWidgetRetry() {
  // Re-submit the original user message
  const msg = pendingUserMessage;
  dismissWidget();
  if (msg) {
    sendMessage(msg);
  }
}

function onWidgetDismiss() {
  dismissWidget();
  setComposerEnabled(true);
}

async function cancelCiba() {
  if (!cibaState) return;
  const authReqId = cibaState.authReqId || cibaState.requestId;
  if (!authReqId) return;

  try {
    await fetch("/api/ciba/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ auth_req_id: authReqId }),
    });
  } catch (e) {
    logErr("[ciba]", "cancel failed:", e);
  }
}

// Tracks the dismiss-after-transition setTimeout id so a new ciba_url that
// arrives during the 300ms hide animation can cancel it. Without this, the
// stale dismiss timer hides the next agent's widget ~300ms after it
// appears (multi-tool fan-out: HR DONE → dismiss scheduled → IT
// ciba_url renders new widget → stale timer fires → user never sees IT
// widget).
let _dismissWidgetTimer = null;

function dismissWidget() {
  stopCountdown();
  const widget = $("consent-widget");
  widget.classList.remove("consent-widget--visible");
  // After transition, hide. Track the timer so renderWidget() can cancel
  // if a new ciba_url arrives in the same tick.
  if (_dismissWidgetTimer !== null) {
    clearTimeout(_dismissWidgetTimer);
  }
  _dismissWidgetTimer = setTimeout(() => {
    _dismissWidgetTimer = null;
    // Race guard: only hide if cibaState is still null. If a new widget
    // was rendered between dismiss and this callback, leave it alone.
    if (cibaState === null) {
      widget.hidden = true;
      widget.className = "consent-widget";
      log("[widget]", "dismiss_fired_no_new_state — widget hidden");
    } else {
      log("[widget]", "dismiss_skipped_new_widget_present", {
        agentId: cibaState.agentId,
      });
    }
  }, 300);
  cibaState = null;
}

function clearWidgetState() {
  stopCountdown();
  const widget = $("consent-widget");
  if (widget) {
    widget.hidden = true;
    widget.className = "consent-widget";
  }
  cibaState = null;
}

function stopCountdown() {
  if (cibaState && cibaState.countdownInterval) {
    clearInterval(cibaState.countdownInterval);
    cibaState.countdownInterval = null;
  }
}

// ─── Chat message submission ─────────────────────────────────────────────────

function onChatSubmit(e) {
  if (e && e.preventDefault) e.preventDefault();
  const input = $("msg-input");
  const text = input.value.trim();
  if (!text) return false;

  if (text.length > 2000) {
    toast(COPY.charLimitToast, "error", 4000);
    return false;
  }

  if (!sseSource || sseSource.readyState === EventSource.CLOSED) {
    toast(COPY.toastOfflineSend, "warn", 5000);
    return false;
  }

  input.value = "";
  onInputChange();
  sendMessage(text);
  return false;
}

async function sendMessage(text) {
  if (requestInFlight) return;

  hideEmptyState();
  appendUserMessage(text);
  pendingUserMessage = text;
  requestInFlight = true;
  routingCount = 0;
  setComposerEnabled(false);

  // Generate the X-Request-ID at the user-action boundary so the audit trail
  // originates one hop earlier than the orchestrator. The middleware accepts
  // and echoes it; if absent it auto-generates with WARN.
  const rid = (crypto.randomUUID && crypto.randomUUID()) ||
              (Date.now().toString(16) + "-" + Math.random().toString(16).slice(2));
  pendingRequestId = rid;
  log("[chat]", "send", { rid, len: text.length });
  recordTraceStart(rid, text);

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Request-ID": rid },
      credentials: "include",
      body: JSON.stringify({ message: text }),
    });

    if (resp.status === 429) {
      appendErrorMessage("Too many requests. Please wait a moment before trying again.");
      requestInFlight = false;
      setComposerEnabled(true);
      return;
    }

    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      appendErrorMessage(body.message || "Something went wrong handling your request.");
      requestInFlight = false;
      setComposerEnabled(true);
      return;
    }

    // Ack received — full response comes via SSE

  } catch (e) {
    logErr("[chat]", "sendMessage error:", e);
    appendErrorMessage("Something went wrong handling your request.");
    requestInFlight = false;
    setComposerEnabled(true);
  }
}

// ─── Transcript helpers ──────────────────────────────────────────────────────

function appendUserMessage(text) {
  const el = document.createElement("div");
  el.className = "msg msg-user";
  el.textContent = text;
  $("chat-transcript").appendChild(el);
  scrollChat();
}

function appendAssistantMessage(content) {
  const el = document.createElement("div");
  el.className = "msg msg-assistant";
  // Render plain text; no markdown library dependency for Sprint 1 simplicity
  el.textContent = content;
  $("chat-transcript").appendChild(el);
  scrollChat();
}

function appendErrorMessage(text) {
  const el = document.createElement("div");
  el.className = "msg msg-error";
  el.textContent = text;
  $("chat-transcript").appendChild(el);
  scrollChat();
}

function appendStatusLine(text, cssClass) {
  const el = document.createElement("div");
  el.className = "msg msg-status " + (cssClass || "");
  el.textContent = text;
  $("chat-transcript").appendChild(el);
  scrollChat();
}

function scrollChat() {
  const el = $("chat-transcript");
  if (el) el.scrollTop = el.scrollHeight;
}

function hideEmptyState() {
  const el = $("empty-state");
  if (el) el.hidden = true;
}

// ─── Composer state ──────────────────────────────────────────────────────────

function setComposerEnabled(enabled) {
  const input = $("msg-input");
  const btn = $("send-btn");
  if (!input || !btn) return;

  input.disabled = !enabled;
  btn.disabled = !enabled;
  input.placeholder = enabled
    ? COPY.composerPlaceholder
    : COPY.composerDisabledPlaceholder;
}

function onInputChange() {
  const input = $("msg-input");
  const counter = $("char-counter");
  const len = input.value.length;

  // Auto-resize textarea
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";

  // Character counter
  if (len > 1800) {
    counter.hidden = false;
    counter.textContent = COPY.charLimit.replace("{n}", len);
    counter.classList.toggle("char-counter-red", len >= 2000);
  } else {
    counter.hidden = true;
  }
}

// ─── Connection status ───────────────────────────────────────────────────────

function setConnStatus(status) {
  const dot = $("conn-dot");
  const label = $("conn-label");
  const indicator = $("conn-indicator");
  if (!dot || !label || !indicator) return;

  dot.className = "conn-dot conn-" + status;

  const statusMap = {
    healthy: { label: COPY.connHealthy, ariaLabel: COPY.connHealthyAriaLabel },
    reconnecting: { label: COPY.connReconnecting, ariaLabel: COPY.connReconnectingAriaLabel },
    lost: { label: COPY.connLost, ariaLabel: COPY.connLostAriaLabel },
  };
  const s = statusMap[status] || statusMap.healthy;
  label.textContent = s.label;
  indicator.setAttribute("aria-label", s.ariaLabel);
  indicator.setAttribute("title", s.label);
}

// ─── Service banner ──────────────────────────────────────────────────────────

function showServiceBanner(text, persistent = false) {
  const banner = $("service-banner");
  $("service-banner-text").textContent = text;
  banner.hidden = false;
  if (!persistent) {
    setTimeout(() => { banner.hidden = true; }, 8000);
  }
}

// ─── Toasts ──────────────────────────────────────────────────────────────────

const activeToasts = new Set();

function toast(text, kind = "info", duration = 4000) {
  const container = $("toast-container");
  if (!container) return;

  const el = document.createElement("div");
  el.className = "toast toast-" + kind;
  el.setAttribute("role", "status");
  el.textContent = text;
  container.appendChild(el);
  activeToasts.add(el);

  if (duration > 0) {
    setTimeout(() => {
      el.classList.add("toast-fade");
      el.addEventListener("transitionend", () => {
        el.remove();
        activeToasts.delete(el);
      }, { once: true });
    }, duration);
  }
}

function dismissToastsByClass(cls) {
  activeToasts.forEach((el) => {
    if (el.classList.contains(cls)) {
      el.remove();
      activeToasts.delete(el);
    }
  });
}

// ─── Utilities ───────────────────────────────────────────────────────────────

function agentLabel(agentId) {
  return (AGENT_CONFIG[agentId] || {}).label || agentId;
}

function agentIconGlyph(iconName) {
  const glyphs = {
    clipboard: "📋",
    laptop: "💻",
    bubble: "💬",
  };
  return glyphs[iconName] || "●";
}

function scopeToAction(scopeStr) {
  if (!scopeStr) return "Perform an action on your behalf";
  const scopes = scopeStr.split(" ");
  for (const s of scopes) {
    if (SCOPE_ACTION_MAP[s]) return SCOPE_ACTION_MAP[s];
  }
  logWarn("[widget]", "unmapped scope:", scopeStr);
  return "Perform an action on your behalf";
}

function scopeToGerund(scopeStr) {
  if (!scopeStr) return "working on it";
  const scopes = scopeStr.split(" ");
  for (const s of scopes) {
    if (SCOPE_GERUND_MAP[s]) return SCOPE_GERUND_MAP[s];
  }
  return "working on it";
}

// Humanize duration (copy-deck §13)
function humanizeDuration(seconds) {
  if (seconds < 60) return "a moment";
  if (seconds < 120) return "1 minute";
  if (seconds < 3600) return Math.floor(seconds / 60) + " minutes";
  if (seconds < 7200) return "1 hour";
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (m >= 1) return h + " hours " + m + " minutes";
    return h + " hours";
  }
  return "over a day";
}

// ─── Trace panel rendering ───────────────────────────────────────────────────

function setTracePanelOpen(open) {
  const panel = $("trace-panel");
  if (!panel) return;
  panel.hidden = !open;
  const btn = $("trace-toggle-btn");
  if (btn) btn.setAttribute("aria-expanded", String(open));
  if (open) renderTracePanel();
}

function toggleTracePanel() {
  const panel = $("trace-panel");
  if (!panel) return;
  setTracePanelOpen(panel.hidden);
}

function renderTracePanel() {
  const list = $("trace-list");
  const badge = $("trace-toggle-count");
  if (badge) badge.textContent = String(traces.length);
  if (!list || $("trace-panel").hidden) return;

  if (!traces.length) {
    list.innerHTML = '<p class="trace-empty">No requests yet. Send a message to see its trace.</p>';
    return;
  }

  const fmtClock = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const fmtDelta = (a, b) => {
    const ms = b - a;
    if (ms < 1000) return `+${ms}ms`;
    return `+${(ms / 1000).toFixed(2)}s`;
  };

  const html = traces.map((t) => {
    const ridShort = t.rid.slice(0, 8);
    const agents = Array.from(t.agents).join(", ") || "—";
    const evRows = t.events.map((e, i) => {
      const delta = i === 0 ? "" : fmtDelta(t.events[0].at, e.at);
      return `<tr><td class="te-time">${fmtClock(e.at)}</td><td class="te-delta">${delta}</td><td class="te-type">${e.type}</td><td class="te-summary">${escapeHtml(e.summary)}</td></tr>`;
    }).join("");
    const statusClass = t.status === "done" ? "ok" : t.status === "error" ? "err" : "live";
    return `
      <details class="trace-row" ${t.status === "in-flight" ? "open" : ""}>
        <summary>
          <span class="trace-status trace-status-${statusClass}" aria-label="status: ${t.status}"></span>
          <code class="trace-rid" title="${t.rid}">${ridShort}</code>
          <span class="trace-msg">${escapeHtml(t.message)}</span>
          <span class="trace-meta">${agents} · ${fmtClock(t.startedAt)}</span>
          <button class="trace-copy" data-rid="${t.rid}" title="Copy full request id">copy rid</button>
        </summary>
        <table class="trace-events"><tbody>${evRows}</tbody></table>
      </details>
    `;
  }).join("");

  list.innerHTML = html;

  list.querySelectorAll(".trace-copy").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const rid = btn.dataset.rid;
      navigator.clipboard?.writeText(rid).then(
        () => { btn.textContent = "copied"; setTimeout(() => { btn.textContent = "copy rid"; }, 1200); },
        () => { btn.textContent = "copy failed"; }
      );
    });
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ─── My Leaves panel (Sprint 4 S4.3 — UC-13/14) ──────────────────────────────
//
// Calls GET /api/me/leaves through the orchestrator-proxy primitive (cookie
// session → token-A → hr_server). Renders a sortable table; on every settled
// chat_message SSE event we re-fetch so chat-applied leaves appear without
// page reload. Defensive: 401/403 hide the panel; 503 shows an error pill.
//
// Demo scale: ≤ a handful of rows, no virtualisation, no pagination.

const LEAVE_STATUS_CLASS = {
  pending:  "leave-status--pending",
  approved: "leave-status--approved",
  rejected: "leave-status--rejected",
};

let _myLeavesSort = { key: "start_date", asc: false };  // default: most recent first
let _myLeavesRows = [];
let _myLeavesFetching = false;

function setMyLeavesStatus(text, kind) {
  const el = $("my-leaves-status");
  if (!el) return;
  if (!text) {
    el.hidden = true;
    el.textContent = "";
    el.className = "my-leaves-status";
    return;
  }
  el.hidden = false;
  el.textContent = text;
  el.className = "my-leaves-status" + (kind ? " my-leaves-status--" + kind : "");
}

async function fetchMyLeaves() {
  const panel = $("my-leaves-panel");
  if (!panel) return;
  if (_myLeavesFetching) return;  // simple in-flight guard
  _myLeavesFetching = true;
  try {
    const resp = await fetch("/api/me/leaves", {
      method: "GET",
      credentials: "include",
      headers: { "Accept": "application/json" },
    });
    if (resp.status === 401 || resp.status === 403) {
      // Panel is silent on auth/scope failures — log + hide so the screen
      // doesn't accuse the user of something they can't fix.
      log("[my-leaves]", "hidden", { status: resp.status });
      panel.hidden = true;
      return;
    }
    if (resp.status >= 500 || resp.status === 503) {
      panel.hidden = false;
      setMyLeavesStatus("Could not load leave requests. Try again later.", "error");
      return;
    }
    const body = await resp.json().catch(() => null);
    if (!body || !Array.isArray(body.data)) {
      panel.hidden = false;
      setMyLeavesStatus("Could not load leave requests. Try again later.", "error");
      return;
    }
    _myLeavesRows = body.data;
    panel.hidden = false;
    setMyLeavesStatus("");
    renderMyLeavesPanel(_myLeavesRows);
  } catch (err) {
    logWarn("[my-leaves]", "fetch failed", err);
    panel.hidden = false;
    setMyLeavesStatus("Could not load leave requests. Try again later.", "error");
  } finally {
    _myLeavesFetching = false;
  }
}

function _sortMyLeavesRows(rows) {
  const { key, asc } = _myLeavesSort;
  const sorted = rows.slice();
  sorted.sort((a, b) => {
    const av = a[key]; const bv = b[key];
    if (av === bv) return 0;
    if (av === undefined || av === null) return 1;
    if (bv === undefined || bv === null) return -1;
    if (typeof av === "number" && typeof bv === "number") {
      return asc ? av - bv : bv - av;
    }
    return asc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
  });
  return sorted;
}

function renderMyLeavesPanel(rows) {
  const tbody = $("my-leaves-tbody");
  const table = $("my-leaves-table");
  const empty = $("my-leaves-empty");
  if (!tbody || !table || !empty) return;

  if (!Array.isArray(rows) || rows.length === 0) {
    tbody.innerHTML = "";
    table.hidden = true;
    empty.hidden = false;
    return;
  }

  empty.hidden = true;
  table.hidden = false;
  const sorted = _sortMyLeavesRows(rows);
  tbody.innerHTML = sorted.map((r) => {
    const status = String(r.status || "");
    const cls = LEAVE_STATUS_CLASS[status.toLowerCase()] || "leave-status--pending";
    return "<tr>"
      + "<td>" + escapeHtml(r.request_id || "") + "</td>"
      + "<td>" + escapeHtml(r.type || "") + "</td>"
      + "<td>" + escapeHtml(r.start_date || "") + "</td>"
      + "<td>" + escapeHtml(r.end_date || "") + "</td>"
      + "<td>" + escapeHtml(String(r.days_requested ?? "")) + "</td>"
      + "<td><span class='leave-status " + cls + "'>" + escapeHtml(status) + "</span></td>"
      + "</tr>";
  }).join("");
}

function wireMyLeavesPanel() {
  const table = $("my-leaves-table");
  if (!table) return;
  table.querySelectorAll("thead th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (_myLeavesSort.key === key) {
        _myLeavesSort.asc = !_myLeavesSort.asc;
      } else {
        _myLeavesSort = { key, asc: true };
      }
      renderMyLeavesPanel(_myLeavesRows);
    });
  });
}

// ─── Boot ─────────────────────────────────────────────────────────────────────

window.addEventListener("DOMContentLoaded", init);

// Expose for inline onclick handlers that may exist
window.app = { signIn };
