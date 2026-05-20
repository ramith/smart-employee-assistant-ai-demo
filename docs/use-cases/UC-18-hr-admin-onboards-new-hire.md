# UC-18 — HR Admin onboards a new hire (seat + laptop + phone, one chat turn)

**Status:** written (S5.16). Composes existing capabilities — no new tools or scopes; relies on the orchestrator's multi-tool serial fan-out (`_run_serial_fan_out` already iterates a list of `ToolCall`s, each resolved to its own agent and given its own per-action CIBA consent) plus the S5.14/S5.15 guided cubicle/seat + IT-issuance flows.

## Goal

The "Act I — Onboarding" demo beat: an HR Admin sets up a new employee's workspace — a desk/seat **and** a laptop **and** a phone — in a single natural-language chat message, and watches three CIBA consent widgets fire in sequence (one per write action, in the same human's session). The result shows up in both the HR-admin reporting tabs and the new hire's own panels.

## Pre-conditions

- Signed in as `hr_admin_user` (HR Admin role — holds `hr_read_rest`, `hr_assets_write_rest`, `it_assets_read_rest`, `it_assets_write_rest`, …).
- The new hire (`employee_user` in the demo, or any IS user created with `username` = the email local-part — see `wso2-is-setup.md` §5.5) exists.
- `hr-agent` and `it-agent` OAuth apps subscribed to all their API scopes (verify: `scripts/check-is-config.py` Section 4c — must include `hr_assets_write_rest`).
- OpenAI/LLM router available (the multi-resource split is an LLM-router behaviour; in keyword-only mode the resources have to be issued one message at a time — see §"Degraded mode").

## Main flow — fully-specified (one turn, three consents)

The cleanest demo beat: name the seat and the two device catalogue ids up front.

1. HR Admin types, e.g.:
   > **Set up the new hire employee_user: assign C-027 to her, and issue MBP-14-001 and PHN-IP15-001 to her.**
2. Orchestrator → LLM router → `tools = [hr.cubicle_assign (cubicle_id=C-027, employee_username=employee_user), it.issue_asset (asset_id=MBP-14-001, employee_id=employee_user), it.issue_asset (asset_id=PHN-IP15-001, employee_id=employee_user)]` — three calls across two agents (`hr_agent`, `it_agent`).
3. `_run_serial_fan_out` runs them **in order**, each its own CIBA flow:
   - **Consent 1** — "Assign cubicle C-027 to employee_user" (`hr_assets_write_rest`). Admin approves.
   - **Consent 2** — "Issue MBP-14-001 to employee_user" (`it_assets_write_rest`). Admin approves.
   - **Consent 3** — "Issue PHN-IP15-001 to employee_user" (`it_assets_write_rest`). Admin approves.
4. Composer reply names all three outcomes: *"Done — C-027 on floor 2 is now assigned to employee_user, and I've issued MBP-14-001 and PHN-IP15-001 to employee_user."*

## Main flow — guided (vague request → pick → assign)

If the admin doesn't name specifics:

1. > **Set up employee_user — give her a seat, a laptop, and a phone.**
2. Router → `tools = [hr.cubicle_summary, it.list_available_assets (asset_type=laptop), it.list_available_assets (asset_type=phone)]` — the three *read* (browse) tools. Each fires a CIBA consent (`hr_read_rest` / `it_assets_read_rest`); admin approves each.
3. Composer presents all the options in one reply — the per-floor cubicle vacancy summary, the available laptops (model + catalogue id + stock), the available phones — and asks the admin to pick: *"Which floor for the seat, and which laptop and phone (the catalogue ids)?"*
4. > **Floor 2, MBP-14-001, PHN-IP15-001.**
5. Router → `tools = [hr.cubicle_list_floor (floor=2), it.issue_asset (MBP-14-001, employee_user), it.issue_asset (PHN-IP15-001, employee_user)]` → composer lists the floor-2 vacant seats and (after the two issue consents) confirms the devices, then asks which specific seat.
6. > **C-027.**
7. Router → `tools = [hr.cubicle_assign (C-027, employee_user)]` → consent → *"C-027 on floor 2 is now assigned to employee_user."*

(The guided path is longer because `hr.cubicle_assign` needs a specific `C-NNN`, not just a floor. The fully-specified path above is the recommended demo beat.)

## Post-conditions / verification

- **HR Admin → Reports → Cubicles:** C-027 row → employee_user.
- **HR Admin → Reports → Devices:** two rows for employee_user — MBP-14-001 (laptop, outstanding) and PHN-IP15-001 (phone, outstanding).
- **Sign in as `employee_user`:** "My Cubicle" → C-027 (floor 2); "My IT Assets" → the laptop + phone.
- IS audit log: three CIBA events for `hr_admin_user`, scopes `hr_assets_write_rest`, `it_assets_write_rest`, `it_assets_write_rest`, `act.sub` = the respective agent ids.

## Notes / edge behaviour

- **Per-action consent is preserved.** Three writes → three consent widgets. There is no "batch consent" — that's deliberate (each delegation is a separate, explicitly-approved action).
- **Partial failure is independent per tool.** If consent 2 is denied, consent 1's cubicle assignment still stands and consent 3 still fires; the composer reports the mix ("C-027 assigned; you declined the laptop; PHN-IP15-001 issued").
- **Mixed-agent ordering** follows the order the router emitted them; the cubicle (hr_agent) and the devices (it_agent) interleave per that order.
- **Prompt-injection safety unchanged (UC-17 §4):** an `Employee`-role user typing the same onboarding message gets the writes denied by IS at CIBA (no `hr_assets_write_rest` / `it_assets_write_rest`) — the per-tool scope is server-fixed in each agent's `_TOOL_REGISTRY`, never from the LLM.

## Degraded mode (keyword router — OpenAI / WSO2 AI Gateway unavailable)

The keyword router does match cubicle/seat phrasings (`seat` → `hr.cubicle_summary`) and `laptop`/`phone`/`monitor`/`screen` → `it.list_available_assets`, so a single "give employee_user a seat, a laptop, and a phone" still fans out to the browse tools — but it can't reliably split `it.issue_asset` from a vague request (it needs a specific catalogue id), so the *issue* step has to be done one message at a time with explicit ids ("issue MBP-14-001 to employee_user", then "issue PHN-IP15-001 to employee_user"). The LLM router handles the one-turn fully-specified flow.
