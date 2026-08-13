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
