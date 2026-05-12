"""Tests for orchestrator/config.py — Wave 4, Sprint 1.

Covers:
1.  Successful construction from a complete env dict — fields and types correct
2.  Default values applied when optional vars are absent
3.  Frozen instance: mutation raises AttributeError/TypeError
4.  is_client_config() returns a correctly wired WSO2ISClientConfig
5.  Issuer/JWKS URL derived from base_url when not set explicitly
6.  Missing required variables raise ValueError with the var name
7.  Invalid URLs raise ValueError (is_base_url, hr_agent_url, it_agent_url)
8.  Frozenset parsing: comma-separated strings → frozenset
9.  F-15: OAuth client ID collision raises ValueError with "F-15"
10. Boolean parsing: true/1/yes → True; false/0 → False
11. Port parsing: non-integer raises ValueError
12. LLM / cookie / session overrides
"""

from __future__ import annotations

# ── Bootstrap ─────────────────────────────────────────────────────────────────
# Load modules directly via importlib.util, bypassing any broken __init__.py
# files in the service directories (same pattern as the existing Wave 1-3 tests).

import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str) -> None:
    """Create a stub package namespace in sys.modules if not already present."""
    if dotted_name in sys.modules:
        return
    stub = types.ModuleType(dotted_name)
    stub.__package__ = dotted_name
    parts = dotted_name.replace(".", "/")
    path = _ROOT / parts
    stub.__path__ = [str(path)]  # type: ignore[assignment]
    sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    """Load a single .py file into sys.modules under dotted_name."""
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


# Ensure intermediate namespaces exist
for _pkg in ("common", "common.auth"):
    _ensure_pkg(_pkg)

# Load the full dependency chain required by orchestrator/config.py
_load_module("common.auth.models", "common/auth/models.py")
_load_module("common.auth.errors", "common/auth/errors.py")
_load_module("common.auth.wso2_is_client", "common/auth/wso2_is_client.py")
_load_module("common.auth.actor_token_provider", "common/auth/actor_token_provider.py")

# The orchestrator directory uses a dash in the filesystem but we load it directly
_ensure_pkg("orchestrator")
_load_module("orchestrator.config", "orchestrator/config.py")

# ── Imports ────────────────────────────────────────────────────────────────────

import logging

import pytest

from common.auth.wso2_is_client import WSO2ISClientConfig
from orchestrator.config import OrchestratorConfig

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _base_env() -> dict[str, str]:
    """Return a complete environment dict that satisfies all required vars."""
    return {
        "WSO2_IS_BASE_URL": "https://is.example.com:9443",
        "IDP_INSECURE_TLS": "1",
        "ORCHESTRATOR_MCP_CLIENT_ID": "mcp-client-id",
        "ORCHESTRATOR_MCP_CLIENT_SECRET": "mcp-client-secret",
        "ORCHESTRATOR_AGENT_ID": "orch-agent-uuid",
        "ORCHESTRATOR_AGENT_SECRET": "orch-agent-secret",
        "ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID": "orch-oauth-client-id",
        "ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET": "orch-oauth-client-secret",
        "HR_AGENT_URL": "http://hr_agent:8001",
        "IT_AGENT_URL": "http://it_agent:8002",
        "HR_AGENT_OAUTH_CLIENT_ID": "hr-oauth-client-id",
        "IT_AGENT_OAUTH_CLIENT_ID": "it-oauth-client-id",
    }


# ── Test 1: successful construction ───────────────────────────────────────────

