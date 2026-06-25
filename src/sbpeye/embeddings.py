import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import numpy as np
from openai import OpenAI
from sqlalchemy import text

from .env import load_app_env, resolve_env_value


load_app_env()


DEFAULT_FASTEMBED_MODEL = "BAAI/bge-base-en-v1.5"
DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"
QUERY_CACHE_SIZE = 128


def _embed_queries_with_cache(
    queries: list[str],
    cache: OrderedDict[str, list[float]],
    lock: threading.Lock,
    embed_missing: Callable[[list[str]], list[list[float]]],
) -> list[list[float]]:
    if not queries:
        return []

    resolved: dict[str, list[float]] = {}
    with lock:
        for query in queries:
            if query in cache:
                resolved[query] = cache[query]
                cache.move_to_end(query)

    missing = list(dict.fromkeys(query for query in queries if query not in resolved))
    if missing:
        embeddings = embed_missing(missing)
        if len(embeddings) != len(missing):
            raise RuntimeError("Embedding backend returned an unexpected number of results")

        resolved.update(zip(missing, embeddings, strict=True))
        with lock:
            for query, embedding in zip(missing, embeddings, strict=True):
                cache[query] = embedding
                cache.move_to_end(query)
                if len(cache) > QUERY_CACHE_SIZE:
                    cache.popitem(last=False)

    return [resolved[query] for query in queries]


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str = "fastembed"
    model: str = DEFAULT_FASTEMBED_MODEL
    base_url: str = DEFAULT_LM_STUDIO_URL
    api_key: str = "lm-studio"

    @classmethod
    def from_env(cls) -> "EmbeddingConfig":
        api_key, _ = resolve_env_value("EMBEDDING_API_KEY", default="lm-studio")
        return cls(
            provider=os.getenv("EMBEDDING_PROVIDER", "fastembed").strip().lower(),
            model=os.getenv("EMBEDDING_MODEL", DEFAULT_FASTEMBED_MODEL).strip(),
            base_url=os.getenv("EMBEDDING_BASE_URL", DEFAULT_LM_STUDIO_URL).strip(),
            api_key=api_key.strip(),
        )

    @classmethod
    def _project_root(cls) -> Path:
        return Path(__file__).resolve().parents[2]

    @classmethod
    def cache_dir(cls) -> str:
        return os.getenv(
            "FASTEMBED_CACHE_PATH",
            str(cls._project_root() / "cache" / "models"),
        )

    @classmethod
    def _from_settings_values(cls, values: dict, fallback: "EmbeddingConfig") -> "EmbeddingConfig":
        """Build a config from stored ``embedding_*`` settings, falling back to env."""
        return cls(
            provider=values.get("embedding_provider", fallback.provider).strip().lower(),
            model=values.get("embedding_model", fallback.model).strip(),
            base_url=values.get("embedding_base_url", fallback.base_url).strip(),
            api_key=fallback.api_key,
        )

    @classmethod
    def from_db(cls, db) -> "EmbeddingConfig":
        from .models import Settings

        rows = db.query(Settings).filter(Settings.key.like("embedding_%")).all()
        return cls._from_settings_values(
            {row.key: row.value for row in rows}, cls.from_env()
        )

    def save_to_db(self, db) -> None:
        from .models import upsert_settings

        upsert_settings(db, {
            "embedding_provider": self.provider,
            "embedding_model": self.model,
            "embedding_base_url": self.base_url,
        })

    @classmethod
    def from_database(cls, engine) -> "EmbeddingConfig":
        config = cls.from_env()
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text("SELECT key, value FROM settings WHERE key LIKE 'embedding_%'")
                ).all()
        except Exception:
            return config

        return cls._from_settings_values({key: value for key, value in rows}, config)

    @classmethod
    def secret_state(cls, provider: str | None = None) -> dict[str, str | bool]:
        normalized_provider = (provider or "fastembed").strip().lower()
        api_key, env_var = resolve_env_value("EMBEDDING_API_KEY", default="")
        return {
            "api_key_configured": normalized_provider != "fastembed" and bool(api_key),
            "api_key_env_var": env_var or "EMBEDDING_API_KEY",
        }


