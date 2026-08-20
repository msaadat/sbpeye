from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key


# Where the code lives and where its data lives are not the same question. In a
# container the source sits in a read-only image layer and the writable volume is
# mounted elsewhere, so everything mutable — both databases, `chroma_db/`,
# `attachments/`, `cache/` and the managed env file — hangs off DATA_ROOT rather than
# off wherever this file happens to sit. Defaulting to the source root keeps local
# checkouts and the whole test suite working unchanged.
#
# `SBPEYE_DATA_DIR` has to be a real process environment variable. It is read before
# any env file is loaded, because it is what says where those files are; setting it in
# `.env.local` cannot work.
CODE_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.getenv("SBPEYE_DATA_DIR") or CODE_ROOT).resolve()

# Every use of this name is a database, a cache or the attachment tree; none of it is a
# code path, so it is an alias rather than a second root. Kept because ~40 call sites
# import it, and because the attachment path-containment guards in `main` and
# `api/serializers` compare against it — one name means they cannot drift from the tree
# they are guarding. Package assets resolve relative to `__file__` (see `main.STATIC_DIR`).
PROJECT_ROOT = DATA_ROOT

MANAGED_ENV_FILE = DATA_ROOT / ".env.local"
_ENV_FILES = (DATA_ROOT / ".env", MANAGED_ENV_FILE)
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
