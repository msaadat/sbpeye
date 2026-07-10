"""Safety net for the router split: the full set of registered routes must not change.

If a handler is dropped or its path/method changes during refactoring, this fails.
"""

from starlette.routing import Match

import sbpeye.main as main_module

EXPECTED_ROUTES = {
    ("DELETE", "/api/chat/sessions/{session_id}"),
    ("DELETE", "/api/chat/sessions/{session_id}/messages/{message_id}"),
    ("DELETE", "/api/workspaces/{workspace_id}"),
    ("DELETE", "/api/workspaces/{workspace_id}/circulars/{circular_id}"),
    ("GET", "/"),
    ("GET", "/api/ai/jobs/{job_id}"),
    ("GET", "/api/app/status"),
    ("GET", "/api/chat/sessions"),
    ("GET", "/api/chat/sessions/{session_id}"),
    ("GET", "/api/circulars/browse"),
    ("GET", "/api/circulars/browse_recent"),
    ("GET", "/api/circulars/by_url"),
    ("GET", "/api/circulars/departments"),
    ("GET", "/api/circulars/export_csv"),
    ("GET", "/api/circulars/search"),
    ("GET", "/api/circulars/sync/status"),
    ("GET", "/api/circulars/tags"),
    ("GET", "/api/circulars/years"),
    ("GET", "/api/circulars/{circular_id}"),
    ("GET", "/api/circulars/{circular_id}/checklist.xlsx"),
    ("GET", "/api/circulars/{circular_id}/document"),
    ("GET", "/api/circulars/{circular_id}/relationships"),
    ("GET", "/api/circulars/{circular_id}/source"),
    ("GET", "/api/documents/{attachment_id}/content"),
    ("GET", "/api/ecodata"),
    ("GET", "/api/ecodata/entries"),
    ("GET", "/api/ecodata/pdf_summary"),
    ("GET", "/api/pdf_preview"),
    ("GET", "/api/pdf_proxy"),
    ("GET", "/api/sbp_news"),
    ("GET", "/api/settings"),
    ("GET", "/api/workspaces"),
    ("GET", "/api/workspaces/default"),
    ("GET", "/api/workspaces/{workspace_id}"),
    ("GET", "/chat"),
    ("GET", "/circulars"),
    ("GET", "/circulars/{path:path}"),
    ("GET", "/documents/{path:path}"),
    ("GET", "/ecodata"),
    ("GET", "/settings"),
    ("PATCH", "/api/chat/sessions/{session_id}"),
    ("PATCH", "/api/workspaces/{workspace_id}"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream"),
    ("POST", "/api/circulars/batch_download"),
    ("POST", "/api/circulars/open"),
    ("POST", "/api/circulars/sync"),
    ("POST", "/api/circulars/{circular_id}/generate"),
    ("POST", "/api/circulars/{circular_id}/refresh"),
    ("POST", "/api/documents/resolve"),
    ("POST", "/api/settings"),
    ("POST", "/api/settings/embeddings/test"),
    ("POST", "/api/settings/test"),
    ("POST", "/api/workspaces"),
    ("POST", "/api/workspaces/{workspace_id}/circulars"),
}


def _app_routes() -> set[tuple[str, str]]:
    routes = set()
    for route in main_module.app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes.add((method, route.path))
    return routes


def test_all_expected_routes_are_registered():
    registered = _app_routes()
    missing = EXPECTED_ROUTES - registered
    assert not missing, f"Routes went missing during refactor: {sorted(missing)}"


def _first_full_match(method: str, path: str):
    scope = {"type": "http", "method": method, "path": path}
    for route in main_module.app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


def test_literal_routes_are_not_shadowed():
    """A literal path (e.g. /api/circulars/export_csv) registered after a
    parameterized sibling (e.g. /api/circulars/{circular_id}) would silently
    resolve to the parameterized route instead — Starlette matches routes in
    registration order. See CODE_REVIEW_FINDINGS.md §1.1."""
    for method, path in EXPECTED_ROUTES:
        if "{" in path:
            continue
        route = _first_full_match(method, path)
        assert route is not None, f"No route matched {method} {path}"
        assert route.path == path, (
            f"{method} {path} is shadowed by earlier route {route.path}"
        )
