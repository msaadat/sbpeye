import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import requests
from openai import APIError, BadRequestError, OpenAI

from sqlalchemy.orm import Session

from .checklist import compact_required_checklist
from .env import load_app_env, resolve_env_value


load_app_env()


TAG_TAXONOMY = [
    "AML",
    "CFT",
    "KYC",
    "CDD",
    "EDD",
    "Sanctions",
    "Compliance",
    "Forex",
    "Remittance",
    "Exchange Rate",
    "Export",
    "Import",
    "Trade Finance",
    "LC",
    "Guarantees",
    "Prudential",
    "Capital Adequacy",
    "Liquidity",
    "Risk Management",
    "Corporate Governance",
    "Payment Systems",
    "Digital Banking",
    "RAAST",
    "RTGS",
    "Card Operations",
    "Consumer Protection",
    "Microfinance",
    "Islamic Banking",
    "Sukuk",
    "Reporting",
    "IT",
    "Cybersecurity",
    "Branch Licensing",
    "Penalty",
    "Interest Rate",
    "Monetary Policy",
    "Tax",
    "Housing Finance",
    "SME Finance",
    "Agriculture Credit",
    "Sustainable Finance",
    "Deposit Insurance",
    "Anti-Fraud",
    "Data Privacy",
    "Outsourcing",
    "Internal Audit",
    "Credit Risk",
    "Market Risk",
    "Operational Risk",
    "Treasury",
]


# Human-readable activity labels for the chat status stream. Keys must match the
# tool function names declared in ``TOOLS`` below.
TOOL_LABELS = {
    "search_selected_documents": "Searching selected documents",
    "search_circulars": "Searching circulars",
    "get_latest_circulars": "Fetching latest circulars",
    "get_circular_details": "Reading circular details",
    "query_regulatory_values": "Looking up regulatory values",
    "get_circulars_by_tag": "Browsing circulars by tag",
}


def tool_activity_label(name: str) -> str:
    """Friendly label for a tool call, falling back to a humanized name."""
    return TOOL_LABELS.get(name, name.replace("_", " ").strip().capitalize())


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_selected_documents",
            "description": "Search passages within the circulars currently selected for this chat, including their attachments. Use this to inspect full text, find exact requirements, or retrieve additional passages. The server enforces the selected-document scope.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A focused question or search phrase for the selected documents"},
                    "limit": {"type": "integer", "description": "Number of passages to return (1-10)", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_circulars",
            "description": "Search SBP circulars by keyword, topic, department, or tag. Use this when the user asks for circulars on a specific subject, regulation, or topic. Returns matching circulars with title, date, department, reference, and summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms. Examples: 'TT remittance', 'foreign exchange rules', 'AML guidelines', 'KYC requirements'"},
                    "department": {"type": "string", "description": "Optional department name to filter by, e.g. 'BPRD', 'Exchange Policy'"},
                    "tag": {"type": "string", "description": "Optional tag to filter by, e.g. 'Remittance', 'Forex', 'AML'"},
                    "limit": {"type": "integer", "description": "Max results to return (1-50)", "default": 10}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_circulars",
            "description": "Retrieve the most recent circulars from the database, optionally filtered by department or topic. Use this when the user asks for the latest or most recent circulars.",
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {"type": "string", "description": "Optional department name to filter by"},
                    "limit": {"type": "integer", "description": "Number of circulars to return (1-20)", "default": 5}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_circular_details",
            "description": "Fetch the full details of a specific circular by its reference number or title. Use this when the user refers to a specific circular by reference (e.g. 'BPRD Circular No. 12 of 2023') or when you need the complete content of a circular found in a search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "circular_reference": {"type": "string", "description": "The circular's reference number, e.g. 'BPRD Circular No. 12 of 2023' or title"}
                },
                "required": ["circular_reference"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_regulatory_values",
            "description": "Query the structured database of regulatory VALUES extracted from circulars — ratios (CAR, LCR, NSFR, Leverage Ratio), monetary thresholds (minimum paid-up capital, MCR, exposure limits), percentage limits, numeric limits, and deadlines. Use this for any quantitative question, e.g. 'what is the current minimum capital requirement for MFBs?', 'which circulars set a threshold above 10%?', 'what is the required CAR?'. Returns each value with its metric, normalized number, unit, comparator (min/max/exactly), subject it applies to, effective date, and a citation to the source circular.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "Metric name to match, e.g. 'CAR', 'LCR', 'Paid-up Capital', 'MCR' (substring match)"},
                    "subject": {"type": "string", "description": "Who the value applies to, e.g. 'MFB', 'locally incorporated banks' (substring match)"},
                    "entity_type": {"type": "string", "description": "Optional: ratio | monetary_threshold | percentage_limit | numeric_limit | deadline | effective_date"},
                    "unit": {"type": "string", "description": "Optional unit filter: '%', 'PKR', 'USD', 'times', 'days', 'months'"},
                    "comparator": {"type": "string", "description": "Optional: min, max, exactly, or range"},
                    "min_value": {"type": "number", "description": "Only return values whose normalized number is >= this (e.g. 10 with unit '%' for 'above 10%')"},
                    "max_value": {"type": "number", "description": "Only return values whose normalized number is <= this"},
                    "current_only": {"type": "boolean", "description": "If true, exclude superseded/cancelled circulars and keep only the latest value per metric+subject. Use for 'current' value questions."},
                    "limit": {"type": "integer", "description": "Max results (1-50)", "default": 20}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_circulars_by_tag",
            "description": "Retrieve all circulars that have a specific AI-generated tag. Use this when the user asks for circulars categorized under a specific topic like 'AML', 'Remittance', 'Forex', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "The tag name, e.g. 'AML', 'Remittance', 'Forex', 'Trade Finance'"},
                    "limit": {"type": "integer", "description": "Number of circulars to return (1-50)", "default": 10}
                },
                "required": ["tag"]
            }
        }
    }
]


@dataclass(frozen=True)
class ProviderDefinition:
    value: str
    label: str
    default_base_url: str
    api_key_env_vars: tuple[str, ...]
    default_model: str = "local-model"
    default_api_key: str = ""


PROVIDER_DEFINITIONS = {
    "lmstudio": ProviderDefinition(
        value="lmstudio",
        label="LM Studio (Local)",
        default_base_url="http://localhost:1234/v1",
        api_key_env_vars=("AI_API_KEY",),
        default_model="local-model",
        default_api_key="lm-studio",
    ),
    "openai": ProviderDefinition(
        value="openai",
        label="OpenAI",
        default_base_url="https://api.openai.com/v1",
        api_key_env_vars=("OPENAI_API_KEY", "AI_API_KEY"),
        default_model="gpt-4o-mini",
    ),
    "google": ProviderDefinition(
        value="google",
        label="Google Gemini",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY", "AI_API_KEY"),
        default_model="gemini-2.0-flash",
    ),
    "ollama": ProviderDefinition(
        value="ollama",
        label="Ollama Cloud",
        default_base_url="https://ollama.com/api",
        api_key_env_vars=("OLLAMA_API_KEY", "AI_API_KEY"),
        default_model="gpt-oss:120b",
    ),
    "mistral": ProviderDefinition(
        value="mistral",
        label="Mistral AI",
        default_base_url="https://api.mistral.ai/v1",
        api_key_env_vars=("MISTRAL_API_KEY", "AI_API_KEY"),
        default_model="mistral-small-latest",
    ),
    "groq": ProviderDefinition(
        value="groq",
        label="Groq",
        default_base_url="https://api.groq.com/openai/v1",
        api_key_env_vars=("GROQ_API_KEY", "AI_API_KEY"),
        default_model="llama-3.1-8b-instant",
    ),
    "openrouter": ProviderDefinition(
        value="openrouter",
        label="OpenRouter",
        default_base_url="https://openrouter.ai/api/v1",
        api_key_env_vars=("OPENROUTER_API_KEY", "AI_API_KEY"),
        default_model="openai/gpt-4o-mini",
    ),
    "custom": ProviderDefinition(
        value="custom",
        label="Custom OpenAI-Compatible",
        default_base_url="http://localhost:1234/v1",
        api_key_env_vars=("AI_API_KEY",),
    ),
}


def normalize_provider(provider: str | None) -> str:
    value = (provider or "lmstudio").strip().lower()
    aliases = {
        "gemini": "google",
        "lm_studio": "lmstudio",
        "mistralai": "mistral",
        "mistral_ai": "mistral",
        "ollama_cloud": "ollama",
    }
    return aliases.get(value, value if value in PROVIDER_DEFINITIONS else "custom")


def get_provider_definition(provider: str | None) -> ProviderDefinition:
    return PROVIDER_DEFINITIONS[normalize_provider(provider)]


