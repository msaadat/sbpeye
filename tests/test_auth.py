"""The authentication and authorization boundary.

Everything else in the suite runs as a signed-in admin, because those tests are about
behaviour and logging in first would say nothing about the thing under test. This module
is the exception: it signs in as a tester, or as nobody, and checks the boundary itself.
"""

import pytest

import sbpeye.ai as ai_module
import sbpeye.main as main_module
from conftest import TEST_ADMIN_ID, sign_in, sign_out
from sbpeye.auth import (
    MIN_PASSWORD_LENGTH,
    SESSION_COOKIE,
    decrypt_secret,
    encrypt_secret,
    issue_session,
    read_session,
    verify_password,
)
from sbpeye.models import ChatSession, User


# --------------------------------------------------------------- the closed door


@pytest.mark.parametrize("path", [
    "/api/circulars/search?q=test",
    "/api/chat/sessions",
    "/api/workspaces",
    "/api/settings",
    "/api/llm/status",
])
def test_api_routes_are_closed_without_a_session(client, path):
    test_client, _ = client
    sign_out(test_client)

    response = test_client.get(path)

    assert response.status_code == 401
    assert response.json() == {"error": "Sign in to continue."}


def test_page_routes_redirect_to_the_login_form(client):
    """A browser navigation gets sent somewhere useful, not a bare 401 body."""
    test_client, _ = client
    sign_out(test_client)

    response = test_client.get("/circulars", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    # The path it was heading for is preserved so the redirect can be undone after login.
    assert "%2Fcirculars" in response.headers["location"]


def test_healthz_and_login_stay_public(client):
    """The two things that cannot present a cookie: a platform probe and the form itself.

    A health check that 401s reads as an unhealthy container and gets a good deploy
    rolled; a login page behind a login is a locked door with the key inside.
    """
    test_client, _ = client
    sign_out(test_client)

    assert test_client.get("/healthz").status_code == 200
    assert test_client.get("/login").status_code == 200


def test_a_new_route_is_private_by_default():
    """The reason the boundary is middleware rather than a per-route dependency.

    If this ever fails, someone has added a path to the allowlist — which is a choice
    worth making deliberately, not by inheriting a default.
    """
    from sbpeye.auth_routes import is_public_path

    assert is_public_path("/api/some/route/added/next/year") is False
    assert is_public_path("/healthz") is True


# --------------------------------------------------------------------- signing in


def test_login_sets_an_httponly_cookie_and_reports_the_user(client, db_factory):
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, email="pat@example.com")
    test_client.cookies.delete(SESSION_COOKIE)

    response = test_client.post("/api/auth/login", json={
        "email": "pat@example.com", "password": "correct-horse-battery-staple",
    })

    assert response.status_code == 200
    assert response.json()["email"] == "pat@example.com"
    cookie = response.headers["set-cookie"]
    # HttpOnly so a cross-site script cannot read the session out of document.cookie.
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()


def test_login_is_case_insensitive_about_the_address(client, db_factory):
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, email="pat@example.com")
    test_client.cookies.delete(SESSION_COOKIE)

    response = test_client.post("/api/auth/login", json={
        "email": "  PAT@Example.COM  ", "password": "correct-horse-battery-staple",
    })

    assert response.status_code == 200


def test_a_wrong_password_and_an_unknown_address_are_indistinguishable(client, db_factory):
    """Otherwise the form is a way to find out who has an account here."""
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, email="pat@example.com")
    test_client.cookies.delete(SESSION_COOKIE)

    wrong_password = test_client.post("/api/auth/login", json={
        "email": "pat@example.com", "password": "not-the-right-one",
    })
    unknown_user = test_client.post("/api/auth/login", json={
        "email": "nobody@example.com", "password": "not-the-right-one",
    })

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_a_tampered_cookie_is_not_a_session(client):
    test_client, _ = client
    test_client.cookies.set(SESSION_COOKIE, "forged.session.value")

    assert test_client.get("/api/chat/sessions").status_code == 401


def test_a_session_survives_a_round_trip_but_not_a_different_secret(monkeypatch):
    user = User(id="u-1", email="a@b.c", password_hash="x")
    token = issue_session(user)
    assert read_session(token) == "u-1"

    monkeypatch.setenv("SBPEYE_SECRET_KEY", "a-completely-different-secret-key-value-01")
    # Rotating the key invalidates every outstanding session. That is the documented
    # revocation mechanism, since there is no sessions table to delete rows from.
    assert read_session(token) is None


