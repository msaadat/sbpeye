"""Bounded SBP HTTP retries with a shared, cancellable request-start pacer."""

from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import threading
import time
from urllib.parse import urljoin

import requests

from ..sbp_urls import normalize_sbp_url as normalized_url


class Cancelled(RuntimeError):
    pass


class RequestPacer:
    def __init__(self, delay=0.5, cancel=None):
        if not 0 <= delay <= 10:
            raise ValueError("Request delay must be between 0 and 10 seconds")
        self.delay = delay
        self.cancel = cancel or threading.Event()
        self.lock = threading.Lock()
        self.next_start = 0.0

    def wait(self, seconds):
        if self.cancel.wait(max(0, seconds)):
            raise Cancelled("Cancellation requested")

    def pace(self, deadline):
        with self.lock:
            self.wait(min(max(0, self.next_start - time.monotonic()), max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                raise requests.Timeout("SBP operation deadline exceeded")
            self.next_start = time.monotonic() + self.delay


_local = threading.local()


@contextmanager
def http_job(pacer):
    previous = getattr(_local, "pacer", None)
    _local.pacer = pacer
    try:
        yield
    finally:
        _local.pacer = previous


def _transient(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        message = str(exc).lower()
        return not ("certificate" in message or "cert_verify" in message) and any(
            word in message for word in ("eof", "closed", "reset"))
    return isinstance(exc, (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError))


def _retry_after(value):
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError):
            return 0


def get(url, *, consume=None, pacer=None, request=None, deadline_seconds=300, **kwargs):
    """With consume, streamed failures retry from a fresh response and consumer state."""
    pacer = pacer or getattr(_local, "pacer", None) or RequestPacer()
    if request is None:
        if not hasattr(_local, "session"):
            _local.session = requests.Session()
            _local.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        request = _local.session.get
    deadline = time.monotonic() + deadline_seconds
    current = normalized_url(url)
    kwargs.pop("allow_redirects", None)
    kwargs.pop("timeout", None)
    buffer_body = not kwargs.get("stream", False)
    kwargs["stream"] = True
    for hop in range(6):
        redirect = None
        for attempt in range(4):
            response = None
            try:
                pacer.pace(deadline)
                remaining = deadline - time.monotonic()
                response = request(current, allow_redirects=False,
                                   timeout=(min(10, remaining), min(50, remaining)), **kwargs)
                status = response.status_code
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect has no destination")
                    redirect = normalized_url(urljoin(current, location))
                    response.close()
                    break
                if status in {429, 500, 502, 503, 504}:
                    wait = _retry_after(response.headers.get("Retry-After")) if status in {429, 503} else 0
                    if wait > 60 or attempt == 3:
                        response.raise_for_status()
                    response.close()
                    pacer.wait(min(max(wait, 0.5 * 2**attempt + random.uniform(0, 0.1)), max(0, deadline - time.monotonic())))
                    continue
                response.raise_for_status()
                if consume:
                    try:
                        return consume(response, deadline)
                    finally:
                        response.close()
                if buffer_body:
                    parts, size = [], 0
                    for part in response.iter_content(chunk_size=64 * 1024):
                        if time.monotonic() >= deadline:
                            raise requests.Timeout("SBP operation deadline exceeded")
                        size += len(part)
                        if size > 20 * 1024 * 1024:
                            raise ValueError("HTML response exceeds the 20 MiB limit")
                        parts.append(part)
                    response._content = b"".join(parts)
                    response._content_consumed = True
                    response.close()
                return response
            except Exception as exc:
                if response is not None:
                    response.close()
                if attempt == 3 or not _transient(exc):
                    raise
                pacer.wait(min(0.5 * 2**attempt + random.uniform(0, 0.1), max(0, deadline - time.monotonic())))
        if redirect is None:
            raise requests.Timeout("SBP retries exhausted")
        current = redirect
    raise ValueError("SBP document fetch exceeded the redirect limit")