GENERIC_CHAT_ERROR = (
    "Sorry, something went wrong while generating a response. Please try again."
)


def friendly_chat_error(exc: Exception) -> str:
    """Translate a provider/SDK exception into a clear, user-facing message.

    Always returns a non-empty string so callers can surface it directly. Raw
    provider payloads (status codes, JSON dumps, stack traces) are never exposed.
    """
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()

    def has(*needles: str) -> bool:
        return any(needle in text for needle in needles)

    # Request too large / rate limited / context window exceeded.
    if status in (413, 429) or has(
        "rate_limit_exceeded",
        "request too large",
        "tokens per minute",
        "requests per minute",
        "context_length_exceeded",
        "maximum context length",
        "reduce your message size",
    ):
        return (
            "This request was too large for the selected model. The provider "
            "rejected it because the conversation plus the selected circulars' "
            "context exceeded its token/rate limit. Try selecting fewer "
            "circulars (or ones with smaller attachments), or ask a more "
            "specific question."
        )

    # Authentication / authorization problems.
    if status in (401, 403) or has(
        "invalid api key",
        "incorrect api key",
        "authentication",
        "unauthorized",
        "permission",
    ):
        return (
            "The AI provider rejected the request due to an authentication "
            "problem. Check that a valid API key is configured for the selected "
            "provider in Settings."
        )

    # Model not found / not available.
    if status == 404 or has(
        "model not found",
        "does not exist",
        "no such model",
        "model_not_found",
    ):
        return (
            "The configured chat model could not be found at the AI provider. "
            "Verify the model name in Settings."
        )

    # Network / connection / timeout issues reaching the provider.
    if exc.__class__.__name__ in {"APIConnectionError", "APITimeoutError"} or has(
        "connection error",
        "connection refused",
        "timed out",
        "timeout",
        "failed to establish",
        "name resolution",
        "max retries",
    ):
        return (
            "Could not reach the AI provider. Check your network connection and "
            "that the provider's base URL is correct in Settings, then try again."
        )

    # Provider-side server errors.
    if (isinstance(status, int) and status >= 500) or has(
        "internal server error",
        "service unavailable",
        "bad gateway",
        "overloaded",
    ):
        return (
            "The AI provider is temporarily unavailable or overloaded. Please "
            "wait a moment and try again."
        )

    return GENERIC_CHAT_ERROR


def classify_provider_state(exc: Exception) -> tuple[str, str]:
    """Map a provider/SDK exception to a coarse availability state and short detail.

    States: ``rate_limited``, ``auth_error``, ``not_found``, ``offline``,
    ``server_error``, ``error``. The detail is a short, user-facing phrase.
    """
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()

    def has(*needles: str) -> bool:
        return any(needle in text for needle in needles)

    if status in (413, 429) or has(
        "rate_limit", "rate limit", "too many requests",
        "tokens per minute", "requests per minute", "quota",
    ):
        return "rate_limited", "Rate limited or quota exceeded"

    if status in (401, 403) or has(
        "invalid api key", "incorrect api key", "authentication",
        "unauthorized", "permission",
    ):
        return "auth_error", "Authentication failed — check API key"

    if status == 404 or has("model not found", "model_not_found", "no such model"):
        return "not_found", "Configured model not found"

    if exc.__class__.__name__ in {"APIConnectionError", "APITimeoutError"} or has(
        "connection error", "connection refused", "timed out", "timeout",
        "failed to establish", "name resolution", "max retries",
    ):
        return "offline", "Provider unreachable"

    if (isinstance(status, int) and status >= 500) or has(
        "internal server error", "service unavailable", "bad gateway", "overloaded",
    ):
        return "server_error", "Provider temporarily unavailable"

    return "error", "Provider check failed"


def is_rate_limit_error(exc: Exception) -> bool:
    """True if the exception is a provider 429 / rate-limit / quota rejection.

    The CLI uses this to abort batch LLM operations on the first 429 instead of
    sending the rest of the batch into the same limit. Handles both SDK errors
    (``status_code`` attribute) and ``requests`` HTTP errors (status on
    ``response``).
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "rate_limit", "rate limit", "too many requests",
            "requests per minute", "tokens per minute", "quota",
        )
    )


def get_provider_api_key(provider: str | None) -> tuple[str, str | None]:
    definition = get_provider_definition(provider)
    return resolve_env_value(
        *definition.api_key_env_vars,
        default=definition.default_api_key,
    )


class OllamaCloudClient:
    """Minimal OpenAI-shaped adapter for Ollama's native cloud API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat_completion_create))
        self.models = SimpleNamespace(list=self._models_list)

    def with_options(
        self,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> "OllamaCloudClient":
        return OllamaCloudClient(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout if timeout is None else timeout,
            max_retries=self.max_retries if max_retries is None else max_retries,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers(),
                    timeout=self.timeout,
                    **kwargs,
                )
                response.raise_for_status()
                return response
            except Exception as exc:
                last_exc = exc
                # Stop immediately on a 429 (rate exceeded) — retrying just walks
                # further into the provider's rate limit.
                if attempt >= self.max_retries or is_rate_limit_error(exc):
                    break
                time.sleep(0.25 * (attempt + 1))
        raise last_exc or RuntimeError("Ollama Cloud request failed")

    @staticmethod
    def _tool_call_to_namespace(tool_call: dict[str, Any], index: int = 0) -> SimpleNamespace:
        function = tool_call.get("function") or {}
        arguments = function.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        return SimpleNamespace(
            id=tool_call.get("id") or f"call_{index}",
            index=index,
            type=tool_call.get("type") or "function",
            function=SimpleNamespace(
                name=function.get("name") or "",
                arguments=arguments,
            ),
        )

    @classmethod
    def _message_to_namespace(cls, message: dict[str, Any]) -> SimpleNamespace:
        tool_calls = message.get("tool_calls") or []
        return SimpleNamespace(
            content=message.get("content") or "",
            tool_calls=[
                cls._tool_call_to_namespace(tool_call, index)
                for index, tool_call in enumerate(tool_calls)
            ],
        )

    @classmethod
    def _chunk_to_namespace(cls, message: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=cls._message_to_namespace(message),
                )
            ]
        )

    @staticmethod
    def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for message in messages:
            item = dict(message)
            if isinstance(item.get("tool_calls"), list):
                converted = []
                for tool_call in item["tool_calls"]:
                    converted_call = dict(tool_call)
                    function = dict(converted_call.get("function") or {})
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            function["arguments"] = json.loads(arguments)
                        except json.JSONDecodeError:
                            function["arguments"] = {}
                    converted_call["function"] = function
                    converted.append(converted_call)
                item["tool_calls"] = converted
            normalized.append(item)
        return normalized

    @staticmethod
    def _response_format(format_value: dict[str, Any] | None) -> Any:
        if not format_value:
            return None
        if format_value.get("type") == "json_schema":
            return (format_value.get("json_schema") or {}).get("schema")
        if format_value.get("type") == "json_object":
            return "json"
        return None

    def _chat_payload(self, **kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": kwargs["model"],
            "messages": self._normalize_messages(kwargs.get("messages", [])),
            "stream": bool(kwargs.get("stream", False)),
        }
        options: dict[str, Any] = {}
        if "temperature" in kwargs and kwargs["temperature"] is not None:
            options["temperature"] = kwargs["temperature"]
        if "max_tokens" in kwargs and kwargs["max_tokens"] is not None:
            options["num_predict"] = kwargs["max_tokens"]
        if options:
            payload["options"] = options
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]
        response_format = self._response_format(kwargs.get("response_format"))
        if response_format:
            payload["format"] = response_format
        return payload

    def _chat_completion_create(self, **kwargs: Any) -> Any:
        payload = self._chat_payload(**kwargs)
        response = self._request("POST", "/chat", json=payload, stream=payload["stream"])
        if payload["stream"]:
            return self._stream_chat(response)
        body = response.json()
        message = self._message_to_namespace(body.get("message") or {})
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def _stream_chat(self, response: requests.Response):
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            body = json.loads(line)
            yield self._chunk_to_namespace(body.get("message") or {})

    def _models_list(self) -> SimpleNamespace:
        response = self._request("GET", "/tags")
        body = response.json()
        models = [
            SimpleNamespace(
                id=model.get("model") or model.get("name") or "",
                model=model.get("model") or model.get("name") or "",
                name=model.get("name") or model.get("model") or "",
            )
            for model in body.get("models", [])
        ]
        return SimpleNamespace(data=models)

