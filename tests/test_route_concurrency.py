"""Route handlers that block must not be declared `async def`.

FastAPI runs an `async def` handler *on the event loop* and a plain `def` handler in a
threadpool. So for code that blocks — every SQLAlchemy query in this application, since
the ORM is synchronous — `async def` is not the more concurrent choice, it is the one that
serializes the whole process. Measured through this app: five concurrent 300 ms requests
took 1.50 s and served 1 other request as `async def`, against 0.30 s and 39 as `def`.

This is a *convention* test rather than a behaviour one, and it exists because the
convention had no other home. Every route in the initial commit was `async def` — 34 of 34
— and the one place it was corrected (`search_circulars`, in an embeddings commit) left no
note, so the fix never propagated and later routes kept inheriting the default. A rule
nobody wrote down is a rule that drifts back. See docs/PERFORMANCE_PLAN.md P6.
"""
import ast
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "sbpeye"
ROUTE_FILES = ["main.py", "api/admin.py", "api/debug.py", "auth_routes.py"]
ROUTE_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

# Anything that means a database session is in play. A handler holding one is blocking by
# definition here, whatever it goes on to do with it.
SESSION_DEPENDENCIES = ("get_db", "get_app_db", "get_debug_db")


def _is_route(node: ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ROUTE_METHODS
            and isinstance(func.value, ast.Name)
            and func.value.id in ("app", "router")
        ):
            return True
    return False


def _awaits(node: ast.AsyncFunctionDef) -> bool:
    """A real suspension point anywhere inside, nested scopes included."""
    return any(
        isinstance(child, (ast.Await, ast.AsyncWith, ast.AsyncFor))
        for child in ast.walk(node)
    )


def _takes_a_session(node, source: str) -> bool:
    defaults = [d for d in node.args.defaults + node.args.kw_defaults if d is not None]
    texts = [ast.get_source_segment(source, d) or "" for d in defaults]
    texts += [ast.get_source_segment(source, d) or "" for d in node.decorator_list]
    return any(dep in text for text in texts for dep in SESSION_DEPENDENCIES)


def _offenders():
    for filename in ROUTE_FILES:
        path = SOURCE_ROOT / filename
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.AsyncFunctionDef) or not _is_route(node):
                continue
            # An `async def` that awaits is doing what async is for; leave it be.
            if _awaits(node):
                continue
            if _takes_a_session(node, source):
                yield f"{filename}:{node.lineno} {node.name}"


def test_no_route_holds_a_database_session_on_the_event_loop():
    offenders = sorted(_offenders())
    assert not offenders, (
        "These routes are `async def`, never `await`, and hold a database session — so "
        "their queries run on the event loop and block every other request in the "
        "process for the duration. Drop the `async`; FastAPI will run them in its "
        "threadpool.\n  " + "\n  ".join(offenders)
    )


def test_the_check_can_actually_find_something():
    """Guards the guard: an AST walk that silently matches nothing always passes.

    If `_is_route` stops recognising the decorator — a rename, a new router object, a
    different FastAPI idiom — the test above goes green by finding no routes at all rather
    than by the routes being correct.
    """
    source = (SOURCE_ROOT / "main.py").read_text()
    routes = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and _is_route(node)
    ]
    assert len(routes) > 50, f"only found {len(routes)} routes in main.py — parser drift?"

    # And that the session sniffer still recognises the idiom the routes actually use.
    with_session = [
        node for node in routes
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and _takes_a_session(node, source)
    ]
    assert len(with_session) > 20, (
        f"only {len(with_session)} routes appear to take a session — the dependency "
        "idiom probably changed, which would make the check above vacuous"
    )
