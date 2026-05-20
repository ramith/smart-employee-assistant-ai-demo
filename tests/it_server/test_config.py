"""Tests for it_server/config.py — Wave 4, Sprint 1.

Covers:
1.  Successful construction from a complete env dict
2.  Default values applied when optional vars are absent
3.  Frozen instance: mutation raises AttributeError/TypeError
4.  is_client_config() returns a correctly wired WSO2ISClientConfig
5.  Issuer/JWKS URL derived from base_url when not explicitly set
6.  Missing required variables raise ValueError with the var name
7.  Invalid URLs raise ValueError
8.  Frozenset parsing: comma-separated strings → frozenset
9.  N28 / F-15: expected_aud logged at INFO during from_env (caplog)
10. Boolean/port parsing edge cases
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

# it_server directory may have a legacy stub; load config.py directly
_ensure_pkg("it_server")
_load_module("it_server.config", "it_server/config.py")

# ── Imports ────────────────────────────────────────────────────────────────────

import logging

import pytest

from common.auth.wso2_is_client import WSO2ISClientConfig
from it_server.config import ITServerConfig

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _base_env() -> dict[str, str]:
    return {
        "WSO2_IS_BASE_URL": "https://is.example.com:9443",
        "DISABLE_SSL_VERIFY": "true",
        "IT_SERVER_EXPECTED_AUD": "it-oauth-client-id",
    }


# ── Test 1: successful construction ───────────────────────────────────────────

class TestSuccessfulConstruction:

    def test_basic_fields(self) -> None:
        cfg = ITServerConfig.from_env(_base_env())
        assert cfg.is_base_url == "https://is.example.com:9443"
        assert cfg.is_insecure_tls is True
        assert cfg.expected_aud == "it-oauth-client-id"

    def test_default_values_applied(self) -> None:
        cfg = ITServerConfig.from_env(_base_env())
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8004
        assert cfg.introspect_enabled is True
        assert cfg.required_scopes == frozenset({"it_assets_read_rest"})

    def test_instance_is_frozen(self) -> None:
        cfg = ITServerConfig.from_env(_base_env())
        with pytest.raises((AttributeError, TypeError)):
            cfg.port = 9999  # type: ignore[misc]

    def test_is_client_config_wired(self) -> None:
        cfg = ITServerConfig.from_env(_base_env())
        isc = cfg.is_client_config()
        assert isinstance(isc, WSO2ISClientConfig)
        assert isc.base_url == cfg.is_base_url
        assert isc.insecure_tls == cfg.is_insecure_tls

    def test_issuer_derived_from_base_url(self) -> None:
        cfg = ITServerConfig.from_env(_base_env())
        assert cfg.is_issuer == "https://is.example.com:9443/oauth2/token"

    def test_jwks_url_derived_from_base_url(self) -> None:
        cfg = ITServerConfig.from_env(_base_env())
        assert cfg.is_jwks_url == "https://is.example.com:9443/oauth2/jwks"

    def test_explicit_jwks_url_used_when_set(self) -> None:
        env = {**_base_env(), "JWKS_URL": "https://custom.example.com/jwks"}
        cfg = ITServerConfig.from_env(env)
        assert cfg.is_jwks_url == "https://custom.example.com/jwks"

    def test_custom_port_parsed(self) -> None:
        env = {**_base_env(), "IT_SERVER_PORT": "9004"}
        cfg = ITServerConfig.from_env(env)
        assert cfg.port == 9004

    def test_insecure_tls_false_when_not_set(self) -> None:
        env = {**_base_env(), "DISABLE_SSL_VERIFY": "false"}
        cfg = ITServerConfig.from_env(env)
        assert cfg.is_insecure_tls is False

    def test_insecure_tls_true_via_yes(self) -> None:
        env = {**_base_env(), "DISABLE_SSL_VERIFY": "yes"}
        cfg = ITServerConfig.from_env(env)
        assert cfg.is_insecure_tls is True

    def test_required_scopes_overridable(self) -> None:
        env = {**_base_env(), "IT_SERVER_REQUIRED_SCOPES": "it.read,it.admin"}
        cfg = ITServerConfig.from_env(env)
        assert cfg.required_scopes == frozenset({"it.read", "it.admin"})

    def test_introspect_url_captured(self) -> None:
        env = {
            **_base_env(),
            "WSO2_IS_INTROSPECT_URL": "https://is.example.com:9443/oauth2/introspect",
        }
        cfg = ITServerConfig.from_env(env)
        assert "introspect" in cfg.introspect_url

    def test_introspect_disabled_when_false(self) -> None:
        env = {**_base_env(), "IT_SERVER_INTROSPECT_ENABLED": "false"}
        cfg = ITServerConfig.from_env(env)
        assert cfg.introspect_enabled is False


# ── Test 2: N28 — expected_aud logged at INFO ─────────────────────────────────

class TestN28StartupLog:
    """expected_aud must appear in the INFO-level log line emitted by from_env."""

    def test_expected_aud_in_startup_log(self, caplog: pytest.LogCaptureFixture) -> None:
        env = {**_base_env(), "IT_SERVER_EXPECTED_AUD": "it-oauth-client-id-xyz"}
        with caplog.at_level(logging.INFO, logger="it_server.config"):
            ITServerConfig.from_env(env)
        assert "it-oauth-client-id-xyz" in caplog.text

    def test_token_enforcement_label_in_startup_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="it_server.config"):
            ITServerConfig.from_env(_base_env())
        assert "token enforcement active" in caplog.text

    def test_trusted_act_subs_in_startup_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = {**_base_env(), "IT_SERVER_TRUSTED_PEER_AGENTS": "it_agent-uuid-n28"}
        with caplog.at_level(logging.INFO, logger="it_server.config"):
            ITServerConfig.from_env(env)
        assert "it_agent-uuid-n28" in caplog.text


# ── Test 3: missing required variable → ValueError ────────────────────────────

class TestMissingRequiredVars:

    REQUIRED = [
        "WSO2_IS_BASE_URL",
        "IT_SERVER_EXPECTED_AUD",
    ]

    @pytest.mark.parametrize("var", REQUIRED)
    def test_missing_var_raises_value_error(self, var: str) -> None:
        env = _base_env()
        del env[var]
        with pytest.raises(ValueError, match=var):
            ITServerConfig.from_env(env)


# ── Test 4: invalid URL → ValueError ──────────────────────────────────────────

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
            ITServerConfig.from_env(env)


# ── Test 5: frozenset parsing ─────────────────────────────────────────────────

class TestFrozensetParsing:

    def test_trusted_act_subs_parsed(self) -> None:
        env = {
            **_base_env(),
            "IT_SERVER_TRUSTED_PEER_AGENTS": "uuid-it-1, uuid-it-2 ,uuid-it-3",
        }
        cfg = ITServerConfig.from_env(env)
        assert cfg.trusted_act_subs == frozenset({"uuid-it-1", "uuid-it-2", "uuid-it-3"})

    def test_trusted_act_subs_empty_when_not_set(self) -> None:
        cfg = ITServerConfig.from_env(_base_env())
        assert cfg.trusted_act_subs == frozenset()

    def test_allowed_origins_parsed(self) -> None:
        env = {
            **_base_env(),
            "ALLOWED_ORIGINS": "http://localhost:3001,http://127.0.0.1:3001",
        }
        cfg = ITServerConfig.from_env(env)
        assert "http://localhost:3001" in cfg.allowed_origins
        assert "http://127.0.0.1:3001" in cfg.allowed_origins

    def test_required_scopes_frozenset_type(self) -> None:
        cfg = ITServerConfig.from_env(_base_env())
        assert isinstance(cfg.required_scopes, frozenset)

    def test_trusted_act_subs_frozenset_type(self) -> None:
        cfg = ITServerConfig.from_env(_base_env())
        assert isinstance(cfg.trusted_act_subs, frozenset)


# ── Test 6: port parsing ───────────────────────────────────────────────────────

class TestPortParsing:

    def test_non_integer_port_raises(self) -> None:
        env = {**_base_env(), "IT_SERVER_PORT": "notaport"}
        with pytest.raises(ValueError, match="IT_SERVER_PORT"):
            ITServerConfig.from_env(env)

    def test_valid_port_accepted(self) -> None:
        env = {**_base_env(), "IT_SERVER_PORT": "9004"}
        cfg = ITServerConfig.from_env(env)
        assert cfg.port == 9004

    def test_default_port_is_8004(self) -> None:
        cfg = ITServerConfig.from_env(_base_env())
        assert cfg.port == 8004
