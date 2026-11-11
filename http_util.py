"""The one HTTP helper of the Pacer. Every failed call raises HttpError, which is a RuntimeError: a Paperclip failure reaches
the governor log as the same `ERROR <message>` line it always did, and callers handle one exception class.

A response with status 2xx/3xx returns normally (urllib follows redirects); status 400 and up, connection failures,
timeouts and bodies that are not JSON raise HttpError."""
import json, time, urllib.error, urllib.request

TIMEOUT_S = 15
BODY_SNIPPET = 300


class HttpError(RuntimeError):
    def __init__(self, method, url, status=None, body="", reason=""):
        self.method, self.url, self.status, self.body = method, url, status, body
        detail = f"HTTP {status} {body}".rstrip() if status is not None else reason
        super().__init__(f"{method} {url} -> {detail}"[:400])


def _send(url, method, headers, data, timeout):
    try:
        req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            snippet = e.read().decode("utf-8", "replace")[:BODY_SNIPPET]
        except OSError:
            snippet = ""
        raise HttpError(method, url, e.code, snippet) from None
    except (urllib.error.URLError, OSError, ValueError) as e:  # URLError, timeouts (OSError), bad URL (ValueError)
        raise HttpError(method, url, reason=str(getattr(e, "reason", None) or e)) from None


def request_text(url, *, method="GET", headers=None, body=None, timeout=TIMEOUT_S, retries=0):
    """Response body decoded as UTF-8 text. `body` is bytes or str (sent as is). Retries only connection failures and
    5xx answers, one second apart; the last failure is raised."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    for attempt in range(retries + 1):
        try:
            return _send(url, method, headers, data, timeout)
        except HttpError as e:
            if attempt == retries or (e.status is not None and e.status < 500):
                raise
            time.sleep(1)


def request_json(url, *, method="GET", headers=None, body=None, timeout=TIMEOUT_S, retries=0):
    """Parsed JSON response (None for an empty body). `body` is any JSON-serialisable object, sent with Content-Type
    application/json. A body that is not JSON raises HttpError."""
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body)
        headers.setdefault("Content-Type", "application/json")
    text = request_text(url, method=method, headers=headers, body=data, timeout=timeout, retries=retries)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError as e:
        raise HttpError(method, url, reason=f"not JSON: {e}") from None
