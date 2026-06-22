from ..ai import get_ai_client


def extract_relationships(circular_text: str):
    # Resolve configuration for each operation so Settings changes apply immediately.
    client = get_ai_client()
    result = client.extract_relationships("", "", circular_text)
    return result