class TestSuccessfulConstruction:

    def test_basic_fields(self) -> None:
        cfg = OrchestratorConfig.from_env(_base_env())
        assert cfg.is_base_url == "https://is.example.com:9443"
        assert cfg.is_insecure_tls is True
        assert cfg.mcp_client_id == "mcp-client-id"
        assert cfg.mcp_client_secret == "mcp-client-secret"
        assert cfg.hr_agent_url == "http://hr_agent:8001"
        assert cfg.it_agent_url == "http://it_agent:8002"
        assert cfg.hr_agent_oauth_client_id == "hr-oauth-client-id"
        assert cfg.it_agent_oauth_client_id == "it-oauth-client-id"
        assert cfg.orchestrator_agent.agent_id == "orch-agent-uuid"
        assert cfg.orchestrator_agent.oauth_client_id == "orch-oauth-client-id"

    def test_default_values_applied(self) -> None:
        cfg = OrchestratorConfig.from_env(_base_env())
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8080
        assert cfg.llm_fallback_mode == "keyword"
        assert cfg.cookie_secure is False
        assert cfg.session_cookie_name == "orch_sid"
        assert cfg.gemini_api_key is None

    def test_instance_is_frozen(self) -> None:
        cfg = OrchestratorConfig.from_env(_base_env())
        with pytest.raises((AttributeError, TypeError)):
            cfg.port = 9999  # type: ignore[misc]

    def test_is_client_config_wired(self) -> None:
        cfg = OrchestratorConfig.from_env(_base_env())
        isc = cfg.is_client_config()
        assert isinstance(isc, WSO2ISClientConfig)
        assert isc.base_url == cfg.is_base_url
        assert isc.insecure_tls == cfg.is_insecure_tls

    def test_issuer_derived_from_base_url_when_absent(self) -> None:
        env = {k: v for k, v in _base_env().items() if k != "WSO2_IS_ISSUER"}
        cfg = OrchestratorConfig.from_env(env)
        assert cfg.is_issuer == "https://is.example.com:9443/oauth2/token"

    def test_custom_port_parsed(self) -> None:
        env = {**_base_env(), "ORCHESTRATOR_PORT": "9090"}
        cfg = OrchestratorConfig.from_env(env)
        assert cfg.port == 9090

    def test_llm_fallback_mode_overridable(self) -> None:
        env = {**_base_env(), "LLM_FALLBACK_MODE": "llm"}
        cfg = OrchestratorConfig.from_env(env)
        assert cfg.llm_fallback_mode == "llm"

    def test_gemini_api_key_present_when_set(self) -> None:
        env = {**_base_env(), "GEMINI_API_KEY": "key-abc123"}
        cfg = OrchestratorConfig.from_env(env)
        assert cfg.gemini_api_key == "key-abc123"

    def test_gemini_model_default_and_override(self) -> None:
        assert OrchestratorConfig.from_env(_base_env()).gemini_model == "gemini-2.5-flash"
        env = {**_base_env(), "GEMINI_MODEL": "gemini-2.5-pro"}
        assert OrchestratorConfig.from_env(env).gemini_model == "gemini-2.5-pro"

    def test_llm_timeout_and_max_tokens_parsed(self) -> None:
        cfg = OrchestratorConfig.from_env(_base_env())
        assert cfg.llm_timeout_s == 8.0
        assert cfg.llm_max_output_tokens == 512
        env = {**_base_env(), "LLM_TIMEOUT_S": "12.5", "LLM_MAX_OUTPUT_TOKENS": "256"}
        cfg2 = OrchestratorConfig.from_env(env)
        assert cfg2.llm_timeout_s == 12.5
        assert cfg2.llm_max_output_tokens == 256

    def test_llm_mode_without_key_does_not_crash(self, caplog) -> None:
        """LLM_FALLBACK_MODE=llm + no GEMINI_API_KEY → warns, keeps mode, no exception
        (main.py then builds llm_client=None → keyword-only behaviour)."""
        env = {k: v for k, v in _base_env().items() if k != "GEMINI_API_KEY"}
        env["LLM_FALLBACK_MODE"] = "llm"
        with caplog.at_level("WARNING"):
            cfg = OrchestratorConfig.from_env(env)
        assert cfg.llm_fallback_mode == "llm"
        assert cfg.gemini_api_key is None
        assert any("GEMINI_API_KEY is empty" in r.message for r in caplog.records)

    def test_llm_fallback_mode_blank_defaults_to_keyword(self) -> None:
        env = {**_base_env(), "LLM_FALLBACK_MODE": "   "}
        assert OrchestratorConfig.from_env(env).llm_fallback_mode == "keyword"

    def test_cookie_secure_parsed_truthy_values(self) -> None:
        for truthy in ("true", "True", "TRUE", "1", "yes", "YES"):
            env = {**_base_env(), "COOKIE_SECURE": truthy}
            cfg = OrchestratorConfig.from_env(env)
            assert cfg.cookie_secure is True, f"Expected True for COOKIE_SECURE={truthy!r}"

    def test_insecure_tls_false_when_zero(self) -> None:
        env = {**_base_env(), "IDP_INSECURE_TLS": "0"}
        cfg = OrchestratorConfig.from_env(env)
        assert cfg.is_insecure_tls is False


# ── Test 2: missing required variable → ValueError ────────────────────────────

