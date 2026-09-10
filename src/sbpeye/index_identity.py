"""Storage-independent identities for semantic index ledger rows."""

import uuid


def ledger_row_id(ledger_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ledger_id))