def test_logout_clears_the_cookie(client):
    test_client, _ = client

    response = test_client.post("/api/auth/logout")

    assert response.status_code == 200
    assert 'sbpeye_session=""' in response.headers["set-cookie"]


# ------------------------------------------------------------------ the admin gate


ADMIN_ONLY_POSTS = [
    "/api/circulars/sync",
    "/api/settings",
    "/api/settings/models",
]


@pytest.mark.parametrize("path", ADMIN_ONLY_POSTS)
def test_corpus_writers_refuse_a_tester(client, db_factory, path):
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, is_admin=False)

    response = test_client.post(path, json={})

    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/api/settings", "/api/admin/users"])
def test_deployment_configuration_is_admin_only_to_read(client, db_factory, path):
    """Reading is gated as well as writing.

    The payload carries no credential (`api_key` is blanked and only a `configured` flag
    is exposed), but which provider and model the deployment runs on is still the admin's
    configuration, and the settings UI hides those cards from a tester anyway.
    """
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, is_admin=False)

    assert test_client.get(path).status_code == 403


def test_a_tester_can_read_and_write_their_own_provider_settings(client, db_factory):
    """The counterpart: the credential they pay with is theirs to set."""
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, is_admin=False)

    assert test_client.get("/api/settings/ai").status_code == 200
    saved = test_client.put("/api/settings/ai", json={
        "provider": "mistral", "api_key": "sk-theirs",
    })
    assert saved.status_code == 200
    assert saved.json()["api_key_set"] is True


def test_link_discovery_explains_itself_rather_than_bare_403(client, db_factory):
    """A tester pasting an SBP link should read a policy, not what looks like a bug."""
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, is_admin=False)

    response = test_client.post("/api/circulars/open", json={"url": "https://x"})

    assert response.status_code == 403
    assert "administrators" in response.json()["error"]
    assert "Ask an admin" in response.json()["error"]


@pytest.mark.parametrize("path", [
    "/api/circulars/sync",
    "/api/circulars/c1/generate",
    "/api/settings",
])
def test_a_refusal_arrives_in_the_shape_the_client_reads(client, db_factory, path):
    """`{"error": ...}`, like every other failure the application returns.

    FastAPI answers a raised `HTTPException` with `{"detail": ...}`, which the browser
    client does not read — so every admin-only route explained itself into a key nobody
    looked at, and the user saw "Request failed with 403" instead.
    """
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, is_admin=False)

    response = test_client.post(path, json={})

    assert response.status_code == 403
    body = response.json()
    assert "detail" not in body
    assert "administrators" in body["error"]


def test_the_trace_console_is_admin_only(client, db_factory):
    """Traces hold whole prompts and responses, including other users' chat turns."""
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, is_admin=False)

    assert test_client.get("/api/debug/status").status_code == 403
    assert test_client.get("/debug").status_code == 403


def test_admin_keeps_access(client):
    test_client, _ = client  # signed in as admin by the fixture

    assert test_client.get("/api/admin/users").status_code == 200


# ------------------------------------------------------ user management, admin only


def test_an_admin_can_add_and_remove_a_tester(client, db_factory):
    test_client, _ = client

    created = test_client.post("/api/admin/users", json={
        "email": "new@example.com", "password": "another-long-password",
    })
    assert created.status_code == 200

    listed = test_client.get("/api/admin/users").json()["items"]
    assert "new@example.com" in [u["email"] for u in listed]

    user_id = created.json()["id"]
    assert test_client.delete(f"/api/admin/users/{user_id}").status_code == 200


def test_a_short_password_is_refused_with_a_reason(client):
    test_client, _ = client

    response = test_client.post("/api/admin/users", json={
        "email": "new@example.com", "password": "short",
    })

    assert response.status_code == 400
    assert f"{MIN_PASSWORD_LENGTH} characters" in response.json()["error"]


def test_a_duplicate_address_is_refused(client):
    test_client, _ = client

    test_client.post("/api/admin/users", json={
        "email": "dup@example.com", "password": "a-long-enough-password",
    })
    again = test_client.post("/api/admin/users", json={
        "email": "dup@example.com", "password": "a-long-enough-password",
    })

    assert again.status_code == 400
    assert "already exists" in again.json()["error"]


def test_the_last_admin_cannot_be_deleted(client, db_factory):
    """Otherwise the deployment locks itself out with no way back short of the volume."""
    test_client, _ = client

    response = test_client.delete(f"/api/admin/users/{TEST_ADMIN_ID}")

    assert response.status_code == 400
    assert "your own account" in response.json()["error"]


