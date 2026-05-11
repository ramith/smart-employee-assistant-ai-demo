# Sprint 5 — Stage 5: API Design

**Date:** 2026-05-11
**Scope:** No change to any HTTP contract the SPA or specialists see. S5 adds (a) one new internal MCP tool endpoint on `hr_server`, (b) one new internal Python interface (`LLMClient`), (c) new env vars. This doc locks all three.

---

## 1. Unchanged contracts (explicit no-ops)

- `POST /api/chat` (orchestrator) — request `{session_id, user_message}`, behaviour unchanged. Response: still the SSE-driven flow (`routing` / `consent_required` / `chat_message` events). **No new SSE event types.** The SPA's "Thinking…" affordance keys off "first SSE event for this request" — no protocol change.
- A2A `message/send` (orchestrator → hr_agent / it_agent) — unchanged. The orchestrator still sends `{tool_id, args, ...}`; the only difference is *where the `tool_id` + `args` came from* (LLM router instead of keyword router). The wire shape of a `ToolCall` is identical.
- All existing `hr_server` / `it_server` MCP + REST routes — unchanged.
- CIBA / consent-widget payloads (`ConsentRequiredPayload`, `CibaUrlEvent`) — unchanged. `action_text` still server-sourced.

## 2. NEW — `hr_server` MCP tool: `POST /mcp/tools/apply_leave`

Mirrors the existing `hr_server/mcp/tools.py` handler pattern (bearer extract → `validate_token(required_scopes={"hr_self_rest"})` → delegate to `hr_service`).

**Request body** (`ApplyLeaveArgs`, Pydantic):
```jsonc
{
  "leave_type":  "Annual Leave",      // required; must be a key of store.leave_policy
  "start_date":  "2026-06-10",        // required; YYYY-MM-DD
  "end_date":    "2026-06-14",        // required; YYYY-MM-DD; >= start_date
  "reason":      "family trip"         // optional; default ""
}
```
- All three of `leave_type`, `start_date`, `end_date` are **required by the dispatcher** (`_REQUIRED_ARGS["hr.apply_leave"]`) — so a partial LLM call fails *before* CIBA with `ERR-AGENT-002`, no round-trip wasted. The MCP body model itself can mark `reason` optional with default `""`; `leave_type`/`start_date`/`end_date` are `str` (non-optional) so a malformed body → 422 (FastAPI validation), consistent with the other write tools.
- Identity: `hr_service.apply_leave(sub, first_name, last_name, leave_type, start_date, end_date, reason)` — `sub` from `claims.sub`; `first_name` from `_username_for(claims)` (same pattern as `get_leave_balance`); `last_name=""`.

**Response** (`ApplyLeaveResult`, Pydantic) — mirrors `hr_service.apply_leave`'s return shape:
```jsonc
// success
{ "success": true, "request_id": "LR007" }
// business rejection (200, success=false + error) — same convention as assign_cubicle
{ "success": false, "error": "insufficient_notice",
  "message": "Annual Leave requires at least 7 days notice; start date is 2 day(s) away." }
```
Possible `error` values (all from `hr_service.apply_leave`): `invalid_leave_type`, `invalid_dates`, `insufficient_notice`, `insufficient_balance`.

