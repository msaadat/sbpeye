"""Pure SBP URL validation, shared by retrieval and migration diagnostics."""

from urllib.parse import urldefrag, urlparse

def is_allowed_sbp_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and bool(hostname)
        and (hostname == "sbp.org.pk" or hostname.endswith(".sbp.org.pk"))
        and parsed.username is None
        and parsed.password is None
    )


def normalize_sbp_url(url: str) -> str:
    normalized = urldefrag(url.strip())[0]
    if normalized.startswith("http://"):
        normalized = "https://" + normalized[7:]
    if not is_allowed_sbp_url(normalized):
        raise ValueError("Only HTTPS links on sbp.org.pk are supported.")
    return normalized