@dataclass
class AIConfig:
    provider: str = "lmstudio"
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"
    model: str = "local-model"
    chat_model: str = ""
    max_context_tokens: int = 4000

    @property
    def effective_chat_model(self) -> str:
        return self.chat_model or self.model

    @staticmethod
    def from_env() -> "AIConfig":
        provider = normalize_provider(os.getenv("AI_PROVIDER", "lmstudio"))
        definition = get_provider_definition(provider)
        api_key, _ = get_provider_api_key(provider)
        return AIConfig(
            provider=provider,
            base_url=os.getenv("AI_BASE_URL", definition.default_base_url),
            api_key=api_key,
            model=os.getenv("AI_MODEL", definition.default_model),
            chat_model=os.getenv("AI_CHAT_MODEL", ""),
            max_context_tokens=int(os.getenv("AI_MAX_CONTEXT_TOKENS", "4000")),
        )

    @staticmethod
    def from_db(db) -> "AIConfig | None":
        try:
            from .models import Settings
            rows = db.query(Settings).all()
            if not rows:
                return None
            kv = {r.key: r.value for r in rows}
            if "ai_provider" not in kv:
                return None
            provider = normalize_provider(kv.get("ai_provider", "lmstudio"))
            env_config = AIConfig.from_env()
            api_key, _ = get_provider_api_key(provider)
            return AIConfig(
                provider=provider,
                base_url=kv.get("ai_base_url", get_provider_definition(provider).default_base_url),
                api_key=api_key,
                model=kv.get("ai_model", env_config.model),
                chat_model=kv.get("ai_chat_model", ""),
                max_context_tokens=int(kv.get("ai_max_context_tokens", str(env_config.max_context_tokens))),
            )
        except Exception:
            return None

    def save_to_db(self, db):
        from .models import upsert_settings
        upsert_settings(db, {
            "ai_provider": normalize_provider(self.provider),
            "ai_base_url": self.base_url,
            "ai_model": self.model,
            "ai_chat_model": self.chat_model,
            "ai_max_context_tokens": str(self.max_context_tokens),
        })

    @classmethod
    def secret_state(cls, provider: str | None) -> dict[str, str | bool]:
        normalized = normalize_provider(provider)
        definition = get_provider_definition(normalized)
        api_key, env_var = get_provider_api_key(normalized)
        return {
            "provider": normalized,
            "api_key_configured": bool(env_var and api_key),
            "api_key_env_var": env_var or definition.api_key_env_vars[0],
        }