class TestMissingRequiredVars:

    REQUIRED = [
        "WSO2_IS_BASE_URL",
        "ORCHESTRATOR_MCP_CLIENT_ID",
        "ORCHESTRATOR_MCP_CLIENT_SECRET",
        "ORCHESTRATOR_AGENT_ID",
        "ORCHESTRATOR_AGENT_SECRET",
        "ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID",
        "ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET",
        "HR_AGENT_URL",
        "IT_AGENT_URL",
        "HR_AGENT_OAUTH_CLIENT_ID",
        "IT_AGENT_OAUTH_CLIENT_ID",
    ]

    @pytest.mark.parametrize("var", REQUIRED)
    def test_missing_var_raises_value_error(self, var: str) -> None:
        env = _base_env()
        del env[var]
        with pytest.raises(ValueError, match=var):
            OrchestratorConfig.from_env(env)


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
            OrchestratorConfig.from_env(env)

    @pytest.mark.parametrize("bad_url", [
        "http://hr_agent:8001/with/path",
        "hr_agent:8001",
    ])
    def test_bad_hr_agent_url_raises(self, bad_url: str) -> None:
        env = {**_base_env(), "HR_AGENT_URL": bad_url}
        with pytest.raises(ValueError):
            OrchestratorConfig.from_env(env)

    @pytest.mark.parametrize("bad_url", [
        "http://it_agent:8002/with/path",
        "it_agent:8002",
    ])
    def test_bad_it_agent_url_raises(self, bad_url: str) -> None:
        env = {**_base_env(), "IT_AGENT_URL": bad_url}
        with pytest.raises(ValueError):
            OrchestratorConfig.from_env(env)


# ── Test 4: frozenset parsing ─────────────────────────────────────────────────

class TestFrozensetParsing:

    def test_trusted_specialist_subs_parsed(self) -> None:
        env = {
            **_base_env(),
            "TRUSTED_SPECIALIST_SUBS": "uuid-hr_agent,uuid-it_agent, uuid-extra ",
        }
        cfg = OrchestratorConfig.from_env(env)
        assert cfg.trusted_specialist_subs == frozenset(
            {"uuid-hr_agent", "uuid-it_agent", "uuid-extra"}
        )

    def test_trusted_specialist_subs_empty_when_not_set(self) -> None:
        cfg = OrchestratorConfig.from_env(_base_env())
        assert cfg.trusted_specialist_subs == frozenset()

    def test_allowed_origins_parsed(self) -> None:
        env = {
            **_base_env(),
            "ALLOWED_ORIGINS": "http://localhost:3001,http://127.0.0.1:3001",
        }
        cfg = OrchestratorConfig.from_env(env)
        assert "http://localhost:3001" in cfg.allowed_origins
        assert "http://127.0.0.1:3001" in cfg.allowed_origins

    def test_single_origin(self) -> None:
        env = {**_base_env(), "ALLOWED_ORIGINS": "http://myapp:3001"}
        cfg = OrchestratorConfig.from_env(env)
        assert cfg.allowed_origins == frozenset({"http://myapp:3001"})

    def test_frozenset_types_are_frozenset(self) -> None:
        cfg = OrchestratorConfig.from_env(_base_env())
        assert isinstance(cfg.trusted_specialist_subs, frozenset)
        assert isinstance(cfg.allowed_origins, frozenset)


# ── Test 5: F-15 OAuth client ID collision ────────────────────────────────────

class TestF15CollisionDetection:

    def test_collision_orch_equals_hr_raises(self) -> None:
        env = {**_base_env(), "HR_AGENT_OAUTH_CLIENT_ID": "orch-oauth-client-id"}
        with pytest.raises(ValueError, match="F-15"):
            OrchestratorConfig.from_env(env)

    def test_collision_orch_equals_it_raises(self) -> None:
        env = {**_base_env(), "IT_AGENT_OAUTH_CLIENT_ID": "orch-oauth-client-id"}
        with pytest.raises(ValueError, match="F-15"):
            OrchestratorConfig.from_env(env)

    def test_collision_hr_equals_it_raises(self) -> None:
        env = {**_base_env(), "IT_AGENT_OAUTH_CLIENT_ID": "hr-oauth-client-id"}
        with pytest.raises(ValueError, match="F-15"):
            OrchestratorConfig.from_env(env)

    def test_all_distinct_passes(self) -> None:
        cfg = OrchestratorConfig.from_env(_base_env())
        assert cfg.hr_agent_oauth_client_id != cfg.it_agent_oauth_client_id
        assert cfg.orchestrator_agent.oauth_client_id != cfg.hr_agent_oauth_client_id
        assert cfg.orchestrator_agent.oauth_client_id != cfg.it_agent_oauth_client_id


# ── Test 6: port parsing ───────────────────────────────────────────────────────

class TestPortParsing:

    def test_non_integer_port_raises(self) -> None:
        env = {**_base_env(), "ORCHESTRATOR_PORT": "not-a-number"}
        with pytest.raises(ValueError, match="ORCHESTRATOR_PORT"):
            OrchestratorConfig.from_env(env)

    def test_valid_port_accepted(self) -> None:
        env = {**_base_env(), "ORCHESTRATOR_PORT": "8090"}
        cfg = OrchestratorConfig.from_env(env)
        assert cfg.port == 8090

    def test_default_port_is_8080(self) -> None:
        cfg = OrchestratorConfig.from_env(_base_env())
        assert cfg.port == 8080
