"""The orchestrator's LLM router/composer/client/prompts modules must import
cleanly *without* ``langchain-openai`` installed — only ``amp_client.py``
imports langchain, and that module is imported lazily by ``main.py`` (only when
``LLM_FALLBACK_MODE=llm`` + a key is configured) and by the prod image (which
ships the dep). This guards the lazy-import discipline so a keyword-only
deployment / a stripped test venv keeps working.
"""

from __future__ import annotations

import builtins
import importlib
import sys


def test_llm_layer_importable_without_langchain(monkeypatch) -> None:
    # Simulate "langchain not installed": block the import + drop any cached modules.
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "langchain_openai" or name.startswith("langchain_openai.") \
           or name == "langchain_core" or name.startswith("langchain_core.") \
           or name == "langchain" or name.startswith("langchain."):
            raise ModuleNotFoundError(f"No module named {name!r} (simulated)")
        return real_import(name, *args, **kwargs)

    for mod in list(sys.modules):
        if mod.startswith(("langchain", "orchestrator.llm")):
            sys.modules.pop(mod, None)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    # These must all import fine — none of them touch langchain at module scope.
    importlib.import_module("orchestrator.llm.client")
    importlib.import_module("orchestrator.llm.prompts")
    importlib.import_module("orchestrator.llm.router")
    importlib.import_module("orchestrator.llm.composer")

    # ...and amp_client.py must FAIL (proving the langchain import lives only there).
    sys.modules.pop("orchestrator.llm.amp_client", None)
    try:
        importlib.import_module("orchestrator.llm.amp_client")
    except ModuleNotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("orchestrator.llm.amp_client imported despite langchain being unavailable")