class EmbeddingBackend(Protocol):
    config: EmbeddingConfig

    def embed_documents(self, documents: list[str]) -> list[list[float]]: ...

    def embed_queries(self, queries: list[str]) -> list[list[float]]: ...


class FastEmbedBackend:
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._model = None
        self._lock = threading.Lock()
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._query_cache_lock = threading.Lock()

    @staticmethod
    def _preferred_providers() -> list[str]:
        # Prefer native GPU backends before broader accelerators, then CPU fallback.
        return [
            "CUDAExecutionProvider",
            "MIGraphXExecutionProvider",
            "DmlExecutionProvider",
            "OpenVINOExecutionProvider",
            "CoreMLExecutionProvider",
            "CPUExecutionProvider",
        ]

    @staticmethod
    def _display_backend(provider: str) -> str:
        labels = {
            "CUDAExecutionProvider": "CUDA",
            "MIGraphXExecutionProvider": "AMD MigraphX",
            "DmlExecutionProvider": "DML",
            "OpenVINOExecutionProvider": "OpenVINO",
            "CoreMLExecutionProvider": "CoreML",
            "CPUExecutionProvider": "CPU",
        }
        return labels.get(provider, provider or "Unknown")

    def _select_providers(self) -> list[str]:
        try:
            import onnxruntime as ort
        except ImportError:
            return ["CPUExecutionProvider"]

        available = set(ort.get_available_providers())
        selected = [
            provider for provider in self._preferred_providers() if provider in available
        ]
        if not selected:
            selected = ["CPUExecutionProvider"]

        return selected

    def _get_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from fastembed import TextEmbedding
                    except ImportError as exc:
                        raise RuntimeError(
                            "FastEmbed could not load ONNX Runtime. On Windows, install the latest "
                            "Microsoft Visual C++ 2015-2022 Redistributable (x64)."
                        ) from exc

                    selected_providers = self._select_providers()

                    try:
                        self._model = TextEmbedding(
                            model_name=self.config.model,
                            cache_dir=EmbeddingConfig.cache_dir(),
                            providers=selected_providers,
                        )
                    except Exception:
                        self._model = TextEmbedding(
                            model_name=self.config.model,
                            cache_dir=EmbeddingConfig.cache_dir(),
                        )
                        print("[FastEmbed] Backend: AUTO")
                    else:
                        active_provider = selected_providers[0] if selected_providers else ""
                        session = getattr(getattr(self._model, "model", None), "model", None)
                        if hasattr(session, "get_providers"):
                            active_providers = session.get_providers()
                            if active_providers:
                                active_provider = active_providers[0]
                        print(
                            f"[FastEmbed] Backend: {self._display_backend(active_provider)} "
                            f"(providers={', '.join(selected_providers)})"
                        )

        return self._model

    @staticmethod
    def _as_lists(embeddings) -> list[list[float]]:
        return [np.asarray(item, dtype=np.float32).tolist() for item in embeddings]

    def embed_documents(self, documents: list[str]) -> list[list[float]]:
        return self._as_lists(self._get_model().embed(documents))

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        def embed_missing(values: list[str]) -> list[list[float]]:
            model = self._get_model()
            if hasattr(model, "query_embed"):
                return self._as_lists(model.query_embed(values))
            return self._as_lists(model.embed(values))

        return _embed_queries_with_cache(
            queries, self._query_cache, self._query_cache_lock, embed_missing
        )


class LMStudioEmbeddingBackend:
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "lm-studio",
            timeout=30.0,
            max_retries=1,
        )
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._query_cache_lock = threading.Lock()

    def _embed(self, values: list[str]) -> list[list[float]]:
        response = self._client.embeddings.create(
            model=self.config.model,
            input=values,
        )
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]

    def embed_documents(self, documents: list[str]) -> list[list[float]]:
        return self._embed(documents)

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        return _embed_queries_with_cache(
            queries, self._query_cache, self._query_cache_lock, self._embed
        )


def create_embedding_backend(config: EmbeddingConfig) -> EmbeddingBackend:
    if config.provider == "fastembed":
        return FastEmbedBackend(config)
    if config.provider in {"lmstudio", "lm_studio"}:
        return LMStudioEmbeddingBackend(config)
    raise ValueError(
        f"Unsupported embedding provider '{config.provider}'. Use 'fastembed' or 'lmstudio'."
    )
