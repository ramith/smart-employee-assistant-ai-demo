"""IT Server auth package.

Mirrors hr_server/auth/__init__.py — wires `common.auth` into the import
path. IT introspection is ON from day 1 (greenfield; no behavior-change
risk) per milestone-plan §3.4 task 23.
"""
import os
import sys
import logging

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT_IN_CONTAINER = os.path.abspath(os.path.join(_THIS_DIR, os.pardir, os.pardir))
if _REPO_ROOT_IN_CONTAINER not in sys.path:
    sys.path.insert(0, _REPO_ROOT_IN_CONTAINER)

logger = logging.getLogger(__name__)

try:
    from common import auth as common_auth  # noqa: F401
    logger.info("common.auth wired (it_server introspection ON by default)")
except ImportError as e:
    logger.warning("common.auth not importable: %s", e)


IT_SERVER_INTROSPECT_ENABLED = os.getenv("IT_SERVER_INTROSPECT_ENABLED", "true").lower() == "true"
