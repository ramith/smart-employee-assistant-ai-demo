"""FastAPI router for the unauthenticated ``POST /public/chat`` endpoint.

Mounted at the ``/public`` prefix in ``orchestrator/main.py``.
No ``verify_token`` dependency — this is intentionally unauthenticated.
Input is validated by Pydantic before reaching the handler (F-3).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field, create_model

from orchestrator.chat.public_handler import PublicInfoHandler


class PublicChatResponse(BaseModel):
    reply: str


def build_public_router(
    handler: PublicInfoHandler,
    *,
    max_message_chars: int = 500,
) -> APIRouter:
    """Return a router with the ``POST /chat`` route wired to *handler*.

    Args:
        handler: The stateless handler that produces replies.
        max_message_chars: Maximum allowed message length, mirrored from
            ``OrchestratorConfig.public_chat_max_chars`` so the limit is
            configurable at runtime rather than hardcoded (F-3).

    Notes:
        ``create_model`` is used instead of a locally-defined class because
        this module uses ``from __future__ import annotations``, which turns
        all annotations into strings.  ``typing.get_type_hints`` cannot
        resolve a locally-scoped class from those strings, causing FastAPI
        to misclassify the request body as a query parameter.  ``create_model``
        registers the model in Pydantic's global registry at call time, so
        the annotation is always resolvable.
    """
    router = APIRouter(tags=["public"])

    # create_model avoids the from-future-annotations / local-class resolution
    # issue described in the docstring above.
    _Req = create_model(
        "PublicChatRequest",
        message=(str, Field(min_length=1, max_length=max_message_chars)),
    )

    @router.post("/chat", response_model=PublicChatResponse)
    async def public_chat(body: _Req) -> PublicChatResponse:  # type: ignore[valid-type]
        reply = await handler.answer(body.message.strip())
        return PublicChatResponse(reply=reply)

    # ``from __future__ import annotations`` turns ``body: _Req`` into the
    # string ``"_Req"``.  ``typing.get_type_hints`` cannot resolve that name
    # from the module globals because ``_Req`` is local to this factory
    # function.  Overwriting the annotation with the live type object lets
    # FastAPI (which calls ``get_type_hints`` internally) recognise the body
    # as a Pydantic model rather than a plain query parameter.
    public_chat.__annotations__["body"] = _Req

    return router
