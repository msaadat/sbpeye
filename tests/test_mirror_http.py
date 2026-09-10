import requests
import pytest

from sbpeye.scraper.http import get, RequestPacer


class Response:
    def __init__(self, status=200, content=b"ok", headers=None):
        self.status_code, self.headers, self.content = status, headers or {}, content
        self.closed = False
    def close(self):
        self.closed = True
    def iter_content(self, **kwargs):
        yield self.content
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


def pacer(monkeypatch):
    value = RequestPacer(0)
    monkeypatch.setattr(value, "wait", lambda seconds: None)
    return value


def test_tls_eof_retries_then_success(monkeypatch):
    calls = []
    def request(*a, **kw):
        calls.append(kw)
        if len(calls) < 3:
            raise requests.exceptions.SSLError("SSLEOFError: EOF occurred in violation of protocol")
        return Response()
    result = get("https://www.sbp.org.pk/circulars/", request=request, pacer=pacer(monkeypatch))
    assert len(calls) == 3 and result.closed
    assert all(call["allow_redirects"] is False for call in calls)


@pytest.mark.parametrize("error", [requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"), requests.HTTPError("403")])
def test_terminal_failures_do_not_retry(monkeypatch, error):
    calls = []
    def request(*a, **kw):
        calls.append(1)
        raise error
    with pytest.raises(type(error)):
        get("https://www.sbp.org.pk/a", request=request, pacer=pacer(monkeypatch))
    assert len(calls) == 1


def test_retry_after_above_limit_is_terminal(monkeypatch):
    response = Response(429, headers={"Retry-After": "90"})
    with pytest.raises(requests.HTTPError):
        get("https://www.sbp.org.pk/a", request=lambda *a, **kw: response, pacer=pacer(monkeypatch))
    assert response.closed


def test_redirect_rejects_external_host(monkeypatch):
    response = Response(302, headers={"location": "https://evil.example/a"})
    with pytest.raises(ValueError, match="HTTPS"):
        get("https://www.sbp.org.pk/a", request=lambda *a, **kw: response, pacer=pacer(monkeypatch))
    assert response.closed


def test_stream_consumer_restarts_cleanly(monkeypatch, tmp_path):
    target = tmp_path / "file.part"
    responses = []
    def request(*args, **kwargs):
        value = Response()
        responses.append(value)
        return value
    def consume(response, deadline):
        with target.open("wb") as out:
            out.write(b"first")
            if len(responses) == 1:
                raise requests.exceptions.ChunkedEncodingError("stream broke")
            out.write(b"last")
    get("https://www.sbp.org.pk/a", request=request, consume=consume, stream=True, pacer=pacer(monkeypatch))
    assert target.read_bytes() == b"firstlast"
    assert len(responses) == 2 and all(response.closed for response in responses)
