"""Compression and caching of what the SPA downloads.

These characterize `docs/PERFORMANCE_PLAN.md` P1 and P7. They are header assertions
rather than timings, because the thing worth locking in is not "it is fast" but the two
properties the speed rests on: that bulk responses are compressed, and that the chunk
Vite content-hashed is allowed to stay in the browser's cache.

The `text/event-stream` case is the important one. Compression is only safe on the chat
stream because Starlette excludes that content type outright; a release that changed its
mind would turn token-by-token delivery into one buffered block at the end of the turn,
which no other test in the suite would notice — `test_chat_stream_creates_session_and_streams`
collects the whole body before asserting on it.
"""
import gzip

from sbpeye.main import SPA_ASSETS_DIR

import pytest


IMMUTABLE = "public, max-age=31536000, immutable"


def _any_asset() -> str:
    if not SPA_ASSETS_DIR.exists():
        pytest.skip("SPA not built — run `npm run build` in frontend/")
    for path in sorted(SPA_ASSETS_DIR.iterdir()):
        if path.suffix in (".js", ".css") and path.stat().st_size > 1000:
            return path.name
    pytest.skip("no built asset over the compression threshold")


def test_spa_assets_are_cacheable_forever(client):
    """Hashed filenames are immutable, so re-validating them is a wasted round trip."""
    test_client, _ = client
    response = test_client.get(f"/spa/assets/{_any_asset()}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE


def test_not_modified_still_carries_cache_control(client):
    """A 304 that says nothing about freshness asks to be re-validated again next time."""
    test_client, _ = client
    name = _any_asset()
    etag = test_client.get(f"/spa/assets/{name}").headers["etag"]

    response = test_client.get(f"/spa/assets/{name}", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.headers["cache-control"] == IMMUTABLE


def test_spa_assets_are_compressed(client):
    """The landing route ships ~800 KB of JS/CSS; uncompressed that is the page load."""
    test_client, _ = client
    name = _any_asset()
    raw = (SPA_ASSETS_DIR / name).read_bytes()

    response = test_client.get(
        f"/spa/assets/{name}", headers={"Accept-Encoding": "gzip"}
    )
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    # httpx transparently decodes, so compare against the file to prove it round-trips.
    assert response.content == raw


def test_index_html_is_not_cached_forever(client):
    """`index.html` names the new hashes after a deploy. Pinning it strands testers."""
    test_client, _ = client
    response = test_client.get("/spa/index.html", follow_redirects=False)
    if response.status_code == 200:
        assert response.headers.get("cache-control") != IMMUTABLE


def test_small_responses_are_not_compressed(client):
    """`minimum_size` has to actually fire.

    It does not fire on its own: `@app.middleware("http")` wraps the app in a
    `BaseHTTPMiddleware` that re-emits every response as a stream, and a GZip layer
    outside that never sees a single-shot body to measure. Compression is registered
    *inside* it for this reason, and this test is what says so.
    """
    test_client, _ = client
    response = test_client.get("/healthz", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert len(response.content) < 1000
    assert "content-encoding" not in response.headers


def test_event_stream_is_never_compressed(client):
    """Compressing SSE would buffer the chat answer into one block at the end."""
    test_client, _ = client
    with test_client.stream(
        "POST",
        "/api/chat/stream",
        json={"message": "Stream me"},
        headers={"Accept-Encoding": "gzip"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in response.headers
        response.read()