def test_deleting_a_user_takes_their_chat_with_them(client, db_factory):
    test_client, db = client
    created = test_client.post("/api/admin/users", json={
        "email": "leaver@example.com", "password": "a-long-enough-password",
    }).json()

    app_db = db()
    app_db.add(ChatSession(id="s-leaver", user_id=created["id"], title="theirs"))
    app_db.commit()
    app_db.close()

    test_client.delete(f"/api/admin/users/{created['id']}")

    app_db = db()
    assert app_db.query(ChatSession).filter(ChatSession.id == "s-leaver").first() is None
    app_db.close()


# ------------------------------------------------------------- per-user chat scoping


def test_one_user_cannot_read_another_users_chat(client, db_factory):
    """The privacy expectation sits on chat, which is why it is the scoped thing."""
    test_client, db = client

    app_db = db()
    app_db.add(ChatSession(id="s-private", user_id="somebody-else", title="not yours"))
    app_db.commit()
    app_db.close()

    response = test_client.get("/api/chat/sessions/s-private")

    # 404, not 403: a 403 would confirm the session exists.
    assert response.status_code == 404


def test_the_session_list_shows_only_your_own(client, db_factory):
    test_client, db = client

    app_db = db()
    app_db.add(ChatSession(id="s-mine", user_id=TEST_ADMIN_ID, title="mine"))
    app_db.add(ChatSession(id="s-theirs", user_id="somebody-else", title="theirs"))
    app_db.commit()
    app_db.close()

    listed = test_client.get("/api/chat/sessions").json()
    ids = [item["id"] for item in listed]

    assert "s-mine" in ids
    assert "s-theirs" not in ids


def test_workspace_chat_session_ids_differ_per_user():
    """Workspaces are shared and chat is not, so the derived id has to carry the owner.

    Without this, every tester in the shared default workspace computes the same session
    id and lands in one another's conversation.
    """
    from sbpeye.api.serializers import (
        _workspace_chat_session_id,
        _workspace_id_from_chat_session,
    )

    mine = _workspace_chat_session_id("default", "user-a")
    theirs = _workspace_chat_session_id("default", "user-b")

    assert mine != theirs
    assert _workspace_id_from_chat_session(mine) == "default"
    assert _workspace_id_from_chat_session(theirs) == "default"
    # The pre-authentication form still resolves, so old rows are not orphaned.
    assert _workspace_id_from_chat_session("workspace:default") == "default"


# ------------------------------------------------------------- per-user provider keys


def test_an_api_key_is_encrypted_at_rest_and_never_returned(client, db_factory):
    test_client, db = client

    saved = test_client.put("/api/settings/ai", json={
        "provider": "mistral", "api_key": "sk-super-secret",
    })
    assert saved.status_code == 200

    app_db = db()
    row = app_db.query(User).filter(User.id == TEST_ADMIN_ID).first()
    stored = row.ai_api_key_encrypted
    app_db.close()

    assert "sk-super-secret" not in (stored or "")
    assert decrypt_secret(stored) == "sk-super-secret"

    # Write-only from the API's side: the browser has no use for it, and echoing it back
    # puts a live credential in every response body and screenshot of the settings page.
    shown = test_client.get("/api/settings/ai").json()
    assert shown["api_key_set"] is True
    assert "sk-super-secret" not in str(shown)


def test_changing_the_model_keeps_the_stored_key(client, db_factory):
    test_client, _ = client
    test_client.put("/api/settings/ai", json={
        "provider": "mistral", "api_key": "sk-keep-me",
    })

    test_client.put("/api/settings/ai", json={"provider": "mistral", "model": "big"})

    shown = test_client.get("/api/settings/ai").json()
    assert shown["api_key_set"] is True
    assert shown["model"] == "big"


def test_an_admin_can_copy_their_own_provider_to_the_deployment(client, db_factory, monkeypatch):
    """An admin should not have to type the same credentials twice.

    Server-side because the personal key is write-only through the API: a browser-side
    copy would have to send the key back out to perform it.
    """
    test_client, db = client
    saved: dict = {}
    monkeypatch.setattr(
        main_module, "_save_ai_secret",
        lambda provider, api_key, clear_secret=False: saved.update(
            provider=provider, api_key=api_key
        ),
    )

    test_client.put("/api/settings/ai", json={
        "provider": "mistral", "model": "mistral-large-latest", "api_key": "sk-admins-own",
    })

    response = test_client.post("/api/settings/adopt-my-provider")

    assert response.status_code == 200
    assert response.json()["provider"] == "mistral"
    # The key reaches the deployment secret store without ever passing through a response.
    assert saved == {"provider": "mistral", "api_key": "sk-admins-own"}
    assert "sk-admins-own" not in response.text


