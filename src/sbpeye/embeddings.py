import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from openai import OpenAI
from sqlalchemy import text


DEFAULT_FASTEMBED_MODEL = "BAAI/bge-base-en-v1.5"
DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str = "fastembed"
    model: str = DEFAULT_FASTEMBED_MODEL
    base_url: str = DEFAULT_LM_STUDIO_URL
    api_key: str = "lm-studio"

    @classmethod
    def from_env(cls) -> "EmbeddingConfig":
        return cls(
            provider=os.getenv("EMBEDDING_PROVIDER", "fastembed").strip().lower(),
            model=os.getenv("EMBEDDING_MODEL", DEFAULT_FASTEMBED_MODEL).strip(),
            base_url=os.getenv("EMBEDDING_BASE_URL", DEFAULT_LM_STUDIO_URL).strip(),
            api_key=os.getenv("EMBEDDING_API_KEY", "lm-studio").strip(),
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
    def from_db(cls, db) -> "EmbeddingConfig":
        from .models import Settings

        config = cls.from_env()
        rows = db.query(Settings).filter(Settings.key.like("embedding_%")).all()
        values = {row.key: row.value for row in rows}
        return cls(
            provider=values.get("embedding_provider", config.provider).strip().lower(),
            model=values.get("embedding_model", config.model).strip(),
            base_url=values.get("embedding_base_url", config.base_url).strip(),
            api_key=values.get("embedding_api_key", config.api_key).strip(),
        )

    def save_to_db(self, db) -> None:
        from .models import Settings

        values = {
            "embedding_provider": self.provider,
            "embedding_model": self.model,
            "embedding_base_url": self.base_url,
            "embedding_api_key": self.api_key,
        }
        for key, value in values.items():
            row = db.query(Settings).filter(Settings.key == key).first()
            if row:
                row.value = value
            else:
                db.add(Settings(key=key, value=value))
        db.commit()

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

        values = {key: value for key, value in rows}
        return cls(
            provider=values.get("embedding_provider", config.provider).strip().lower(),
            model=values.get("embedding_model", config.model).strip(),
            base_url=values.get("embedding_base_url", config.base_url).strip(),
            api_key=values.get("embedding_api_key", config.api_key).strip(),
        )


class EmbeddingBackend(Protocol):
    config: EmbeddingConfig

    def embed_documents(self, documents: list[str]) -> list[list[float]]: ...

    def embed_queries(self, queries: list[str]) -> list[list[float]]: ...


class FastEmbedBackend:
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._model = None
        self._lock = threading.Lock()

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
        model = self._get_model()
        if hasattr(model, "query_embed"):
            return self._as_lists(model.query_embed(queries))
        return self._as_lists(model.embed(queries))


class LMStudioEmbeddingBackend:
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "lm-studio",
            timeout=30.0,
            max_retries=1,
        )

    def _embed(self, values: list[str]) -> list[list[float]]:
        response = self._client.embeddings.create(
            model=self.config.model,
            input=values,
        )
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]

    def embed_documents(self, documents: list[str]) -> list[list[float]]:
        return self._embed(documents)

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        return self._embed(queries)


def create_embedding_backend(config: EmbeddingConfig) -> EmbeddingBackend:
    if config.provider == "fastembed":
        return FastEmbedBackend(config)
    if config.provider in {"lmstudio", "lm_studio"}:
        return LMStudioEmbeddingBackend(config)
    raise ValueError(
        f"Unsupported embedding provider '{config.provider}'. Use 'fastembed' or 'lmstudio'."
    )
