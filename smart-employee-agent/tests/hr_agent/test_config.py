"""Tests for hr_agent/config.py — Wave 4, Sprint 1.

Covers:
1.  Successful construction from a complete env dict
2.  Default values applied when optional vars are absent
3.  Frozen instance: mutation raises AttributeError/TypeError
4.  is_client_config() returns a correctly wired WSO2ISClientConfig
5.  Issuer/JWKS URL derived from base_url when not explicitly set
6.  Missing required variables raise ValueError with the var name
7.  Invalid URLs raise ValueError (is_base_url, hr_server_url)
8.  Frozenset parsing: comma-separated strings → frozenset
9.  Boolean parsing: various truthy/falsy strings
10. Port parsing: non-integer raises ValueError
11. CIBA scope, canonical URL, and redirect URI overrides
"""

from __future__ import annotations

# ── Bootstrap ─────────────────────────────────────────────────────────────────

import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str) -> None:
    if dotted_name in sys.modules:
        return
    stub = types.ModuleType(dotted_name)
    stub.__package__ = dotted_name
    stub.__path__ = [str(_ROOT / dotted_name.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    if dotted_name in sys.modules:
        return sys.modules[dotted_name]
    file_path = _ROOT / rel_path
    spec = importlib.util.spec_from_file_location(dotted_name, file_path)
    assert spec is not None and spec.loader is not None, f"Cannot load {file_path}"
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted_name.rsplit(".", 1)[0] if "." in dotted_name else ""
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


for _pkg in ("common", "common.auth"):
    _ensure_pkg(_pkg)

_load_module("common.auth.models", "common/auth/models.py")
_load_module("common.auth.errors", "common/auth/errors.py")
_load_module("common.auth.wso2_is_client", "common/auth/wso2_is_client.py")
_load_module("common.auth.actor_token_provider", "common/auth/actor_token_provider.py")

# hr_agent directory has a dash in the filesystem; load config.py directly
_ensure_pkg("hr_agent")
_load_module("hr_agent.config", "hr_agent/config.py")

# ── Imports ────────────────────────────────────────────────────────────────────

import pytest

from common.auth.wso2_is_client import WSO2ISClientConfig
from hr_agent.config import HRAgentConfig

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _base_env() -> dict[str, str]:
    return {
        "WSO2_IS_BASE_URL": "https://is.example.com:9443",
        "IDP_INSECURE_TLS": "1",
        "HR_AGENT_ID": "hr_agent-uuid",
        "HR_AGENT_SECRET": "hr_agent-secret",
        "HR_AGENT_OAUTH_CLIENT_ID": "hr-oauth-client-id",
        "HR_AGENT_OAUTH_CLIENT_SECRET": "hr-oauth-client-secret",
        "HR_MCP_SERVER_URL": "http://hr_server:8000",
        "HR_EXPECTED_INBOUND_AUD": "orch-mcp-client-id",
    }


# ── Test 1: successful construction ───────────────────────────────────────────

class TestSuccessfulConstruction:

    def test_basic_fields(self) -> None:
        cfg = HRAgentConfig.from_env(_base_env())
        assert cfg.is_base_url == "https://is.example.com:9443"
        assert cfg.is_insecure_tls is True
        assert cfg.agent.agent_id == "hr_agent-uuid"
        assert cfg.agent.agent_secret == "hr_agent-secret"
        assert cfg.agent.oauth_client_id == "hr-oauth-client-id"
        assert cfg.agent.oauth_client_secret == "hr-oauth-client-secret"
        assert cfg.hr_server_url == "http://hr_server:8000"
        assert cfg.expected_inbound_aud == "orch-mcp-client-id"

    def test_default_values_applied(self) -> None:
        cfg = HRAgentConfig.from_env(_base_env())
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8001
        assert cfg.ciba_scope == "openid hr.read"
        assert cfg.max_poll_seconds == 240
        assert cfg.canonical_url == "http://hr_agent:8001/a2a"

    def test_instance_is_frozen(self) -> None:
        cfg = HRAgentConfig.from_env(_base_env())
        with pytest.raises((AttributeError, TypeError)):
            cfg.port = 9999  # type: ignore[misc]

    def test_is_client_config_wired(self) -> None:
        cfg = HRAgentConfig.from_env(_base_env())
        isc = cfg.is_client_config()
        assert isinstance(isc, WSO2ISClientConfig)
        assert isc.base_url == cfg.is_base_url
        assert isc.insecure_tls == cfg.is_insecure_tls

    def test_issuer_derived_from_base_url_when_absent(self) -> None:
        cfg = HRAgentConfig.from_env(_base_env())
        assert cfg.is_issuer == "https://is.example.com:9443/oauth2/token"

    def test_jwks_url_derived_from_base_url(self) -> None:
        cfg = HRAgentConfig.from_env(_base_env())
        assert cfg.is_jwks_url == "https://is.example.com:9443/oauth2/jwks"

    def test_custom_port_parsed(self) -> None:
        env = {**_base_env(), "HR_AGENT_PORT": "8011"}
        cfg = HRAgentConfig.from_env(env)
        assert cfg.port == 8011

    def test_ciba_scope_overridable(self) -> None:
        env = {**_base_env(), "HR_CIBA_SCOPE": "openid hr.read hr.write"}
        cfg = HRAgentConfig.from_env(env)
        assert cfg.ciba_scope == "openid hr.read hr.write"

    def test_insecure_tls_false_when_zero(self) -> None:
        env = {**_base_env(), "IDP_INSECURE_TLS": "0"}
        cfg = HRAgentConfig.from_env(env)
        assert cfg.is_insecure_tls is False

    def test_insecure_tls_true_via_yes(self) -> None:
        env = {**_base_env(), "IDP_INSECURE_TLS": "yes"}
        cfg = HRAgentConfig.from_env(env)
        assert cfg.is_insecure_tls is True

    def test_redirect_uri_default_contains_callback(self) -> None:
        cfg = HRAgentConfig.from_env(_base_env())
        assert "agent-callback" in cfg.agent.redirect_uri

    def test_custom_canonical_url(self) -> None:
        env = {**_base_env(), "HR_AGENT_CANONICAL_URL": "https://hr.example.com/a2a"}
        cfg = HRAgentConfig.from_env(env)
        assert cfg.canonical_url == "https://hr.example.com/a2a"


# ── Test 2: missing required variable → ValueError ────────────────────────────

class TestMissingRequiredVars:

    REQUIRED = [
        "WSO2_IS_BASE_URL",
        "HR_AGENT_ID",
        "HR_AGENT_SECRET",
        "HR_AGENT_OAUTH_CLIENT_ID",
        "HR_AGENT_OAUTH_CLIENT_SECRET",
        "HR_MCP_SERVER_URL",
        "HR_EXPECTED_INBOUND_AUD",
    ]

    @pytest.mark.parametrize("var", REQUIRED)
    def test_missing_var_raises_value_error(self, var: str) -> None:
        env = _base_env()
        del env[var]
        with pytest.raises(ValueError, match=var):
            HRAgentConfig.from_env(env)


# ── Test 3: invalid URL → ValueError ──────────────────────────────────────────

class TestURLValidation:

    @pytest.mark.parametrize("bad_url", [
        "not-a-url",
        "https://is.example.com/with/path",
        "https://is.example.com:9443/",
        "ftp://is.example.com:9443",
    ])
    def test_bad_is_base_url_raises(self, bad_url: str) -> None:
        env = {**_base_env(), "WSO2_IS_BASE_URL": bad_url}
        with pytest.raises(ValueError):
            HRAgentConfig.from_env(env)

    @pytest.mark.parametrize("bad_url", [
        "http://hr_server:8000/mcp",
        "hr_server:8000",
        "ftp://hr_server:8000",
    ])
    def test_bad_hr_server_url_raises(self, bad_url: str) -> None:
        env = {**_base_env(), "HR_MCP_SERVER_URL": bad_url}
        with pytest.raises(ValueError):
            HRAgentConfig.from_env(env)


# ── Test 4: frozenset parsing ─────────────────────────────────────────────────

class TestFrozensetParsing:

    def test_trusted_orchestrator_subs_parsed(self) -> None:
        env = {
            **_base_env(),
            "HR_TRUSTED_PEER_AGENTS": "uuid-orch-1, uuid-orch-2 ,uuid-orch-3",
        }
        cfg = HRAgentConfig.from_env(env)
        assert cfg.trusted_orchestrator_subs == frozenset(
            {"uuid-orch-1", "uuid-orch-2", "uuid-orch-3"}
        )

    def test_trusted_orchestrator_subs_empty_when_not_set(self) -> None:
        cfg = HRAgentConfig.from_env(_base_env())
        assert cfg.trusted_orchestrator_subs == frozenset()

    def test_single_sub_parsed(self) -> None:
        env = {**_base_env(), "HR_TRUSTED_PEER_AGENTS": "single-uuid"}
        cfg = HRAgentConfig.from_env(env)
        assert cfg.trusted_orchestrator_subs == frozenset({"single-uuid"})

    def test_whitespace_only_entries_ignored(self) -> None:
        env = {**_base_env(), "HR_TRUSTED_PEER_AGENTS": "uuid-a,  ,uuid-b"}
        cfg = HRAgentConfig.from_env(env)
        assert cfg.trusted_orchestrator_subs == frozenset({"uuid-a", "uuid-b"})

    def test_result_type_is_frozenset(self) -> None:
        cfg = HRAgentConfig.from_env(_base_env())
        assert isinstance(cfg.trusted_orchestrator_subs, frozenset)


# ── Test 5: port parsing ───────────────────────────────────────────────────────

class TestPortParsing:

    def test_non_integer_port_raises(self) -> None:
        env = {**_base_env(), "HR_AGENT_PORT": "abc"}
        with pytest.raises(ValueError, match="HR_AGENT_PORT"):
            HRAgentConfig.from_env(env)

    def test_valid_port_accepted(self) -> None:
        env = {**_base_env(), "HR_AGENT_PORT": "9001"}
        cfg = HRAgentConfig.from_env(env)
        assert cfg.port == 9001

    def test_default_port_is_8001(self) -> None:
        cfg = HRAgentConfig.from_env(_base_env())
        assert cfg.port == 8001
