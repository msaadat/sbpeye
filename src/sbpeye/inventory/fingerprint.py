"""Identity of the things that decide what a chunk looks like.

The ledger stores these alongside every source so a later run can tell whether an index
entry was produced by the chunker and embedding model currently configured. Any change
here must change the recorded value, or stale chunks will pass validation.

See docs/INVENTORY_SEARCH_PLAN.md sections 10 and 12.
"""

import hashlib

# Bump whenever chunk boundaries or embedded text change. Section 12.2 of the plan moved
# from 350-word chunks carrying a "{doc_label}. {ref}. " prefix to mention-sized chunks
# with the prefix held in metadata only; that transition is v1 -> v2.
CHUNKER_VERSION = "v2"


def embedding_fingerprint(config) -> str:
    """Stable identity for an embedding configuration.

    Provider and model decide the vector space. Dimension is not included: it is a
    consequence of the model, and reading it would require loading the backend.
    """
    payload = f"{config.provider}|{config.model}"
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def content_hash(text: str) -> str:
    """Hash of the exact source text a set of chunks was built from."""
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]
