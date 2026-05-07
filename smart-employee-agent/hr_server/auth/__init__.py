"""HR Server auth package.

Sprint 0 wires `common.auth` into the import path for forward use; the
existing `auth.jwt_validator.JWTValidator` remains the active validator
in the Pattern-1/2/3 flows. Sprint 2 (when `HR_INTROSPECT_ENABLED=true`)
delegates to `common.auth.introspector.Introspector` for revocation checks.

For the import to succeed, hr_server must build with the repo root as its
build context so that `/app/common/` is on the Python path. See
`hr_server/Dockerfile` and `docker-compose.yml`.
"""
import os
import sys
import logging

# Make /app/common importable when running inside the container.
# (PYTHONPATH-style fix without requiring a dedicated env var.)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT_IN_CONTAINER = os.path.abspath(os.path.join(_THIS_DIR, os.pardir, os.pardir))
if _REPO_ROOT_IN_CONTAINER not in sys.path:
    sys.path.insert(0, _REPO_ROOT_IN_CONTAINER)

logger = logging.getLogger(__name__)

# Sprint 0: import works → ImportError fails the build/run-time check that
# the package is wired correctly. Functional consumption deferred to Sprint 2.
try:
    from common import auth as common_auth  # noqa: F401
    logger.info("common.auth wired (introspection feature-flagged via HR_INTROSPECT_ENABLED)")
except ImportError as e:
    # Don't fail startup in Sprint 0 if common/ isn't on path yet — but log loudly.
    logger.warning("common.auth not importable yet: %s", e)


HR_INTROSPECT_ENABLED = os.getenv("HR_INTROSPECT_ENABLED", "false").lower() == "true"
"""Feature flag — defaults False. Sprint 2 flips to True after R12 passes."""