def test_copying_a_provider_that_was_never_set_explains_itself(client, db_factory):
    test_client, _ = client

    response = test_client.post("/api/settings/adopt-my-provider")

    assert response.status_code == 400
    assert "Settings first" in response.json()["error"]


def test_copying_the_provider_is_admin_only(client, db_factory):
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, is_admin=False)

    assert test_client.post("/api/settings/adopt-my-provider").status_code == 403


def test_a_user_without_a_key_is_told_to_add_one(client, db_factory):
    """Chat must not fall back to a shared credential; that is what this exists to stop."""
    from sbpeye.ai import AIConfig, MissingUserAIConfig, get_ai_client_for_user

    unconfigured = User(id="u-2", email="x@y.z", password_hash="x")

    assert AIConfig.for_user(unconfigured) is None
    with pytest.raises(MissingUserAIConfig) as excinfo:
        get_ai_client_for_user(unconfigured)
    assert "your own AI provider API key" in str(excinfo.value)


def test_the_llm_badge_reports_the_callers_own_provider_not_the_deployments(
    client, db_factory, monkeypatch
):
    """The sidebar indicator has to answer for the backend chat will actually use.

    It used to probe the deployment config, so a tester with no key of their own saw a
    green light for the admin's provider and then got "add your own API key" on their
    first chat turn.
    """
    test_client, _ = client
    # The suite stubs the per-user resolver for chat; this test is about the resolution
    # itself, so it puts the real one back.
    monkeypatch.setattr(main_module, "get_ai_client_for_user", ai_module.get_ai_client_for_user)
    sign_out(test_client)
    sign_in(test_client, db_factory, is_admin=False)

    response = test_client.get("/api/llm/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "not_configured"
    assert payload["available"] is False
    assert "your own AI provider API key" in payload["detail"]
    # The admin's provider and model do not travel to an account that cannot use them.
    assert payload["provider"] is None
    assert payload["model"] is None


def test_the_llm_badge_probes_the_signed_in_user(client, db_factory, monkeypatch):
    test_client, _ = client
    probed: list = []

    class ProbedClient:
        def check_availability(self):
            return {
                "available": True, "state": "online", "detail": "Backend reachable",
                "provider": "mistral", "model": "mistral-large-latest",
            }

    def resolve(user):
        probed.append(user.email)
        return ProbedClient()

    monkeypatch.setattr(main_module, "get_ai_client_for_user", resolve)
    sign_out(test_client)
    sign_in(test_client, db_factory, email="tester@example.com", is_admin=False)

    response = test_client.get("/api/llm/status")

    assert response.status_code == 200
    assert response.json()["state"] == "online"
    assert probed == ["tester@example.com"]


def test_a_hosted_provider_without_a_key_is_not_a_usable_config():
    from sbpeye.ai import AIConfig

    user = User(id="u-3", email="x@y.z", password_hash="x", ai_provider="mistral")

    # Better to report "not configured" than to let the request reach the vendor and
    # come back 401 in the middle of a chat turn.
    assert AIConfig.for_user(user) is None


def test_the_default_provider_is_mistral():
    """LM Studio's default is a localhost port that exists on a laptop and nowhere else."""
    from sbpeye.ai import AIConfig, normalize_provider

    assert AIConfig().provider == "mistral"
    assert normalize_provider("") == "mistral"
    assert "mistral.ai" in AIConfig().base_url


def test_password_hashes_are_not_the_password():
    from sbpeye.auth import hash_password

    stored = hash_password("correct-horse-battery-staple")

    assert "correct-horse" not in stored
    assert stored.startswith("$argon2")
    assert verify_password(stored, "correct-horse-battery-staple")
    assert not verify_password(stored, "wrong")


def test_encryption_round_trips_and_fails_soft(monkeypatch):
    token = encrypt_secret("sk-value")
    assert decrypt_secret(token) == "sk-value"

    monkeypatch.setenv("SBPEYE_SECRET_KEY", "yet-another-secret-key-long-enough-here")
    # A rotated key makes stored keys unreadable. Treated as unset rather than raised on,
    # so the user is asked to re-enter one instead of meeting a 500 on every AI request.
    assert decrypt_secret(token) == ""