class AIClient:
    def __init__(self, config: AIConfig | None = None):
        if config is None:
            config = AIConfig.from_env()
        self.config = config
        self._client = self._create_client()

    def _create_client(self) -> Any:
        if self.config.provider == "ollama":
            return OllamaCloudClient(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
            )
        kwargs: dict[str, Any] = {
            "base_url": self.config.base_url,
            "api_key": self.config.api_key,
        }
        if self.config.provider == "openrouter":
            kwargs["default_headers"] = {"X-Title": "SBPEye"}
        return OpenAI(**kwargs)

    @staticmethod
    def _model_metadata(model: Any) -> dict[str, Any]:
        if isinstance(model, dict):
            return model
        if hasattr(model, "model_dump"):
            return model.model_dump()
        return {}

    def detect_context_window(self) -> int | None:
        """Return the smallest provider-reported window used by this config."""
        if self.config.provider in {"openai", "google"}:
            return None
        try:
            response = self._client.with_options(timeout=5.0, max_retries=0).models.list()
        except Exception:
            return None

        windows: dict[str, int] = {}
        for model in response.data:
            metadata = self._model_metadata(model)
            model_id = str(metadata.get("id") or getattr(model, "id", ""))
            for key in ("context_window", "context_length", "max_context_length"):
                value = metadata.get(key)
                if isinstance(value, int) and value > 0:
                    windows[model_id] = value
                    break

        model_ids = {self.config.model, self.config.effective_chat_model}
        detected = [windows.get(model_id) for model_id in model_ids]
        if any(value is None for value in detected):
            return None
        return min(value for value in detected if value is not None)

    def list_models(self) -> list[dict[str, str]]:
        """Return provider model IDs in a normalized shape for the settings UI."""
        response = self._client.with_options(timeout=10.0, max_retries=0).models.list()
        models: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in response.data:
            metadata = self._model_metadata(item)
            model_id = str(
                metadata.get("id")
                or metadata.get("model")
                or metadata.get("name")
                or getattr(item, "id", "")
                or getattr(item, "model", "")
                or getattr(item, "name", "")
            ).strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            label = str(
                metadata.get("name")
                or metadata.get("display_name")
                or getattr(item, "name", "")
                or model_id
            ).strip()
            models.append({"id": model_id, "name": label or model_id})
        return sorted(models, key=lambda model: model["id"].lower())

    def _complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = "",
        temperature: float = 0.0,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        model = model or self.config.model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        if json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "sbpeye_response",
                    "strict": True,
                    "schema": json_schema,
                },
            }
        try:
            response = self._client.chat.completions.create(**kwargs)
        except BadRequestError as exc:
            if not json_schema or "response_format" not in str(exc):
                raise
            # Some OpenAI-compatible local servers only accept text responses.
            kwargs["response_format"] = {"type": "text"}
            response = self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    def _complete_chat(self, messages: list[dict[str, str]], model: str = "", temperature: float = 0.3) -> str:
        model = model or self.config.effective_chat_model
        response = self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    @staticmethod
    def _is_tool_choice_none_error(exc: Exception) -> bool:
        return "Tool choice is none, but model called a tool" in str(exc)

    @staticmethod
    def _tool_iteration_limit_message() -> str:
        return (
            "I could not complete the answer because the model kept requesting "
            "additional database tool calls after the lookup limit was reached. "
            "Please retry the question, or narrow it to the selected circulars."
        )

    def _tool_result_synthesis_messages(
        self,
        messages: list[dict[str, str]],
        full_messages: list[dict[str, Any]],
        circulars_context: str | None,
    ) -> list[dict[str, str]]:
        tool_sections: list[str] = []
        remaining_chars = max(4000, self.config.max_context_tokens * 4)
        for item in full_messages:
            if item.get("role") != "tool":
                continue
            content = str(item.get("content") or "")
            if not content:
                continue
            if len(content) > remaining_chars:
                content = content[:remaining_chars]
            tool_sections.append(content)
            remaining_chars -= len(content)
            if remaining_chars <= 0:
                break

        synthesis_context = [
            "Selected circular context:",
            circulars_context or "No selected circular context was provided.",
        ]
        if tool_sections:
            synthesis_context.extend([
                "Database lookup results already gathered:",
                "\n\n".join(tool_sections),
            ])

        return [
            {
                "role": "system",
                "content": (
                    "You are an expert assistant for SBP circulars and regulations. "
                    "No tools are available in this step. Answer using only the "
                    "provided selected context and database lookup results. Preserve "
                    "any exact citation tokens you use."
                ),
            },
            *messages,
            {"role": "user", "content": "\n\n".join(synthesis_context)},
        ]

    def _truncate_context(self, content_text: str) -> str:
        """Clip document text to the configured context budget before prompting."""
        limit = self.config.max_context_tokens
        return content_text[:limit] if len(content_text) > limit else content_text

    def summarize(self, title: str, content_text: str) -> str:
        system = "You are a concise financial regulations analyst. Summarize the following SBP circular in 3-5 sentences, focusing on the key regulatory changes, requirements, and impact on banks/DFIs/MFBs. Be factual and specific."
        truncated = self._truncate_context(content_text)
        user = f"Title: {title}\n\nContent:\n{truncated}"
        result = self._complete(system, user, temperature=0.2)
        return result.strip()

    def generate_tags(self, title: str, content_text: str) -> list[str]:
        system = f"You are a financial regulations classifier. Select the most relevant tags from the following taxonomy that apply to the given SBP circular.\n\nTaxonomy: {json.dumps(TAG_TAXONOMY)}\n\nReturn ONLY a JSON object with a 'tags' key containing a list of 1-5 selected tag strings from the taxonomy."
        truncated = self._truncate_context(content_text)
        user = f"Title: {title}\n\nContent:\n{truncated}"
        result = self._complete(
            system,
            user,
            temperature=0.0,
            json_schema={
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "enum": TAG_TAXONOMY},
                        "maxItems": 5,
                    },
                },
                "required": ["tags"],
                "additionalProperties": False,
            },
        )
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError("The model returned invalid JSON for tags.") from exc
        tags = parsed.get("tags") if isinstance(parsed, dict) else None
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("The model returned an invalid tags payload.")
        valid_tags = [tag for tag in tags if tag in TAG_TAXONOMY]
        if not valid_tags:
            valid_tags = tags[:5]
        return valid_tags[:5]

    @staticmethod
    def _response_excerpt(result: str, limit: int = 300) -> str:
        compact = re.sub(r"\s+", " ", result or "").strip()
        return compact[:limit] + ("..." if len(compact) > limit else "")

    @staticmethod
    def _parse_json_object(result: str) -> dict[str, Any]:
        text = (result or "").strip()
        if not text:
            raise ValueError("The model returned an empty checklist response.")

        candidates = [text]
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced:
            candidates.insert(0, fenced.group(1).strip())
        decoder = json.JSONDecoder()
        for start, char in enumerate(text):
            if char not in "[{":
                continue
            try:
                _, end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            candidates.append(text[start:start + end])
            break

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"items": parsed}
        raise ValueError("The model returned an invalid checklist JSON payload.")

    @staticmethod
    def _checklist_extraction_schema() -> dict[str, Any]:
        string_field = {"type": "string"}
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "requirement": string_field,
                            "classification": {
                                "type": "string",
                                "enum": ["required", "optional"],
                            },
                            "actor": string_field,
                            "action": string_field,
                            "object": string_field,
                            "conditions": string_field,
                            "deadline": string_field,
                            "evidence": string_field,
                            "applicability": string_field,
                            "source_unit_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                        },
                        "required": ["requirement", "classification", "source_unit_ids"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        }

    @classmethod
    def _parse_checklist_items(
        cls,
        result: str,
        valid_source_ids: set[str],
    ) -> list[dict[str, Any]]:
        parsed = cls._parse_json_object(result)
        entries = parsed.get("items", parsed.get("checklist_items", parsed.get("checklist")))
        if not isinstance(entries, list):
            raise ValueError("The model returned a checklist payload without an items array.")

        normalized: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("The model returned a non-object checklist item.")
            requirement = entry.get("requirement", entry.get("item"))
            if not isinstance(requirement, str) or len(requirement.strip()) < 8:
                raise ValueError("The model returned a checklist item without a usable requirement.")
            classification = entry.get("classification")
            if classification is None and isinstance(entry.get("action_required"), bool):
                classification = "required" if entry["action_required"] else "optional"
            classification = str(classification or "").strip().lower()
            if classification not in {"required", "optional"}:
                raise ValueError("The model returned an invalid checklist classification.")

            source_ids = entry.get("source_unit_ids", entry.get("source_ids", []))
            if isinstance(source_ids, str):
                source_ids = [source_ids]
            if not isinstance(source_ids, list):
                raise ValueError("The model returned invalid source citations.")
            citations_provided = bool(source_ids)
            cited = list(dict.fromkeys(
                str(source_id) for source_id in source_ids
                if str(source_id) in valid_source_ids
            ))
            if not cited and not citations_provided and len(valid_source_ids) == 1:
                cited = list(valid_source_ids)
            if not cited:
                raise ValueError("The model returned a checklist item without a valid source citation.")

            item = {
                "requirement": re.sub(r"\s+", " ", requirement).strip(),
                "classification": classification,
                "source_unit_ids": cited,
            }
            for field_name in (
                "actor", "action", "object", "conditions", "deadline", "evidence", "applicability"
            ):
                value = entry.get(field_name, "")
                item[field_name] = re.sub(r"\s+", " ", str(value or "")).strip()
            normalized.append(item)
        return normalized

    def _extract_checklist_block(
        self,
        *,
        circular_label: str,
        block,
        trace_callback=None,
    ) -> list[dict[str, Any]]:
        system = """You are a conservative SBP regulatory compliance analyst. Extract actionable compliance requirements from exactly one complete SOURCE BLOCK.

Include explicit duties, prohibitions, eligibility conditions, controls, deadlines, recordkeeping, submission requirements, required evidence, and explicit permissions or recommendations. Exclude headings, definitions without an obligation, explanatory narrative, addresses, greetings, signature labels, blank form fields, empty table cells, and formatting fragments. For forms and tables, extract only substantive required fields, attestations, evidence, submission actions, or format constraints; do not turn individual words or decorative labels into items. Combine an introductory clause with its dependent list when they form one obligation, but keep genuinely separate actions separate. Use "required" for duties, prohibitions, conditions, and mandatory evidence; use "optional" only for explicit permissions or recommendations. Populate actor, action, object, conditions, deadline, evidence, and applicability when present, using an empty string when a field is absent. Cite one or more SOURCE_ID values exactly as supplied. If the block contains no actionable requirement, return {"items":[]}. Return only the JSON object."""
        user = f"""Circular: {circular_label}
Document: {block.doc_label}
Block reference: {block.ref}
Block type: {block.block_type}
Pages: {block.page_start or 'HTML'}-{block.page_end or block.page_start or 'HTML'}

SOURCE BLOCK:
{block.source_text}"""
        if trace_callback:
            trace_callback("llm_input", {
                "block": block,
                "system_prompt": system,
                "user_prompt": user,
            })
        result = self._complete(
            system,
            user,
            temperature=0.0,
            json_schema=self._checklist_extraction_schema(),
        )
        if trace_callback:
            trace_callback("llm_output", {"block": block, "raw_response": result})
        valid_source_ids = set(block.source_unit_ids)
        try:
            return self._parse_checklist_items(result, valid_source_ids)
        except ValueError:
            retry_system = (
                system
                + "\nYour previous response was malformed. Return the schema-compliant JSON object only, with valid SOURCE_ID citations."
            )
            retry_result = self._complete(
                retry_system,
                user,
                temperature=0.0,
                json_schema=self._checklist_extraction_schema(),
            )
            if trace_callback:
                trace_callback("llm_output", {
                    "block": block,
                    "raw_response": retry_result,
                    "attempt": 2,
                })
            try:
                return self._parse_checklist_items(retry_result, valid_source_ids)
            except ValueError as exc:
                first = self._response_excerpt(result)
                second = self._response_excerpt(retry_result)
                raise ValueError(
                    "The model returned an invalid checklist response after retry. "
                    f"First: {first!r}; retry: {second!r}"
                ) from exc

    @staticmethod
    def _materialize_checklist_item(entry, block, units_by_id) -> dict[str, Any]:
        source_units = [units_by_id[source_id] for source_id in entry["source_unit_ids"]]
        refs = list(dict.fromkeys(unit.ref for unit in source_units))
        pages = [
            page
            for unit in source_units
            for page in (unit.page_start, unit.page_end)
            if page is not None
        ]
        digest = hashlib.sha256(
            f"{block.doc_id}\0{entry['requirement']}\0{'|'.join(entry['source_unit_ids'])}".encode("utf-8")
        ).hexdigest()[:20]
        source_text = "\n\n".join(unit.source_text for unit in source_units)
        requirement_tokens = set(re.findall(r"[a-z0-9]+", entry["requirement"].casefold()))
        candidates = [
            re.sub(r"\s+", " ", candidate).strip(" -*|\n")
            for candidate in re.split(r"(?<=[.;:])\s+|\n+", source_text)
        ]
        candidates = [candidate for candidate in candidates if candidate]
        source_excerpt = max(
            candidates,
            key=lambda candidate: len(
                requirement_tokens
                & set(re.findall(r"[a-z0-9]+", candidate.casefold()))
            ),
            default=source_text,
        )
        if len(source_excerpt) > 900:
            source_excerpt = source_excerpt[:897].rstrip() + "..."
        return {
            "item_id": f"checklist:{digest}",
            "requirement": entry["requirement"],
            "classification": entry["classification"],
            "actor": entry["actor"],
            "action": entry["action"],
            "object": entry["object"],
            "conditions": entry["conditions"],
            "deadline": entry["deadline"],
            "evidence": entry["evidence"],
            "applicability": entry["applicability"],
            "ref": "; ".join(refs),
            "source_refs": refs,
            "source_unit_ids": entry["source_unit_ids"],
            "source_text": source_excerpt,
            "doc_id": block.doc_id,
            "doc_type": block.doc_type,
            "doc_label": block.doc_label,
            "page_start": min(pages) if pages else None,
            "page_end": max(pages) if pages else None,
        }

    @staticmethod
    def _deduplicate_checklist_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduplicated: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}
        for item in items:
            semantic_fields = [
                item.get("actor"), item.get("action"), item.get("object"),
                item.get("conditions"), item.get("deadline"), item.get("applicability"),
            ]
            key_text = " | ".join(str(value or "") for value in semantic_fields)
            if not item.get("action") or not item.get("object"):
                key_text = str(item.get("requirement") or "")
            key = re.sub(r"[^a-z0-9]+", " ", key_text.casefold()).strip()
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = item
                deduplicated.append(item)
                continue
            existing["classification"] = (
                "required"
                if "required" in {existing.get("classification"), item.get("classification")}
                else "optional"
            )
            for list_field in ("source_refs", "source_unit_ids"):
                existing[list_field] = list(dict.fromkeys([
                    *existing.get(list_field, []), *item.get(list_field, [])
                ]))
            existing["ref"] = "; ".join(existing["source_refs"])
            merged_source_text = "\n\n".join(dict.fromkeys([
                existing.get("source_text", ""), item.get("source_text", "")
            ]))
            existing["source_text"] = (
                merged_source_text
                if len(merged_source_text) <= 900
                else merged_source_text[:897].rstrip() + "..."
            )
            pages = [
                page for page in (
                    existing.get("page_start"), existing.get("page_end"),
                    item.get("page_start"), item.get("page_end"),
                ) if page is not None
            ]
            if pages:
                existing["page_start"] = min(pages)
                existing["page_end"] = max(pages)
        return deduplicated

    def generate_checklist(
        self,
        circular,
        *,
        delay: float = 0.0,
        progress_callback=None,
        trace_callback=None,
        documents: list[dict[str, Any]] | None = None,
        gaps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from .checklist import build_analysis_blocks, build_checklist_corpus, segment_document

        if documents is None:
            documents, discovered_gaps = build_checklist_corpus(circular)
            gaps = discovered_gaps if gaps is None else list(gaps)
        else:
            documents = list(documents)
            gaps = list(gaps or [])
        if trace_callback:
            for document in documents:
                trace_callback("document", {"document": document})
        document_units = []
        failed_document_ids: set[str] = set()
        for document in documents:
            try:
                units = segment_document(document)
            except Exception as exc:
                units = []
                failed_document_ids.add(document["doc_id"])
                gaps.append({
                    "doc_id": document["doc_id"],
                    "doc_type": document["doc_type"],
                    "doc_label": document["doc_label"],
                    "reason": "docling_conversion_error",
                    "error": str(exc),
                })
            document_units.append((document, units))
        if trace_callback:
            for document, units in document_units:
                trace_callback("parsing", {"document": document, "units": units})
        for document, units in document_units:
            if not units and document["doc_id"] not in failed_document_ids:
                gaps.append({
                    "doc_id": document["doc_id"],
                    "doc_type": document["doc_type"],
                    "doc_label": document["doc_label"],
                    "reason": "no_items",
                })
        document_blocks = [
            (document, units, build_analysis_blocks(units))
            for document, units in document_units
        ]
        if trace_callback:
            for document, _, blocks in document_blocks:
                trace_callback("analysis_blocks", {"document": document, "blocks": blocks})
        total_blocks = sum(len(blocks) for _, _, blocks in document_blocks)
        completed = 0
        checklist_items: list[dict[str, Any]] = []
        all_units = [unit for _, units in document_units for unit in units]
        units_by_id = {unit.unit_id: unit for unit in all_units}
        circular_label = circular.reference or circular.title
        if progress_callback:
            progress_callback(0, total_blocks)

        for _, _, blocks in document_blocks:
            for block in blocks:
                try:
                    extracted = self._extract_checklist_block(
                        circular_label=circular_label,
                        block=block,
                        trace_callback=trace_callback,
                    )
                    materialized = [
                        self._materialize_checklist_item(entry, block, units_by_id)
                        for entry in extracted
                    ]
                    checklist_items.extend(materialized)
                except ValueError as exc:
                    materialized = []
                    gaps.append({
                        "doc_id": block.doc_id,
                        "doc_type": block.doc_type,
                        "doc_label": block.doc_label,
                        "reason": "checklist_extraction_error",
                        "error": str(exc),
                        "block_id": block.block_id,
                        "ref": block.ref,
                        "page_start": block.page_start,
                        "page_end": block.page_end,
                    })
                if trace_callback:
                    trace_callback("normalized_block", {
                        "block": block,
                        "items": materialized,
                        "completed": completed + 1,
                        "total": total_blocks,
                    })
                completed += 1
                if progress_callback:
                    progress_callback(completed, total_blocks)
                if delay > 0 and completed < total_blocks:
                    time.sleep(delay)

        checklist_items = self._deduplicate_checklist_items(checklist_items)
        return {
            "schema_version": 2,
            "status": "completed_with_gaps" if gaps else "completed",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "coverage_gaps": gaps,
            "checklist_items": checklist_items,
            "source_units": [unit.payload() for unit in all_units],
            "analysis_blocks": [
                {
                    key: value
                    for key, value in block.payload().items()
                    if key != "source_text"
                }
                for _, _, blocks in document_blocks
                for block in blocks
            ],
        }

    @staticmethod
    def _entity_extraction_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity_type": {
                                "type": "string",
                                "enum": [
                                    "ratio",
                                    "monetary_threshold",
                                    "percentage_limit",
                                    "numeric_limit",
                                    "deadline",
                                    "effective_date",
                                ],
                            },
                            "metric": {"type": "string"},
                            "comparator": {
                                "type": ["string", "null"],
                                "enum": ["min", "max", "exactly", "range", None],
                            },
                            "value_numeric": {"type": ["number", "null"]},
                            "value_high": {"type": ["number", "null"]},
                            "unit": {
                                "type": ["string", "null"],
                                "enum": ["%", "PKR", "USD", "times", "days", "months", None],
                            },
                            "value_text": {"type": "string"},
                            "subject": {"type": "string"},
                            "effective_date": {"type": ["string", "null"]},
                            "context_snippet": {"type": "string"},
                            "source_unit_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "confidence": {"type": ["number", "null"]},
                        },
                        "required": [
                            "entity_type", "metric", "comparator", "value_numeric",
                            "value_high", "unit", "value_text", "subject",
                            "effective_date", "context_snippet", "source_unit_ids",
                            "confidence",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["entities"],
            "additionalProperties": False,
        }

    @staticmethod
    def _entity_system_prompt() -> str:
        return """You are a meticulous SBP regulatory data analyst. From exactly one SOURCE BLOCK, extract every specific regulatory VALUE that a bank/DFI/MFB must comply with. Capture:
- ratios named in the text (e.g. CAR/Capital Adequacy Ratio, LCR, NSFR, Leverage Ratio, CCB);
- monetary thresholds (minimum paid-up capital, MCR, exposure/finance limits) in PKR or USD;
- percentage limits (caps, floors, weights, rates);
- other numeric limits (e.g. number of branches, multiples/"times", tenor in days/months);
- deadlines and effective dates attached to a requirement.

For each value output:
- entity_type: one of ratio | monetary_threshold | percentage_limit | numeric_limit | deadline | effective_date.
- metric: the canonical short name of what the value measures (e.g. "CAR", "LCR", "NSFR", "MCR", "Paid-up Capital", "Leverage Ratio", "Exposure Limit"). Use a concise noun phrase if no standard acronym exists.
- comparator: min (for "at least"/"minimum"/"not less than"), max (for "maximum"/"shall not exceed"/"up to"), exactly (a fixed required value), or range; null for pure dates.
- value_numeric: the value NORMALIZED TO BASE UNITS as a plain number. Convert scales: "Rs. 23 billion" -> 23000000000, "Rs 5 crore" -> 50000000, "US$ 300 million" -> 300000000, "8%" -> 8, "1.5 times" -> 1.5. For dates use null.
- value_high: upper bound when comparator is range, else null.
- unit: % | PKR | USD | times | days | months, or null for dates.
- value_text: the value exactly as written in the text (e.g. "Rs. 23 billion", "8%").
- subject: who/what the value applies to (e.g. "locally incorporated banks", "MFBs", "foreign banks with up to 5 branches"); empty string if unspecified.
- effective_date: the date this value takes effect or is due, as ISO YYYY-MM-DD, or null if none. For entity_type deadline/effective_date this is the date itself.
- context_snippet: the sentence/clause the value came from (<= 300 chars).
- source_unit_ids: one or more SOURCE_ID values cited exactly as supplied.
- confidence: 0.0-1.0.

Emit one entry per distinct (metric, subject, value) tuple — phased schedules produce multiple entries. Do NOT extract values from definitions, examples, or narrative that impose no requirement. If the block contains no regulatory value, return {"entities": []}. Return only the JSON object."""

    def _parse_entities(self, result: str, valid_source_ids: set[str]) -> list[dict[str, Any]]:
        parsed = self._parse_json_object(result)
        raw_entities = parsed.get("entities")
        if not isinstance(raw_entities, list):
            raise ValueError("The model returned an invalid entities payload.")
        valid_types = {
            "ratio", "monetary_threshold", "percentage_limit",
            "numeric_limit", "deadline", "effective_date",
        }
        cleaned: list[dict[str, Any]] = []
        for entry in raw_entities:
            if not isinstance(entry, dict):
                continue
            entity_type = str(entry.get("entity_type") or "").strip()
            if entity_type not in valid_types:
                continue
            # Keep the raw citations the model returned; they are resolved against the
            # block's units later (tolerant of full ids or suffix-only ids). A mangled
            # citation must not discard an otherwise-valid extracted value.
            source_ids = [
                str(sid).strip() for sid in (entry.get("source_unit_ids") or []) if str(sid).strip()
            ]
            value_numeric = self._coerce_number(entry.get("value_numeric"))
            value_text = re.sub(r"\s+", " ", str(entry.get("value_text") or "")).strip()
            effective_date = self._coerce_date(entry.get("effective_date"))
            # A ratio/threshold/limit with no usable number carries no queryable value.
            if entity_type in {"ratio", "monetary_threshold", "percentage_limit", "numeric_limit"} and value_numeric is None:
                continue
            if entity_type in {"deadline", "effective_date"}:
                # Models often place the date in value_text rather than effective_date.
                if effective_date is None:
                    effective_date = self._coerce_date(value_text)
                if effective_date is None and not value_text:
                    continue
            cleaned.append({
                "entity_type": entity_type,
                "metric": re.sub(r"\s+", " ", str(entry.get("metric") or "")).strip() or None,
                "comparator": (str(entry.get("comparator")).strip() or None) if entry.get("comparator") else None,
                "value_numeric": value_numeric,
                "value_high": self._coerce_number(entry.get("value_high")),
                "unit": (str(entry.get("unit")).strip() or None) if entry.get("unit") else None,
                "value_text": value_text or None,
                "subject": re.sub(r"\s+", " ", str(entry.get("subject") or "")).strip() or None,
                "effective_date": effective_date,
                "context_snippet": re.sub(r"\s+", " ", str(entry.get("context_snippet") or "")).strip()[:500] or None,
                "source_unit_ids": list(dict.fromkeys(source_ids)),
                "confidence": self._coerce_number(entry.get("confidence")),
            })
        return cleaned

    @staticmethod
    def _coerce_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = re.sub(r"[,\s]", "", value)
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    @staticmethod
    def _coerce_date(value: Any):
        if not value or not isinstance(value, str):
            return None
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text[:10])
        except ValueError:
            return None

    def _extract_entities_block(self, *, circular_label: str, block) -> list[dict[str, Any]]:
        system = self._entity_system_prompt()
        user = f"""Circular: {circular_label}
Document: {block.doc_label}
Block reference: {block.ref}
Pages: {block.page_start or 'HTML'}-{block.page_end or block.page_start or 'HTML'}

SOURCE BLOCK:
{block.source_text}"""
        result = self._complete(
            system, user, temperature=0.0, json_schema=self._entity_extraction_schema()
        )
        valid_source_ids = set(block.source_unit_ids)
        try:
            return self._parse_entities(result, valid_source_ids)
        except ValueError:
            retry_system = (
                system
                + "\nYour previous response was malformed. Return the schema-compliant JSON object only, with valid SOURCE_ID citations."
            )
            retry_result = self._complete(
                retry_system, user, temperature=0.0, json_schema=self._entity_extraction_schema()
            )
            return self._parse_entities(retry_result, valid_source_ids)

    def extract_entities(
        self,
        circular,
        *,
        delay: float = 0.0,
        progress_callback=None,
        documents: list[dict[str, Any]] | None = None,
        gaps: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Extract structured regulatory values from a circular and its PDF attachments.

        Returns one dict per value, with keys matching the CircularEntity columns
        (entity_type, metric, comparator, value_numeric, value_high, unit, value_text,
        subject, effective_date, context_snippet, source_unit_id, page_start, confidence)."""
        from .checklist import build_analysis_blocks, build_checklist_corpus, segment_document

        if documents is None:
            documents, _ = build_checklist_corpus(circular)
        document_units: list[tuple[dict[str, Any], list]] = []
        for document in documents:
            try:
                units = segment_document(document)
            except Exception:
                units = []
            document_units.append((document, units))

        document_blocks = [
            (units, build_analysis_blocks(units)) for _, units in document_units
        ]
        units_by_id = {
            unit.unit_id: unit for _, units in document_units for unit in units
        }
        # Models frequently cite only the hash suffix of a unit_id ("doc:suffix").
        # Map suffixes back so those citations still resolve to a page.
        suffix_to_id: dict[str, str] = {}
        for uid in units_by_id:
            suffix_to_id.setdefault(uid.rsplit(":", 1)[-1], uid)
        total_blocks = sum(len(blocks) for _, blocks in document_blocks)
        completed = 0
        circular_label = circular.reference or circular.title
        if progress_callback:
            progress_callback(0, total_blocks)

        entities: list[dict[str, Any]] = []
        for _, blocks in document_blocks:
            for block in blocks:
                try:
                    extracted = self._extract_entities_block(
                        circular_label=circular_label, block=block
                    )
                    for entry in extracted:
                        resolved_ids = []
                        for sid in entry["source_unit_ids"]:
                            uid = sid if sid in units_by_id else suffix_to_id.get(sid)
                            if uid:
                                resolved_ids.append(uid)
                        source_units = [units_by_id[uid] for uid in resolved_ids]
                        pages = [
                            unit.page_start
                            for unit in source_units
                            if unit.page_start is not None
                        ]
                        entry["source_unit_id"] = resolved_ids[0] if resolved_ids else None
                        entry["page_start"] = min(pages) if pages else None
                        del entry["source_unit_ids"]
                        entities.append(entry)
                except ValueError:
                    pass
                completed += 1
                if progress_callback:
                    progress_callback(completed, total_blocks)
                if delay > 0 and completed < total_blocks:
                    time.sleep(delay)
        return entities

    def extract_relationships(self, title: str, reference: str, content_text: str) -> dict:
        system = (
            "You are a financial regulations analyst. Extract any mentions of this circular relating to "
            "previous circulars — whether it amends, supersedes, cancels, adds to, or clarifies them. Return "
            "ONLY valid JSON with these keys: 'amends' (list of reference strings), 'supersedes' (list), "
            "'cancels' (list), 'adds_to' (list), 'clarifies' (list). Each reference string should be as close "
            "to the original format as possible, e.g. 'BPRD Circular No. 12 of 2023'.\n"
            "Also detect BLANKET supersession: when the circular supersedes, withdraws, repeals, or "
            "consolidates ALL previous/earlier instructions on a subject WITHOUT naming specific circulars "
            "(e.g. 'This will supersede all previous instructions issued on the subject', 'all earlier "
            "instructions stand withdrawn', 'consolidated the existing instructions on the subject'). In that "
            "case set 'supersedes_all_previous' to true and set 'subject' to a short noun phrase naming that "
            "subject (e.g. 'Cash Reserve Requirement'). If there is no such blanket clause, set "
            "'supersedes_all_previous' to false and 'subject' to an empty string."
        )
        truncated = self._truncate_context(content_text)
        user = f"Title: {title}\nReference: {reference}\n\nContent:\n{truncated}"
        relationship_properties = {
            key: {"type": "array", "items": {"type": "string"}}
            for key in ("amends", "supersedes", "cancels", "adds_to", "clarifies")
        }
        relationship_properties["supersedes_all_previous"] = {"type": "boolean"}
        relationship_properties["subject"] = {"type": "string"}
        result = self._complete(
            system,
            user,
            temperature=0.0,
            json_schema={
                "type": "object",
                "properties": relationship_properties,
                "required": list(relationship_properties),
                "additionalProperties": False,
            },
        )
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError("The model returned invalid JSON for relationships.") from exc
        if not isinstance(parsed, dict):
            raise ValueError("The model returned an invalid relationships payload.")
        relationships = {}
        for key in ("amends", "supersedes", "cancels", "adds_to", "clarifies"):
            values = parsed.get(key, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f"The model returned invalid {key} relationships.")
            relationships[key] = values
        relationships["supersedes_all_previous"] = bool(parsed.get("supersedes_all_previous", False))
        subject = parsed.get("subject", "")
        relationships["subject"] = subject.strip() if isinstance(subject, str) else ""
        return relationships

    def select_superseded(
        self,
        current_title: str,
        subject: str,
        candidates: list[dict],
    ) -> list[str]:
        """Given a blanket supersession on `subject`, decide which candidate circulars it covers.

        `candidates` is a list of {"id", "title", "date", "snippet"} dicts. Returns the subset of
        candidate ids that genuinely concern the same subject and are therefore superseded.
        """
        if not candidates:
            return []
        system = (
            "You are a financial regulations analyst. A newer circular supersedes ALL previous "
            "instructions on a stated subject. From the list of older candidate circulars, identify "
            "ONLY those that concern the SAME subject and are therefore superseded. Be strict: include a "
            "candidate only if its title/content clearly addresses the same subject. Exclude circulars on "
            "merely adjacent or broader topics. Return ONLY valid JSON: "
            '{"superseded_ids": [list of candidate id strings]}.'
        )
        lines = [
            f"Superseding circular title: {current_title}",
            f"Subject superseded: {subject}",
            "",
            "Candidates (older circulars):",
        ]
        for candidate in candidates:
            entry = f"- id={candidate['id']} | date={candidate.get('date', '')} | title={candidate['title']}"
            snippet = candidate.get("snippet")
            if snippet:
                entry += f" | snippet={snippet}"
            lines.append(entry)
        valid_ids = [candidate["id"] for candidate in candidates]
        result = self._complete(
            system,
            "\n".join(lines),
            temperature=0.0,
            json_schema={
                "type": "object",
                "properties": {
                    "superseded_ids": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["superseded_ids"],
                "additionalProperties": False,
            },
        )
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError("The model returned invalid JSON for superseded selection.") from exc
        selected = parsed.get("superseded_ids", []) if isinstance(parsed, dict) else []
        if not isinstance(selected, list):
            return []
        allowed = set(valid_ids)
        return [str(item) for item in selected if str(item) in allowed]

    def _execute_tool(
        self,
        name: str,
        arguments: dict,
        db: Session,
        selected_circular_ids: list[str] | None = None,
    ) -> str:
        """Execute a tool by name and return the result as a JSON string.
        IDs are exposed only inside opaque citation tokens that the UI can resolve."""
        try:
            if name == "search_selected_documents":
                from .chat_retrieval import ScopedChatRetriever

                if not selected_circular_ids:
                    return json.dumps({"error": "No circulars are selected for this chat"})
                query = str(arguments.get("query", "")).strip()
                if not query:
                    return json.dumps({"error": "No search query provided"})
                limit = max(1, min(int(arguments.get("limit", 5)), 10))
                retriever = ScopedChatRetriever(db, selected_circular_ids)
                results = retriever.search(
                    query,
                    limit=limit,
                    token_budget=max(1, self.config.max_context_tokens // 4),
                )
                return json.dumps({"results": results, "count": len(results)})

            if name == "search_circulars":
                from .search import search_engine
                from .models import Circular
                query = arguments.get("query", "")
                department = arguments.get("department", "")
                tag = arguments.get("tag", "")
                limit = int(arguments.get("limit", 10))
                results, _ = search_engine.search(
                    query, db, limit=limit,
                    department=department if department else None,
                    tag=tag if tag else None,
                )
                relaxed_department = False
                if not results and department:
                    results, _ = search_engine.search(
                        query, db, limit=limit, tag=tag if tag else None
                    )
                    relaxed_department = bool(results)
                out = []
                for r in results:
                    c = r["circular"]
                    out.append({
                        "title": c.title,
                        "reference": c.reference,
                        "department": c.department,
                        "date": c.date.strftime("%Y-%m-%d") if c.date else None,
                        "summary": c.summary[:500] if c.summary else None,
                        "status": c.status or "active",
                        "tags": json.loads(c.tags) if c.tags else [],
                        "url": c.url,
                        "citation": f"[[circular:{c.id}|{c.display_name}]]",
                    })
                return json.dumps({
                    "results": out,
                    "count": len(out),
                    "department_filter_relaxed": relaxed_department,
                })

            elif name == "get_latest_circulars":
                from .models import Circular
                department = arguments.get("department", "")
                limit = int(arguments.get("limit", 5))
                q = db.query(Circular).order_by(Circular.date.desc())
                if department:
                    q = q.filter(Circular.department.ilike(f"%{department}%"))
                rows = q.limit(limit).all()
                out = []
                for c in rows:
                    out.append({
                        "title": c.title,
                        "reference": c.reference,
                        "department": c.department,
                        "date": c.date.strftime("%Y-%m-%d") if c.date else None,
                        "summary": c.summary[:500] if c.summary else None,
                        "status": c.status or "active",
                        "tags": json.loads(c.tags) if c.tags else [],
                        "url": c.url,
                        "citation": f"[[circular:{c.id}|{c.display_name}]]",
                    })
                return json.dumps({"results": out, "count": len(out)})

            elif name == "get_circular_details":
                from .models import Circular
                from sqlalchemy import or_
                ref = arguments.get("circular_reference", "").strip()
                if not ref:
                    return json.dumps({"error": "No circular reference provided"})
                from .search import SearchEngine, search_engine

                has_year = bool(re.search(r"\b(?:19\d{2}|20\d{2})\b", ref))
                ref_matches = SearchEngine._search_by_reference(ref, db, limit=5)
                if len(ref_matches) > 1 and not has_year:
                    return json.dumps({
                        "error": (
                            "Ambiguous circular reference. Include the year to "
                            "retrieve a specific circular."
                        ),
                        "candidates": [
                            {
                                "title": item.title,
                                "reference": item.reference,
                                "department": item.department,
                                "date": item.date.strftime("%Y-%m-%d") if item.date else None,
                                "citation": (
                                    f"[[circular:{item.id}|{item.display_name}]]"
                                ),
                            }
                            for item in ref_matches
                        ],
                    })

                c = ref_matches[0] if ref_matches else None
                # Try exact reference match, then title ILIKE, when the query is
                # not a parsed circular reference.
                if not c:
                    c = db.query(Circular).filter(Circular.reference == ref).first()
                if not c:
                    c = db.query(Circular).filter(
                        or_(
                            Circular.title.ilike(f"%{ref}%"),
                            Circular.reference.ilike(f"%{ref}%"),
                        )
                    ).first()
                if not c:
                    results, _ = search_engine.search(ref, db, limit=1)
                    c = results[0]["circular"] if results else None
                if not c:
                    return json.dumps({"error": f"Circular not found: {ref}"})
                return json.dumps({
                    "title": c.title,
                    "reference": c.reference,
                    "department": c.department,
                    "date": c.date.strftime("%Y-%m-%d") if c.date else None,
                    "url": c.url,
                    "summary": c.summary,
                    "tags": json.loads(c.tags) if c.tags else [],
                    "compliance_checklist": compact_required_checklist(c.compliance_checklist),
                    "status": c.status or "active",
                    "content_preview": (c.content_text or "")[:2000],
                    "citation": f"[[circular:{c.id}|{c.display_name}]]",
                    "attachment_citations": [
                        f"[[attachment:{item.id}|{item.filename}]]"
                        for item in c.attachments
                    ],
                })

            elif name == "get_circulars_by_tag":
                from .models import Circular
                tag = arguments.get("tag", "")
                limit = int(arguments.get("limit", 10))
                rows = db.query(Circular).filter(
                    Circular.tags.like(f'%"{tag}"%')
                ).order_by(Circular.date.desc()).limit(limit).all()
                out = []
                for c in rows:
                    out.append({
                        "title": c.title,
                        "reference": c.reference,
                        "department": c.department,
                        "date": c.date.strftime("%Y-%m-%d") if c.date else None,
                        "summary": c.summary[:500] if c.summary else None,
                        "status": c.status or "active",
                        "tags": json.loads(c.tags) if c.tags else [],
                        "url": c.url,
                        "citation": f"[[circular:{c.id}|{c.display_name}]]",
                    })
                return json.dumps({"results": out, "count": len(out)})

            elif name == "query_regulatory_values":
                from .models import Circular, CircularEntity
                query = db.query(CircularEntity).join(
                    Circular, CircularEntity.circular_id == Circular.id
                )
                metric = str(arguments.get("metric", "")).strip()
                subject = str(arguments.get("subject", "")).strip()
                entity_type = str(arguments.get("entity_type", "")).strip()
                unit = str(arguments.get("unit", "")).strip()
                comparator = str(arguments.get("comparator", "")).strip()
                if metric:
                    from .search import resolve_metric_terms
                    distinct_metrics = [
                        m[0] for m in db.query(CircularEntity.metric).distinct() if m[0]
                    ]
                    matched = resolve_metric_terms(metric, distinct_metrics)
                    if matched:
                        query = query.filter(CircularEntity.metric.in_(matched))
                    else:
                        query = query.filter(CircularEntity.metric.ilike(f"%{metric}%"))
                if subject:
                    query = query.filter(CircularEntity.subject.ilike(f"%{subject}%"))
                if entity_type:
                    query = query.filter(CircularEntity.entity_type == entity_type)
                if unit:
                    query = query.filter(CircularEntity.unit == unit)
                if comparator:
                    query = query.filter(CircularEntity.comparator == comparator)
                if arguments.get("min_value") is not None:
                    query = query.filter(CircularEntity.value_numeric >= float(arguments["min_value"]))
                if arguments.get("max_value") is not None:
                    query = query.filter(CircularEntity.value_numeric <= float(arguments["max_value"]))
                if arguments.get("current_only"):
                    query = query.filter(~Circular.status.in_(("superseded", "cancelled")))
                limit = max(1, min(int(arguments.get("limit", 20)), 50))
                rows = query.order_by(
                    CircularEntity.effective_date.desc().nullslast(),
                    Circular.date.desc().nullslast(),
                ).limit(200).all()
                if arguments.get("current_only"):
                    seen: set[tuple] = set()
                    deduped = []
                    for entity in rows:
                        key = ((entity.metric or "").lower(), (entity.subject or "").lower())
                        if key in seen:
                            continue
                        seen.add(key)
                        deduped.append(entity)
                    rows = deduped
                rows = rows[:limit]
                out = []
                for entity in rows:
                    c = entity.circular
                    out.append({
                        "metric": entity.metric,
                        "entity_type": entity.entity_type,
                        "comparator": entity.comparator,
                        "value": entity.value_numeric,
                        "value_high": entity.value_high,
                        "unit": entity.unit,
                        "value_text": entity.value_text,
                        "subject": entity.subject,
                        "effective_date": entity.effective_date.strftime("%Y-%m-%d") if entity.effective_date else None,
                        "context": entity.context_snippet,
                        "circular_status": (c.status or "active") if c else None,
                        "circular_date": c.date.strftime("%Y-%m-%d") if c and c.date else None,
                        "citation": f"[[circular:{c.id}|{c.display_name}]]" if c else None,
                    })
                return json.dumps({"results": out, "count": len(out)})

            else:
                return json.dumps({"error": f"Unknown tool: {name}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _chat_system_prompt(self, circulars_context: str | None = None) -> str:
        if circulars_context:
            return f"""You are an expert assistant for analyzing State Bank of Pakistan (SBP) circulars and regulations.
You have been provided with pre-selected circulars as context below. Answer primarily from these,
but you also have tools to search the database if the user asks about circulars not covered here.

IMPORTANT RULES:
1. Cite a circular only with the exact [[circular:ID|label]] token supplied in context or tool results.
2. Cite an attachment only with the exact [[attachment:ID|label]] token supplied in context or tool results.
Never expose IDs outside those tokens, alter a token, invent a token, or turn plain-text references into links.
3. Be precise and highlight regulatory differences when comparing circulars.
4. Use search_selected_documents when the included passages do not contain enough detail. It can
search the complete selected circulars and their attachments. Do not claim attachment content is
unavailable merely because it was not included in the initial context.
5. Use global circular search tools only when the user explicitly requests broader research.

Pre-selected circulars:
{circulars_context}"""
        return """You are an expert assistant for SBP circulars and regulations.
Use your tools to search and retrieve relevant circulars from the database before answering.

IMPORTANT RULES:
1. Cite a circular only with an exact [[circular:ID|label]] token returned by a tool.
2. Cite an attachment only with an exact [[attachment:ID|label]] token returned by a tool.
Never expose IDs outside those tokens, alter a token, invent a token, or turn plain-text references into links.
3. If you need more details on a circular found in a search, use the get_circular_details tool with the circular reference or title."""

    def _chat_full_messages(
        self,
        messages: list[dict[str, str]],
        circulars_context: str | None,
        selected_circular_ids: list[str] | None,
    ) -> list[dict]:
        """Prepend the (context-aware) system prompt to the conversation."""
        system_prompt = self._chat_system_prompt(
            circulars_context if selected_circular_ids else None
        )
        return [{"role": "system", "content": system_prompt}] + messages

    def _apply_tool_calls(
        self,
        full_messages: list[dict],
        assistant_content: str,
        tool_call_dicts: list[dict],
        db: Session,
        selected_circular_ids: list[str] | None,
    ) -> None:
        """Record the assistant's tool requests, run each tool, and append its result.

        `tool_call_dicts` are normalized to the OpenAI on-the-wire shape so the streaming
        and non-streaming paths share this loop.
        """
        full_messages.append({
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": tool_call_dicts,
        })
        for tc in tool_call_dicts:
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            result = self._execute_tool(
                tc["function"]["name"], args, db, selected_circular_ids
            )
            full_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    def chat(
        self,
        messages: list[dict[str, str]],
        db: Session,
        circulars_context: str | None = None,
        selected_circular_ids: list[str] | None = None,
    ) -> str:
        full_messages = self._chat_full_messages(
            messages, circulars_context, selected_circular_ids
        )

        max_iterations = 5
        for _ in range(max_iterations):
            response = self._client.chat.completions.create(
                model=self.config.effective_chat_model,
                messages=full_messages,
                temperature=0.3,
                tools=TOOLS,
                tool_choice="auto",
            )

            msg = response.choices[0].message

            if not msg.tool_calls:
                return msg.content or ""

            self._apply_tool_calls(
                full_messages,
                msg.content or "",
                [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
                db,
                selected_circular_ids,
            )

        # Fallback if max iterations reached. Use a fresh synthesis prompt so
        # models that keep requesting tools do not see prior tool-call messages.
        synthesis_messages = self._tool_result_synthesis_messages(
            messages, full_messages, circulars_context
        )
        try:
            final_response = self._client.chat.completions.create(
                model=self.config.effective_chat_model,
                messages=synthesis_messages,
                temperature=0.3,
            )
        except APIError as exc:
            if self._is_tool_choice_none_error(exc):
                return self._tool_iteration_limit_message()
            raise
        return final_response.choices[0].message.content or ""

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        db: Session,
        circulars_context: str | None = None,
        selected_circular_ids: list[str] | None = None,
    ):
        full_messages = self._chat_full_messages(
            messages, circulars_context, selected_circular_ids
        )

        max_iterations = 5
        for _ in range(max_iterations):
            yield {"phase": "thinking"}
            stream = self._client.chat.completions.create(
                model=self.config.effective_chat_model,
                messages=full_messages,
                temperature=0.3,
                tools=TOOLS,
                tool_choice="auto",
                stream=True,
            )

            content_parts: list[str] = []
            tool_calls: dict[int, dict] = {}

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    content_parts.append(delta.content)
                    yield delta.content

                for tc in delta.tool_calls or []:
                    index = tc.index
                    item = tool_calls.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.id:
                        item["id"] = tc.id
                    if tc.type:
                        item["type"] = tc.type
                    if tc.function:
                        if tc.function.name:
                            item["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            item["function"]["arguments"] += tc.function.arguments

            if not tool_calls:
                return

            ordered_calls = [tool_calls[index] for index in sorted(tool_calls)]
            yield {
                "phase": "tools",
                "tools": [
                    tool_activity_label(call["function"]["name"])
                    for call in ordered_calls
                ],
            }

            self._apply_tool_calls(
                full_messages,
                "".join(content_parts),
                ordered_calls,
                db,
                selected_circular_ids,
            )

        yield {"phase": "thinking"}
        synthesis_messages = self._tool_result_synthesis_messages(
            messages, full_messages, circulars_context
        )
        try:
            final_stream = self._client.chat.completions.create(
                model=self.config.effective_chat_model,
                messages=synthesis_messages,
                temperature=0.3,
                stream=True,
            )
            for chunk in final_stream:
                if not chunk.choices:
                    continue
                content = chunk.choices[0].delta.content
                if content:
                    yield content
        except APIError as exc:
            if self._is_tool_choice_none_error(exc):
                yield self._tool_iteration_limit_message()
                return
            raise

    def check_availability(self) -> dict:
        """Lightweight reachability/auth probe for the configured provider.

        Uses ``models.list()`` (with a short timeout and no retries) so it does
        not consume chat tokens or count against tight generation rate limits.
        Returns a coarse state the sidebar can render without exposing raw
        provider payloads.
        """
        try:
            self._client.with_options(timeout=5.0, max_retries=0).models.list()
        except Exception as exc:
            state, detail = classify_provider_state(exc)
            return {
                "available": False,
                "state": state,
                "detail": detail,
                "provider": self.config.provider,
                "model": self.config.effective_chat_model,
            }
        return {
            "available": True,
            "state": "online",
            "detail": "Backend reachable",
            "provider": self.config.provider,
            "model": self.config.effective_chat_model,
        }

    def test_connection(self) -> dict:
        try:
            response = self._client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": "Say 'Connection successful.'"}],
                max_tokens=10,
                temperature=0.0,
            )
            content = response.choices[0].message.content or ""
            return {"success": True, "response": content}
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_ai_client(db=None) -> AIClient:
    config = None
    if db is not None:
        config = AIConfig.from_db(db)
    if config is None:
        config = AIConfig.from_env()
    return AIClient(config)
