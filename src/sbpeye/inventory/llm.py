"""The narrow LLM interface the inventory service depends on.

Kept to one method so the service can be exercised with a stub, and so no adapter's
client type leaks into search semantics. ``AIClientAdapter`` wraps the existing
``AIClient``; nothing else in the package imports it.
"""

import json
from typing import Any, Protocol


class InventoryLLM(Protocol):
    """Structured completion. Implementations must return a parsed JSON object."""

    @property
    def model_name(self) -> str: ...

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


class AIClientAdapter:
    """Adapts ``sbpeye.ai.AIClient`` to :class:`InventoryLLM`."""

    def __init__(self, client):
        self._client = client

    @property
    def model_name(self) -> str:
        return getattr(getattr(self._client, "config", None), "model", "") or ""

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        raw = self._client._complete_json(
            system_prompt, user_prompt, json_schema=json_schema, temperature=0.0
        )
        parsed = self._client._parse_json_object(raw)
        if not isinstance(parsed, dict):
            raise ValueError("model did not return a JSON object")
        return parsed


def load_llm(db=None) -> InventoryLLM:
    """Build the default LLM adapter. Import-local so the service stays LLM-optional."""
    from ..ai import get_ai_client

    return AIClientAdapter(get_ai_client(db))


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def field_of_type(response: dict[str, Any], key: str, kind: type) -> Any | None:
    """The field named ``key``, or — failing that — the object's only field of ``kind``.

    Only the ``json_schema`` tier makes the provider enforce field names. Under
    ``json_object`` and ``text`` the model names the field itself, and it was measured
    answering the term-generation schema's ``terms`` with ``search_terms`` and a full,
    correct list of terms. Reading one hard-coded key throws that away and reports it
    as though the layer had produced nothing, which is indistinguishable from real
    failure and costs the whole lexical arm.

    Falling back only when there is exactly one field of the expected type keeps this a
    rename-tolerance, not a guess: an object with two lists in it is ambiguous, so it
    is left to fail loudly.
    """
    value = response.get(key)
    if isinstance(value, kind):
        return value
    matches = [v for v in response.values() if isinstance(v, kind)]
    return matches[0] if len(matches) == 1 else None
