"""Tests for common/auth/binding_messages.py — Sprint 3 3B.2 / FIX-17.

Pins the reason → template precedence rules that drive UC-09 / UC-10 /
UC-06 binding-message copy. The visible-to-user wording is what the demo
audience reads on the consent widget; if a future change breaks the
precedence we want to catch it before the live walk.

Coverage:
  B-01  ``reason="admin_terminated"`` always picks POST_ADMIN_TERMINATED
        (beats is_refresh).
  B-02  ``reason="user_signed_out"`` picks POST_USER_SIGNED_OUT.
  B-03  ``reason="token_expired"`` picks REFRESH (synonym of is_refresh).
  B-04  ``reason=None, is_refresh=True`` picks REFRESH.
  B-05  ``reason=None, is_refresh=False`` picks FRESH.
  B-06  Unknown reason falls through to FRESH/REFRESH per is_refresh.
  B-07  render() formats placeholders in each template.
  B-08  request_id is truncated to 8 chars in rendered output.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted: str) -> None:
    if dotted in sys.modules:
        return
    stub = types.ModuleType(dotted)
    stub.__package__ = dotted
    stub.__path__ = [str(_ROOT / dotted.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted] = stub


def _load(dotted: str, rel: str) -> types.ModuleType:
    if dotted in sys.modules and hasattr(sys.modules[dotted], "__file__"):
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


for _pkg in ("common", "common.auth"):
    _ensure_pkg(_pkg)
_bm = _load("common.auth.binding_messages", "common/auth/binding_messages.py")

FRESH = _bm.FRESH
REFRESH = _bm.REFRESH
POST_USER_SIGNED_OUT = _bm.POST_USER_SIGNED_OUT
POST_ADMIN_TERMINATED = _bm.POST_ADMIN_TERMINATED
select_template = _bm.select_template
render = _bm.render


# ---------------------------------------------------------------------------
# B-01..06 — select_template precedence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_refresh", [False, True])
def test_b01_admin_terminated_beats_is_refresh(is_refresh):
    """admin_terminated always wins, even when is_refresh=True."""
    assert select_template("admin_terminated", is_refresh=is_refresh) is POST_ADMIN_TERMINATED


@pytest.mark.parametrize("is_refresh", [False, True])
def test_b02_user_signed_out_beats_is_refresh(is_refresh):
    """user_signed_out wins over is_refresh too — explicit user action takes precedence."""
    assert select_template("user_signed_out", is_refresh=is_refresh) is POST_USER_SIGNED_OUT


def test_b03_token_expired_picks_refresh():
    """reason=token_expired is a synonym for is_refresh — both yield REFRESH."""
    assert select_template("token_expired", is_refresh=False) is REFRESH


def test_b04_no_reason_with_refresh_picks_refresh():
    """No specific reason but cache had a prior entry → routine UC-06 refresh."""
    assert select_template(None, is_refresh=True) is REFRESH


def test_b05_no_reason_no_refresh_picks_fresh():
    """First-ever consent: FRESH."""
    assert select_template(None, is_refresh=False) is FRESH


def test_b06_unknown_reason_falls_through():
    """An unrecognised reason string degrades to FRESH/REFRESH cleanly.

    This is the forward-compat path: if a future sprint adds a new reason
    code that this version of the code hasn't been taught about yet, the
    receiver shouldn't crash — it shows the routine copy. SIEM can still
    grep for reason=<unknown> to detect drift.
    """
    assert select_template("force_logout_v9", is_refresh=False) is FRESH
    assert select_template("force_logout_v9", is_refresh=True) is REFRESH


# ---------------------------------------------------------------------------
# B-07/B-08 — render() formats each template correctly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template,expected_phrase",
    [
        (FRESH, "wants to"),
        (REFRESH, "previous access has expired"),
        (POST_USER_SIGNED_OUT, "Your previous session ended"),
        (POST_ADMIN_TERMINATED, "ended by your administrator"),
    ],
)
def test_b07_render_includes_distinguishing_phrase(template, expected_phrase):
    """Each template renders with its visibly-distinct phrase intact."""
    out = render(
        template,
        agent_label="HR Agent",
        action="View your leave balance",
        request_id="abc12345-uuid-tail",
    )
    assert expected_phrase in out
    assert "HR Agent" in out
    assert "View your leave balance" in out


def test_b08_request_id_truncated_to_8_chars():
    """request_id is truncated to first 8 chars in rendered output."""
    long_rid = "abcd1234-very-long-request-id"
    out = render(
        FRESH,
        agent_label="HR Agent",
        action="View your leave balance",
        request_id=long_rid,
    )
    assert "abcd1234" in out
    assert "very-long-request-id" not in out


# ---------------------------------------------------------------------------
# Integration sanity — admin_terminated copy is visibly distinct from
# user_signed_out copy. The slice-plan exit criterion is "the consent
# widget visibly says 'your previous session was ended' (not 'you signed
# out')". This pins that.
# ---------------------------------------------------------------------------


def test_admin_terminated_renders_visibly_different_from_user_signed_out():
    """Renders for admin_terminated and user_signed_out share no body phrase."""
    common_kwargs = dict(
        agent_label="HR Agent",
        action="View your leave balance",
        request_id="r-12345678",
    )
    admin_msg = render(
        select_template("admin_terminated", is_refresh=False),
        **common_kwargs,
    )
    user_msg = render(
        select_template("user_signed_out", is_refresh=False),
        **common_kwargs,
    )
    assert admin_msg != user_msg
    assert "administrator" in admin_msg
    assert "administrator" not in user_msg
