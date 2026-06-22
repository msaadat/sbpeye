from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANAGED_ENV_FILE = PROJECT_ROOT / ".env.local"
_ENV_FILES = (PROJECT_ROOT / ".env", MANAGED_ENV_FILE)
_ORIGINAL_ENV = dict(os.environ)


@lru_cache(maxsize=1)
def load_app_env() -> Path:
    for path in _ENV_FILES:
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if value is None or key in _ORIGINAL_ENV:
                continue
            os.environ[key] = value
    return MANAGED_ENV_FILE


def managed_env_path() -> Path:
    return load_app_env()


def resolve_env_value(*keys: str, default: str = "") -> tuple[str, str | None]:
    load_app_env()
    for key in keys:
        value = os.getenv(key)
        if value is not None and value != "":
            return value, key
    return default, None


def set_managed_env_value(key: str, value: str) -> None:
    path = managed_env_path()
    path.touch(exist_ok=True)
    set_key(str(path), key, value, quote_mode="auto")
    os.environ[key] = value


def unset_managed_env_value(key: str) -> None:
    path = managed_env_path()
    if path.exists():
        unset_key(str(path), key)
    if key in _ORIGINAL_ENV:
        os.environ[key] = _ORIGINAL_ENV[key]
    else:
        os.environ.pop(key, None)
