from .ai import get_ai_client, AIClient

_client = None

def _get_client() -> AIClient:
    global _client
    if _client is None:
        _client = get_ai_client()
    return _client


def extract_relationships(circular_text: str):
    client = _get_client()
    result = client.extract_relationships("", "", circular_text)
    return result