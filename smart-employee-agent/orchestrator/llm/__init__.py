"""Sprint 5 — LLM-driven chat routing + reply composition for the orchestrator.

Module map:
  client.py    — the ``LLMClient`` Protocol + the small dataclasses crossing it
                 (``ToolCatalogueEntry``, ``RoutedToolCall``, ``ToolOutcome``)
                 and ``LLMError``. **Stdlib-only — no langchain import here.**
  prompts.py   — router/composer system prompts, router-output parsing, the
                 outcome renderer, and the sensitive-key strip (sprint-5.md §2.7).
                 **Stdlib-only.**
  router.py    — ``resolve_tool_calls(message, deps)``: one LLM call → validate
                 each returned tool against the agent registry → keyword fallback.
                 **Stdlib + client/prompts only.**
  composer.py  — ``compose_reply(message, outcomes, fallback_text, deps)``: one
                 LLM call → the natural-language reply, with the keyword-mode
                 ``_render_result`` concatenation as the fallback. **Stdlib + client only.**
  amp_client.py — ``OpenAILLMClient(LLMClient)`` wrapping ``langchain-openai``'s
                 ``ChatOpenAI``. **This is the ONLY module that imports langchain.**
                 It is imported lazily by ``orchestrator/main.py`` only when
                 ``LLM_FALLBACK_MODE=llm`` and ``OPENAI_API_KEY`` is configured,
                 so keyword-only deployments (and the test venv) never need the package.

Security invariant (sprint-5.md §2): the LLM picks which tools are tried and
writes the reply prose — it never chooses scopes, writes consent copy, emits
HTML, or sees tokens/``sub``s.
"""
