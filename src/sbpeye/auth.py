"""Authentication: password hashing, signed session cookies, and the request guards.

Scope is a test deploy (deployment plan, 7.1): email and password, no verification mail,
no reset flow, no OAuth. Two boundaries are worth having — one between a known tester and
the internet, and one between a tester and the admin — and everything past that is out of
scope until this stops being a test.

There is no sessions table. A session is a signed cookie carrying the user id, and its
authority is `SBPEYE_SECRET_KEY`. That buys statelessness and costs revocation: a stolen
cookie stays valid until it expires, and the only way to invalidate every outstanding
session at once is to rotate the key. For a handful of known testers that is the right
trade; it would not be for a real service.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import uuid
from datetime import datetime

from argon2 import PasswordHasher
from cryptography.fernet import Fernet, InvalidToken
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .models import User

SESSION_COOKIE = "sbpeye_session"
SESSION_SALT = "sbpeye.session.v1"

# Fourteen days. Long enough that testers are not re-typing a password every day, short
# enough that a cookie lifted from a laptop does not work indefinitely.
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60

# Deliberately loose. This is a sanity check against typos, not an attempt to decide what
# a valid address is — that argument has no winner and every strict pattern rejects real
# addresses.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Eight, not twelve. Twelve is the better floor and this is a knowing trade for a test
# deploy with a handful of known accounts; it should go back up before the deployment
# stops being a test. There is no separate admin rule — this governs every account,
# including the seeded one, and a weaker floor for the most privileged account would be
# the wrong way round.
MIN_PASSWORD_LENGTH = 8

_hasher = PasswordHasher()


class AuthConfigError(RuntimeError):
    """The deployment is configured in a way that cannot be served safely."""


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    """Return the normalized address, or raise ValueError describing the problem."""
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("An email address is required.")
    if not _EMAIL_RE.match(normalized):
        raise ValueError("That does not look like an email address.")
    return normalized


def validate_password(password: str) -> str:
    """A length floor and nothing else.

    Composition rules (a digit, a symbol, a capital) push people towards `Password1!`
    and buy less than length does. The floor is the only rule worth enforcing here, and
    at eight characters it is a low one — see `MIN_PASSWORD_LENGTH`.
    """
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    return password


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def secret_key() -> str:
    """The cookie signing key.

    Refuses to invent one. A generated-per-boot key would silently log every user out on
    each restart, and a hardcoded fallback is worse than that — it would let anyone who
    has read this file mint a valid admin session on any deployment that forgot to set
    the variable.
    """
    key = (os.getenv("SBPEYE_SECRET_KEY") or "").strip()
    if not key:
        raise AuthConfigError(
            "SBPEYE_SECRET_KEY is not set. It signs session cookies; without it "
            "sessions cannot be issued or trusted. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`."
        )
    if len(key) < 32:
        raise AuthConfigError(
            "SBPEYE_SECRET_KEY is too short to sign sessions with; use at least 32 "
            "characters."
        )
    return key


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key(), salt=SESSION_SALT)


def issue_session(user: User) -> str:
    """Mint the cookie value for a signed-in user."""
    return _serializer().dumps({"uid": user.id})


def read_session(token: str | None) -> str | None:
    """The user id a cookie vouches for, or None if it does not.

    Every failure — tampered, expired, malformed, signed with a rotated key — returns
    None rather than raising. A caller cannot act differently on the difference, and
    telling an attacker which one it was is free information.
    """
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    except AuthConfigError:
        raise
    except Exception:
        logging.exception("Unexpected error reading a session cookie")
        return None
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("uid")
    return user_id if isinstance(user_id, str) and user_id else None


# --------------------------------------------------------------- secrets at rest


def _fernet() -> Fernet:
    """The box provider API keys are kept in.

    Keyed off `SBPEYE_SECRET_KEY` rather than a second secret, so there is one thing to
    set and one thing to protect. The consequence is worth stating plainly: **rotating
    `SBPEYE_SECRET_KEY` makes every stored API key undecryptable**, and each user has to
    re-enter theirs. It already logs everyone out, so the blast radius is the same event.

    What this protects against is a leaked `sbpeye_app.db` — a volume snapshot, a stray
    copy — without the environment variable. It does not protect a key from the running
    application or from someone who has both the file and the environment, and it is not
    meant to.
    """
    digest = hashlib.sha256(secret_key().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str | None) -> str:
    """The plaintext, or "" if it cannot be recovered.

    An unreadable value is treated as absent rather than raised on: the usual cause is a
    rotated secret key, and the useful behaviour then is "your key needs re-entering",
    not a 500 on every request that touches the provider.
    """
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logging.warning("A stored secret could not be decrypted; treating it as unset.")
        return ""


# --------------------------------------------------------------------------- users


def create_user(
    app_db: Session, *, email: str, password: str, is_admin: bool = False
) -> User:
    """Create a user, or raise ValueError with a message fit to show the caller."""
    normalized = validate_email(email)
    validate_password(password)

    existing = app_db.query(User).filter(User.email == normalized).first()
    if existing is not None:
        raise ValueError("A user with that email already exists.")

    user = User(
        id=str(uuid.uuid4()),
        email=normalized,
        password_hash=hash_password(password),
        is_admin=1 if is_admin else 0,
    )
    app_db.add(user)
    app_db.commit()
    app_db.refresh(user)
    return user


def authenticate(app_db: Session, email: str, password: str) -> User | None:
    """The user those credentials belong to, or None.

    A password check runs even when the address is unknown. Skipping it would make a
    miss measurably faster than a hit, which turns response time into an oracle for
    which addresses are registered.
    """
    normalized = normalize_email(email)
    user = app_db.query(User).filter(User.email == normalized).first()
    if user is None:
        _hasher.hash(secrets.token_urlsafe(16))
        return None
    if not verify_password(user.password_hash, password):
        return None

    user.last_login_at = datetime.utcnow()
    app_db.commit()
    return user


def seed_first_admin(app_db: Session) -> User | None:
    """Create the bootstrap admin from the environment, if there is no admin yet.

    Runs on every boot and does nothing once an admin exists, so leaving the variables
    set does not resurrect a deleted account or reset a changed password. Returns the
    user it created, or None when there was nothing to do.
    """
    if app_db.query(User).filter(User.is_admin == 1).first() is not None:
        return None

    email = (os.getenv("SBPEYE_ADMIN_EMAIL") or "").strip()
    password = os.getenv("SBPEYE_ADMIN_PASSWORD") or ""
    if not email or not password:
        logging.warning(
            "No admin user exists and SBPEYE_ADMIN_EMAIL / SBPEYE_ADMIN_PASSWORD are "
            "not both set, so none was created. Nobody can sign in until one is."
        )
        return None

    try:
        user = create_user(app_db, email=email, password=password, is_admin=True)
    except ValueError as exc:
        logging.error("Could not seed the first admin: %s", exc)
        return None

    logging.warning("Created the first admin user: %s", user.email)
    return user