**Scope:** `hr_self_rest` (self-service write to the caller's own leave record — same scope `get_leave_balance` / `get_my_leaves` use, per `docs/scope-policy.md`; *not* a new scope).

**Auth errors:** `ERR-AUTH-006` (no bearer), `ERR-MCP-003` (scope missing), `ERR-MCP-001`/etc. (token invalid) — identical to the other tools.

**Status codes:** 200 (success or business rejection), 401 (auth), 422 (malformed body).

## 3. NEW — `HRMcpClient.apply_leave(...)`

```python
async def apply_leave(
    self, *, token_b: OAuthToken,
    leave_type: str, start_date: str, end_date: str, reason: str = "",
    request_id: str | None = None,
) -> dict:
    """POST /mcp/tools/apply_leave with Bearer token-B. Scope: hr_self_rest."""
```
Body: `{"leave_type": ..., "start_date": ..., "end_date": ..., "reason": ...}`. Returns the parsed JSON dict (success or `{success: false, error, message}`).

## 4. NEW — HR Agent dispatcher registry entry

`hr_agent/ciba/orchestrator.py`:
```python
_REQUIRED_ARGS["hr.apply_leave"] = ["leave_type", "start_date", "end_date"]

_TOOL_REGISTRY["hr.apply_leave"] = (
    "Apply for leave on your behalf",          # action_text (consent widget copy)
    "apply_leave",                              # HRMcpClient method
    lambda args: {                              # args -> kwargs
        "leave_type": args.get("leave_type"),
        "start_date": args.get("start_date"),
        "end_date":   args.get("end_date"),
        "reason":     args.get("reason", ""),
    },
    "openid hr_self_rest",                       # CIBA scope override (explicit, like read_balance's None=env-default)
)
```
And the `hr_agent_valid.json` card gains a `hr.apply_leave` skill (`scope: "hr_self_rest"`, `required_scopes: ["hr_self_rest"]`).

## 5. NEW — `LLMClient` Protocol (orchestrator-internal)

`orchestrator/llm/client.py`:
```python
from typing import Protocol
from dataclasses import dataclass

@dataclass(frozen=True)
class ToolCatalogueEntry:
    agent_id: str          # "hr_agent"
    tool_id: str           # "hr.apply_leave"
    label: str             # "Apply for leave"
    description: str       # human description from the card
    args: list[str]        # arg names the tool accepts, e.g. ["leave_type","start_date","end_date","reason"]

@dataclass(frozen=True)
class ToolOutcome:
    agent_id: str
    tool_id: str
    ok: bool
    data: dict | None      # result data when ok
    error_id: str | None   # e.g. "ERR-CIBA-005", "ERR-AGENT-002" when not ok
    reason: str | None     # human reason when not ok

class LLMClient(Protocol):
    async def route(self, user_message: str, catalogue: list[ToolCatalogueEntry]) -> list["RoutedToolCall"]:
        """Pick zero or more tools + extract their args from the message.
        Raises LLMError on transport/parse failure (caller falls back to keyword router)."""
        ...

    async def compose(self, user_message: str, outcomes: list[ToolOutcome]) -> str:
        """Turn the tool outcomes into one natural-language reply.
        Raises LLMError on transport failure (caller falls back to _render_result concatenation)."""
        ...

@dataclass(frozen=True)
class RoutedToolCall:
    agent_id: str
    tool_id: str
    args: dict             # raw args from the LLM; validated/coerced by the router before becoming a chat ToolCall
```
- The concrete impl `GeminiLLMClient(LLMClient)` wraps `langchain_google_genai.ChatGoogleGenerativeAI` (model from `cfg.gemini_model`, `temperature` per call, `max_output_tokens=cfg.llm_max_output_tokens`, per-call `asyncio.wait_for(..., cfg.llm_timeout_s)`).
- `FakeLLMClient(LLMClient)` (in `tests/`) returns canned values; constructed with a list of `RoutedToolCall`s and a canned reply string, or an `LLMError` to raise — covers the fallback tests.
- `LLMError(Exception)` — single exception type; the router/composer catch it (plus `asyncio.TimeoutError`, JSON parse errors, and any `langchain` exception) and fall back.

## 6. NEW — env vars (read in `OrchestratorConfig.from_env`)

| Var | Default | Meaning |
|---|---|---|
| `LLM_FALLBACK_MODE` | `keyword` | `llm` → LLM router+composer active (with keyword/`_render_result` fallback). Any other value → keyword-only (today). **Now actually read at runtime.** |
| `GEMINI_API_KEY` | *(none)* | Required when mode is `llm`; if mode is `llm` and this is empty, `from_env` logs a warning and the orchestrator behaves as keyword-only (graceful — don't crash). |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model id. |
| `LLM_TIMEOUT_S` | `8` | Per-LLM-call timeout (seconds). On timeout → fallback. |
| `LLM_MAX_OUTPUT_TOKENS` | `512` | Cap on Gemini output tokens (both calls). |

`OrchestratorConfig` gains: `gemini_model: str = "gemini-2.5-flash"`, `llm_timeout_s: float = 8.0`, `llm_max_output_tokens: int = 512`. (`llm_fallback_mode` and `gemini_api_key` already exist.) `docker-compose.yml` orchestrator service: the new vars come through `env_file` (whole-file load); add explicit `GEMINI_MODEL` / `LLM_TIMEOUT_S` lines with `${...:-default}` for clarity. **`GEMINI_API_KEY` is NOT added to the compose `environment:` block** (it must only ever come from the gitignored `.env`).

## 7. Contract test obligations (Stage 10)

- `apply_leave` MCP tool: 200 success path (store reflects new request), 200 business-rejection paths (each `error` value), 401 missing scope (`ERR-MCP-003`), 401 no bearer, 422 malformed body.
- `HRMcpClient.apply_leave`: posts the right body to the right path with the bearer header.
- HR dispatcher: `hr.apply_leave` with all args → CIBA initiated with scope `openid hr_self_rest`; with a missing required arg → `ERR-AGENT-002` and *no* CIBA call.
- `LLMClient` Protocol: `FakeLLMClient` satisfies it (structural typing test).
- `OrchestratorConfig.from_env`: `LLM_FALLBACK_MODE=llm` + no key → warns, mode effectively keyword; with key → llm mode + new vars parsed with defaults.
