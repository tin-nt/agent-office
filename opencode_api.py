"""Small, loopback-only client for the OpenCode V2 HTTP API.

The control plane deliberately uses the public HTTP API instead of patching or
embedding OpenCode.  The service registration written by OpenCode supplies the
loopback origin and its Basic-auth password; this module never accepts a
caller-supplied origin or credential and never sends the password anywhere
except that validated loopback origin.

No Authorization header, prompt, or response body is logged by this module.
Errors returned to callers are short, printable summaries.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

DEFAULT_USERNAME = "opencode"
MIN_VERSION = (2, 2, 0)
REQUEST_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ERROR_CHARS = 300

Transport = Callable[[str, str, Optional[bytes], Dict[str, str], float],
                     Tuple[int, Any]]


class OpenCodeAPIError(Exception):
    """A request error that can be returned to a local HTTP caller."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class OpenCodeTransportError(OpenCodeAPIError):
    """A connection-level failure that is safe to retry for idempotent calls."""


def _redact(value: Any) -> str:
    """Reduce an arbitrary runtime value to a short, safe summary."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = "".join(char if char.isprintable() or char == " " else " " for char in text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_ERROR_CHARS:
        text = text[:MAX_ERROR_CHARS] + "…"
    return text or "unspecified OpenCode error"


def _loopback_origin(value: Any) -> str:
    """Validate and normalize a loopback OpenCode origin."""
    if not isinstance(value, str) or not value:
        raise OpenCodeAPIError(503, "the OpenCode service registration has no URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise OpenCodeAPIError(503, "the OpenCode service URL is malformed") from exc
    if (
        parsed.scheme != "http"
        or hostname not in ("127.0.0.1", "localhost", "::1")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port is None
    ):
        raise OpenCodeAPIError(
            503,
            "the OpenCode service URL is not a loopback http origin with an explicit port",
        )
    host = "127.0.0.1" if hostname in ("localhost", "127.0.0.1") else "[::1]"
    return f"http://{host}:{port}"


def _registration_candidates() -> Iterable[Path]:
    """Yield likely OpenCode service registration files, most specific first."""
    seen = set()

    def add(path: Path) -> Iterable[Path]:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            yield path

    explicit = os.environ.get("OPENCODE_STATE_HOME")
    if explicit:
        yield from add(Path(explicit) / "opencode" / "service.json")
        yield from add(Path(explicit) / "service.json")
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        yield from add(Path(xdg) / "opencode" / "service.json")
    home = Path.home()
    yield from add(home / ".local" / "state" / "opencode" / "service.json")
    yield from add(home / ".local" / "state" / "opencode" / "service-dev.json")
    # A private config file has the password but not the origin.  It is only a
    # fallback for a separately supplied origin (see discover_registration).
    yield from add(home / ".config" / "opencode" / "service.json")


def read_registration(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read one service registration without logging its password."""
    candidates = [path] if path is not None else list(_registration_candidates())
    last_error: Optional[Exception] = None
    for candidate in candidates:
        try:
            raw = candidate.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            last_error = exc
            continue
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise OpenCodeAPIError(
                503,
                f"the OpenCode service registration at {candidate} is not valid JSON",
            ) from exc
        if not isinstance(data, dict):
            raise OpenCodeAPIError(
                503,
                f"the OpenCode service registration at {candidate} is not an object",
            )
        url = data.get("url")
        if not isinstance(url, str) or not url:
            # A private config file may only carry the password.  Keep looking
            # for a registration that names the origin.
            continue
        password = data.get("password")
        if password is not None and not isinstance(password, str):
            raise OpenCodeAPIError(
                503,
                f"the OpenCode service registration at {candidate} has an invalid password",
            )
        return {
            "url": _loopback_origin(url),
            "password": password or "",
            "version": data.get("version") if isinstance(data.get("version"), str) else "",
            "pid": data.get("pid"),
            "path": str(candidate),
        }
    if last_error is not None:
        raise OpenCodeAPIError(
            503,
            f"the OpenCode service registration could not be read: {type(last_error).__name__}",
        ) from last_error
    raise OpenCodeAPIError(
        503,
        "no OpenCode service registration was found; start OpenCode and try again",
    )


def _version_tuple(value: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _query_string(query: Optional[Dict[str, Any]]) -> str:
    if not query:
        return ""
    pairs: List[Tuple[str, str]] = []

    def flatten(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                flatten(f"{prefix}[{key}]" if prefix else str(key), item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                flatten(prefix, item)
        elif value is not None:
            pairs.append((prefix, str(value)))

    flatten("", query)
    return "?" + urlencode(pairs) if pairs else ""


def _reject_redirects() -> urllib.request.OpenerDirector:
    class RejectRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            return None

    # Local API prompts must not inherit HTTP_PROXY/HTTPS_PROXY from the user's
    # environment.  An empty ProxyHandler disables automatic proxy discovery.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirect())


def _urllib_transport(method: str, url: str, body: Optional[bytes],
                      headers: Dict[str, str], timeout: float) -> Tuple[int, Any]:
    request = urllib.request.Request(url, data=body, method=method)
    for name, value in headers.items():
        request.add_header(name, value)
    opener = _reject_redirects()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
            status = response.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES)
        except OSError:
            raw = b""
        status = exc.code
    except (TimeoutError, ConnectionResetError) as exc:
        raise OpenCodeTransportError(503, f"the OpenCode service exchange was interrupted: {exc}") from exc
    except urllib.error.URLError as exc:
        raise OpenCodeTransportError(503, f"the OpenCode service is unreachable: {exc.reason}") from exc
    except OSError as exc:
        raise OpenCodeTransportError(503, f"the OpenCode service exchange failed: {exc}") from exc
    data: Any = None
    if raw:
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            data = None
    return status, data


class _PersistentTransport:
    """Thread-local HTTP/1.1 connections to the loopback OpenCode service.

    Reusing a connection avoids a Windows/Python failure mode where a burst of
    short-lived loopback connections is reset by the host.  The loop and each
    request handler get their own connection, so one slow caller cannot block
    the others.
    """

    def __init__(self):
        self._local = threading.local()

    def _connection(self, parsed, timeout):
        connection = getattr(self._local, "connection", None)
        origin = getattr(self._local, "origin", None)
        if connection is not None and origin == (parsed.scheme, parsed.netloc):
            return connection
        self._close()
        connection = http.client.HTTPConnection(
            parsed.hostname, parsed.port, timeout=timeout,
        )
        self._local.connection = connection
        self._local.origin = (parsed.scheme, parsed.netloc)
        return connection

    def _close(self):
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        self._local.connection = None
        self._local.origin = None

    def __call__(self, method: str, url: str, body: Optional[bytes],
                 headers: Dict[str, str], timeout: float) -> Tuple[int, Any]:
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection = self._connection(parsed, timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
            if response.will_close:
                self._close()
        except (ConnectionError, TimeoutError, OSError, http.client.HTTPException) as exc:
            self._close()
            raise OpenCodeTransportError(
                503, f"the OpenCode service exchange was interrupted: {exc}"
            ) from exc
        data: Any = None
        if raw:
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                data = None
        return status, data


class OpenCodeAPI:
    """One validated connection to a running local OpenCode service."""

    def __init__(self, base_url: str, password: str = "", *,
                 username: Optional[str] = None,
                 transport: Optional[Transport] = None):
        self.base_url = _loopback_origin(base_url)
        self.username = username or os.environ.get("OPENCODE_SERVER_USERNAME") or DEFAULT_USERNAME
        self._password = password or ""
        self._transport: Transport = transport or _PersistentTransport()

    @classmethod
    def discover(cls, *, transport: Optional[Transport] = None,
                 registration: Optional[Path] = None) -> "OpenCodeAPI":
        record = read_registration(registration)
        client = cls(record["url"], record["password"], transport=transport)
        info = client.info()
        version = _version_tuple(info.get("version"))
        if version is None:
            raise OpenCodeAPIError(
                409,
                "the OpenCode service did not report a compatible version",
            )
        if version < MIN_VERSION:
            raise OpenCodeAPIError(
                409,
                f"OpenCode {info.get('version')} is too old; this control panel requires 2.2.0 or newer",
            )
        return client

    # -- transport ---------------------------------------------------------

    def request(self, method: str, path: str, body: Any = None,
                query: Optional[Dict[str, Any]] = None,
                timeout: float = REQUEST_TIMEOUT, retries: int = 0) -> Any:
        if not path.startswith("/"):
            raise ValueError("API path must start with /")
        url = self.base_url + path + _query_string(query)
        payload = None
        headers = {"Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._password:
            token = base64.b64encode(
                f"{self.username}:{self._password}".encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        attempts = max(1, min(int(retries), 5) + 1)
        last: Optional[OpenCodeTransportError] = None
        for attempt in range(attempts):
            try:
                status, data = self._transport(method, url, payload, headers, timeout)
                break
            except OpenCodeTransportError as exc:
                last = exc
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.2 * (attempt + 1))
        else:
            raise last or OpenCodeTransportError(503, "the OpenCode service is unreachable")
        if status >= 400:
            message = data
            if isinstance(data, dict):
                message = data.get("message") or data.get("error") or data.get("_tag")
            raise OpenCodeAPIError(status, _redact(message or f"OpenCode answered HTTP {status}"))
        return data

    @staticmethod
    def _data(value: Any) -> Any:
        if isinstance(value, dict) and "data" in value:
            return value["data"]
        return value

    # -- read --------------------------------------------------------------

    def info(self) -> Dict[str, Any]:
        data = self.request("GET", "/api/info", retries=3)
        if not isinstance(data, dict):
            raise OpenCodeAPIError(503, "the OpenCode service returned invalid info")
        return data

    def projects(self) -> List[Dict[str, Any]]:
        data = self._data(self.request("GET", "/api/project", retries=3))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def agents(self, directory: str) -> List[Dict[str, Any]]:
        data = self._data(self.request(
            "GET", "/api/agent", query={"location": {"directory": directory}}, retries=3
        ))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def models(self) -> List[Dict[str, Any]]:
        data = self._data(self.request("GET", "/api/model", retries=3))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def default_model(self) -> Optional[Dict[str, Any]]:
        data = self._data(self.request("GET", "/api/model/default", retries=3))
        return data if isinstance(data, dict) else None

    def commands(self, directory: str) -> List[Dict[str, Any]]:
        data = self._data(self.request(
            "GET", "/api/command", query={"location": {"directory": directory}}, retries=3
        ))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def sessions(self, directory: Optional[str] = None, limit: int = 200,
                 order: str = "desc") -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"limit": str(max(1, min(int(limit), 500))), "order": order}
        if directory:
            query["directory"] = directory
        data = self._data(self.request("GET", "/api/session", query=query, retries=3))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def session(self, session_id: str) -> Dict[str, Any]:
        data = self._data(self.request("GET", f"/api/session/{session_id}", retries=3))
        if not isinstance(data, dict):
            raise OpenCodeAPIError(503, "the OpenCode service returned an invalid session")
        return data

    def active_sessions(self) -> Dict[str, Any]:
        data = self._data(self.request("GET", "/api/session/active", retries=3))
        return data if isinstance(data, dict) else {}

    def messages(self, session_id: str, *, limit: int = 50, order: str = "desc",
                 cursor: Optional[str] = None, type: Optional[str] = None) -> Dict[str, Any]:
        query: Dict[str, Any] = {"limit": str(max(1, min(int(limit), 200))), "order": order}
        if cursor:
            query["cursor"] = cursor
        if type:
            query["type"] = type
        data = self._data(self.request("GET", f"/api/session/{session_id}/message", query=query, retries=3))
        if isinstance(data, dict):
            return data
        return {"data": data if isinstance(data, list) else [], "cursor": {"next": None, "previous": None}}

    def inbox(self, session_id: str) -> List[Dict[str, Any]]:
        data = self._data(self.request("GET", f"/api/session/{session_id}/inbox", retries=3))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def permissions(self, session_id: str) -> List[Dict[str, Any]]:
        data = self._data(self.request("GET", f"/api/session/{session_id}/permission", retries=3))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def forms(self, session_id: str) -> List[Dict[str, Any]]:
        data = self._data(self.request("GET", f"/api/session/{session_id}/form", retries=3))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    # -- write -------------------------------------------------------------

    def create_session(self, *, directory: str, title: str,
                       agent: Optional[str] = None,
                       model: Optional[Dict[str, Any]] = None,
                       session_id: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "title": title,
            "location": {"directory": directory},
        }
        if session_id:
            body["id"] = session_id
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = model
        try:
            data = self._data(self.request(
                "POST", "/api/session", body=body, retries=2,
            ))
        except OpenCodeAPIError as exc:
            # A retry can race the first request's successful response.  When
            # a client-supplied session ID already exists, treat it as the
            # same create operation instead of creating a second session.
            if session_id and exc.status in (409, 400):
                existing = self.session(session_id)
                if existing.get("id") == session_id:
                    return existing
            raise
        if not isinstance(data, dict):
            raise OpenCodeAPIError(503, "the OpenCode service did not return the created session")
        return data

    def prompt(self, session_id: str, text: str, *,
               metadata: Optional[Dict[str, Any]] = None,
               delivery: str = "queue",
               message_id: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"text": text, "delivery": delivery}
        if message_id:
            body["id"] = message_id
        if metadata:
            body["metadata"] = metadata
        try:
            data = self._data(self.request(
                "POST", f"/api/session/{session_id}/prompt", body=body,
            ))
        except OpenCodeAPIError as exc:
            # A retry after a lost response can find the same client-supplied
            # message already queued.  The caller only needs its identity.
            if message_id and exc.status == 409:
                return {"id": message_id}
            raise
        return data if isinstance(data, dict) else {}

    def command(self, session_id: str, name: str, text: str, *,
                delivery: str = "queue") -> Any:
        return self._data(self.request(
            "POST", f"/api/session/{session_id}/command",
            body={"name": name, "text": text, "delivery": delivery},
        ))

    def interrupt(self, session_id: str, *, resume: bool = False) -> Dict[str, Any]:
        data = self._data(self.request(
            "POST", f"/api/session/{session_id}/interrupt",
            query={"resume": "true" if resume else "false"}, retries=1,
        ))
        return data if isinstance(data, dict) else {}

    def reply_permission(self, session_id: str, request_id: str, decision: str,
                         message: Optional[str] = None) -> Any:
        body: Dict[str, Any] = {"decision": decision}
        if message:
            body["message"] = message
        try:
            return self._data(self.request(
                "POST", f"/api/session/{session_id}/permission/{request_id}/reply",
                body=body, retries=1,
            ))
        except OpenCodeAPIError as exc:
            if exc.status == 404:
                # The retry may have arrived after the first reply settled.
                return {}
            raise

    def reply_form(self, session_id: str, form_id: str, answer: Dict[str, Any]) -> Any:
        try:
            return self._data(self.request(
                "POST", f"/api/session/{session_id}/form/{form_id}/reply",
                body={"answer": answer}, retries=1,
            ))
        except OpenCodeAPIError as exc:
            if exc.status == 404:
                return {}
            raise
