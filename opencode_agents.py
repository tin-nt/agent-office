"""Opt-in OpenCode agent control plane for the standalone Agent Office server.

The observer adapters stay observation-only.  This module is a separate,
explicitly enabled control plane: it binds one OpenCode session to one
allowlisted project, exposes a small chat/control API, and can keep a session
working autonomously until the operator stops it or its token budget is spent.

The control plane uses OpenCode's public V2 HTTP API.  It never edits OpenCode
configuration, never approves permissions automatically, and never stores
credentials or transcripts.  Bindings and loop counters live in
``agents.json`` inside the office data directory; the conversation itself
remains in OpenCode.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from .opencode_api import OpenCodeAPI, OpenCodeAPIError, OpenCodeTransportError
except ImportError:
    from opencode_api import OpenCodeAPI, OpenCodeAPIError, OpenCodeTransportError

MAX_BINDINGS = 64
MAX_GOAL_CHARS = 4000
MAX_PROMPT_CHARS = 8192
MAX_TITLE_CHARS = 200
MAX_AGENT_ID_CHARS = 256
MAX_PROJECT_PATH_CHARS = 4096
MAX_TOKENS = 100_000_000
MAX_TURNS = 10_000
MAX_DURATION_S = 7 * 24 * 3600
MAX_ACTIVE_AGENTS = 16
DEFAULT_MAX_TOKENS = 2_000_000
DEFAULT_MAX_TURNS = 100
DEFAULT_MAX_DURATION_S = 8 * 3600
LOOP_INTERVAL = 2.0
CONTINUATION_COOLDOWN = 1.5
STALL_LIMIT = 3
RECENT_MESSAGE_LIMIT = 50
MAX_REQUEST_IDS = 256
MAX_ERROR_CHARS = 300

ACTIVE_STATUSES = frozenset({"starting", "running", "waiting_input"})
TERMINAL_STATUSES = frozenset({"complete", "error", "budget_exhausted", "limit_reached", "stopped"})
OFFICE_KINDS = frozenset({"goal", "continuation", "budget_wrapup"})

COMPLETION_RE = re.compile(r"^\s*GOAL_COMPLETE\s*:\s*(.+?)\s*$", re.IGNORECASE)
BLOCKED_RE = re.compile(r"^\s*GOAL_BLOCKED\s*:\s*(.+?)\s*$", re.IGNORECASE)
BRACKET_COMPLETE_RE = re.compile(r"^\s*\[goal:complete\]\s*(.*?)\s*$", re.IGNORECASE)
BRACKET_BLOCKED_RE = re.compile(r"^\s*\[goal:blocked\]\s*(.*?)\s*$", re.IGNORECASE)


class AgentError(Exception):
    """A request error that can be returned to a local HTTP caller."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _redact(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = "".join(char if char.isprintable() or char == " " else " " for char in text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_ERROR_CHARS:
        text = text[:MAX_ERROR_CHARS] + "…"
    return text or "unspecified agent error"


def _text(value: Any, limit: int, *, multiline: bool = False) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False
    allowed = "\n\r\t" if multiline else ""
    return not any(
        (ord(char) < 32 and char not in allowed)
        or ord(char) == 127
        or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    )


def _optional_text(value: Any, limit: int, *, multiline: bool = False) -> bool:
    return value is None or (isinstance(value, str) and len(value) <= limit
                             and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
                             and (not value or _text(value, limit, multiline=multiline)))


def _integer(value: Any, minimum: int, maximum: int) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and minimum <= value <= maximum)


def _canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return str(parsed) == value.lower()


def _norm(path: Any) -> str:
    return os.path.normcase(str(Path(path)))


def _short(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit]


def _version_key(value: Any) -> str:
    return _short(value, 128)


def _marker(text: Any) -> Optional[Dict[str, str]]:
    """Return a completion/blocked marker outside fenced or indented code."""
    if not isinstance(text, str) or not text:
        return None
    fenced = False
    fence = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            token = stripped[:3]
            if not fenced:
                fenced = True
                fence = token
            elif token == fence:
                fenced = False
            continue
        if fenced:
            continue
        if line.startswith("    ") or line.startswith("\t"):
            continue
        match = COMPLETION_RE.match(line) or BRACKET_COMPLETE_RE.match(line)
        if match:
            return {"kind": "complete", "reason": _short(match.group(1), 500)}
        match = BLOCKED_RE.match(line) or BRACKET_BLOCKED_RE.match(line)
        if match:
            return {"kind": "blocked", "reason": _short(match.group(1), 500)}
    return None


def _message_text(message: Dict[str, Any]) -> str:
    parts = message.get("content")
    if not isinstance(parts, list):
        return ""
    chunks = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


def _message_tools(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    parts = message.get("content")
    if not isinstance(parts, list):
        return []
    tools = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "tool":
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        tool = {
            "id": _short(part.get("id"), 128),
            "name": _short(part.get("name"), 128),
            "status": _short(state.get("status"), 32),
        }
        if isinstance(state.get("input"), dict):
            tool["input"] = _short(state.get("input"), 1200)
        if state.get("status") == "error" and isinstance(state.get("error"), dict):
            tool["error"] = _short(
                state["error"].get("message") or state["error"].get("name") or "tool error",
                300,
            )
        elif state.get("status") == "completed" and isinstance(state.get("content"), list):
            chunks = []
            for item in state["content"]:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
            if chunks:
                tool["output"] = _short("\n".join(chunks), 1200)
        tools.append(tool)
    return tools


def _message_role(message: Dict[str, Any]) -> str:
    value = message.get("type")
    return value if isinstance(value, str) and value else "unknown"


def _office_metadata(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return None
    office = metadata.get("agent_office")
    return office if isinstance(office, dict) else None


def _is_office_message(message: Dict[str, Any]) -> bool:
    office = _office_metadata(message)
    return bool(office and office.get("kind") in OFFICE_KINDS)


def _session_tokens(session: Dict[str, Any]) -> tuple[int, int, bool]:
    """Return (fresh, total, reported) token counts for one session."""
    tokens = session.get("tokens")
    if not isinstance(tokens, dict):
        return 0, 0, False
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}

    def number(value: Any) -> Optional[int]:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    values = [
        number(tokens.get("input")),
        number(tokens.get("output")),
        number(tokens.get("reasoning")),
        number(cache.get("read")),
        number(cache.get("write")),
    ]
    if all(value is None for value in values):
        return 0, 0, False
    input_, output, reasoning, read, write = (value or 0 for value in values)
    fresh = input_ + write + output + reasoning
    total = fresh + read
    return fresh, total, True


def _session_cost(session: Dict[str, Any]) -> float:
    value = session.get("cost")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0.0


class BindingStore:
    """Atomic, versioned storage for bindings and loop counters."""

    VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._bindings: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self.load()

    def load(self) -> None:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AgentError(503, f"agent bindings could not be read: {type(exc).__name__}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise AgentError(
                503,
                "agent bindings could not be parsed; the file is preserved. "
                "Repair or remove agents.json to reset the binding list.",
            ) from exc
        if not isinstance(data, dict) or data.get("version") != self.VERSION \
                or not isinstance(data.get("bindings"), list):
            raise AgentError(
                503,
                "agent bindings have an unsupported format; the file is preserved. "
                "Repair or remove agents.json to reset the binding list.",
            )
        for item in data["bindings"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise AgentError(503, "agent bindings contain an invalid record")
            self._bindings[item["id"]] = copy.deepcopy(item)
            self._order.append(item["id"])
        if len(self._order) > MAX_BINDINGS:
            raise AgentError(503, "agent bindings exceed the supported limit")

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": self.VERSION,
                "bindings": [self._bindings[identity] for identity in self._order],
            }
            raw = json.dumps(payload, indent=2, ensure_ascii=True, allow_nan=False).encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix="agent-office-agents-", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(self._bindings[identity]) for identity in self._order]

    def get(self, identity: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self._bindings.get(identity)
            return copy.deepcopy(value) if value is not None else None

    def put(self, binding: Dict[str, Any], *, save: bool = True) -> None:
        with self._lock:
            identity = binding["id"]
            if identity not in self._bindings and len(self._order) >= MAX_BINDINGS:
                raise AgentError(409, "the agent binding limit has been reached")
            if identity not in self._bindings:
                self._order.append(identity)
            self._bindings[identity] = copy.deepcopy(binding)
            if save:
                self.save()

    def remove(self, identity: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self._bindings.pop(identity, None)
            if value is not None:
                self._order.remove(identity)
                self.save()
            return copy.deepcopy(value) if value is not None else None


class OpenCodeAgents:
    """Bind OpenCode sessions to allowlisted projects and run their loop."""

    def __init__(self, project_roots: Iterable[os.PathLike[str] | str], *,
                 max_active: int = 4, office_dir: Optional[Path] = None,
                 api: Optional[OpenCodeAPI] = None,
                 interval: float = LOOP_INTERVAL):
        roots = []
        seen = set()
        for raw in project_roots:
            path = Path(raw)
            if not path.is_absolute():
                raise ValueError(f"project root must be an absolute path: {raw}")
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"project root is unavailable: {raw}") from exc
            if not resolved.is_dir():
                raise ValueError(f"project root is not a directory: {raw}")
            key = _norm(resolved)
            if key in seen:
                raise ValueError(f"project root is listed more than once: {resolved}")
            seen.add(key)
            roots.append(resolved)
        if not roots:
            raise ValueError("at least one project root is required")

        self.token = secrets.token_urlsafe(32)
        self.instance_id = str(uuid.uuid4())
        self._roots = roots
        self._max_active = max(1, min(int(max_active), MAX_ACTIVE_AGENTS))
        self._interval = max(0.5, float(interval))
        self._api = api
        self._api_explicit = api is not None
        self._connected = api is not None
        self._server_info: Dict[str, Any] = {}
        self._connection_error: Optional[str] = None
        self._last_discovery_at = 0.0
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._request_ids: Dict[str, tuple[str, str, Dict[str, Any]]] = {}
        self._catalog_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._project_cache: tuple[float, List[Dict[str, Any]]] | None = None
        self._model_cache: tuple[float, List[Dict[str, Any]]] | None = None
        self._last_persist_at = 0.0
        self._load_error: Optional[str] = None
        try:
            self._store = BindingStore(
                (Path(office_dir) if office_dir is not None else Path.home() / ".hermes" / "pixel-office")
                / "agents.json"
            )
        except AgentError as exc:
            self._store = BindingStore.__new__(BindingStore)
            self._store.path = (Path(office_dir) if office_dir is not None else Path.home() / ".hermes" / "pixel-office") / "agents.json"
            self._store._lock = threading.RLock()
            self._store._bindings = {}
            self._store._order = []
            self._load_error = exc.message

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        if self._load_error:
            return
        # A control plane restart is not proof that a previous turn finished.
        # Pause every loop and require an explicit resume.
        changed = False
        for binding in self._store.all():
            if binding.get("status") in ACTIVE_STATUSES:
                binding["status"] = "paused"
                binding["pause_reason"] = "restart"
                binding["updated_at"] = time.time()
                self._store.put(binding, save=False)
                changed = True
        if changed:
            self._store.save()
        self._thread = threading.Thread(
            target=self._loop, name="agent-office-opencode-agents", daemon=True
        )
        self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, timeout))
        if not self._load_error:
            try:
                self._store.save()
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                api = self._require_api()
            except AgentError:
                self._pause_for_disconnect()
                self._stop.wait(self._interval)
                continue
            for binding in self._store.all():
                if self._stop.is_set():
                    break
                if binding.get("status") not in ("starting", "running", "waiting_input"):
                    continue
                try:
                    self._tick(api, binding["id"])
                except OpenCodeAPIError as exc:
                    self._record_api_error(binding["id"], exc)
                except AgentError as exc:
                    self._record_api_error(binding["id"], exc)
                except Exception as exc:
                    self._record_api_error(
                        binding["id"], AgentError(500, f"agent loop failed: {type(exc).__name__}")
                    )
            if time.time() - self._last_persist_at >= 5.0:
                try:
                    self._store.save()
                    self._last_persist_at = time.time()
                except Exception:
                    pass
            self._stop.wait(self._interval)

    def _pause_for_disconnect(self) -> None:
        changed = False
        for binding in self._store.all():
            if binding.get("status") in ACTIVE_STATUSES:
                binding["status"] = "paused"
                binding["pause_reason"] = "disconnected"
                binding["error"] = self._connection_error or "OpenCode service is unavailable"
                binding["updated_at"] = time.time()
                self._store.put(binding, save=False)
                changed = True
        if changed:
            try:
                self._store.save()
            except Exception:
                pass

    def _record_api_error(self, binding_id: str, exc: Exception) -> None:
        status = getattr(exc, "status", 500)
        message = getattr(exc, "message", str(exc))
        transport = isinstance(exc, OpenCodeTransportError) or status == 503
        with self._lock:
            binding = self._store.get(binding_id)
            if binding is None:
                return
            count = int(binding.get("consecutive_errors") or 0) + 1
            binding["consecutive_errors"] = count
            binding["error"] = _redact(message)
            binding["updated_at"] = time.time()
            if transport and count < 3:
                # A transient loopback reset should not end a long run.  Keep
                # the loop status and retry on the next tick; the transport
                # client already retries idempotent reads.
                self._store.put(binding, save=True)
                return
            if status in (401, 403, 404, 409) or transport:
                binding["status"] = "paused"
                binding["pause_reason"] = "disconnected" if transport else "error"
            self._store.put(binding, save=True)

    # -- discovery ---------------------------------------------------------

    def _try_api(self) -> tuple[Optional[OpenCodeAPI], Optional[str]]:
        with self._lock:
            if self._api is not None:
                return self._api, None
            if self._connection_error and time.time() - self._last_discovery_at < 5.0:
                return None, self._connection_error
        try:
            api = OpenCodeAPI.discover()
        except OpenCodeAPIError as exc:
            with self._lock:
                self._connected = False
                self._connection_error = exc.message
                self._last_discovery_at = time.time()
            return None, exc.message
        with self._lock:
            self._api = api
            self._connected = True
            self._connection_error = None
            self._server_info = api.info()
        return api, None

    def _require_api(self) -> OpenCodeAPI:
        api, error = self._try_api()
        if api is None:
            raise AgentError(503, error or "OpenCode service is unavailable")
        return api

    # -- validation --------------------------------------------------------

    def _resolve_project(self, raw: Any) -> Path:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_PROJECT_PATH_CHARS:
            raise AgentError(400, "project_directory must be a non-empty absolute path")
        path = Path(raw)
        if not path.is_absolute():
            raise AgentError(400, "project_directory must be an absolute path")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise AgentError(400, "project_directory is unavailable") from None
        if not resolved.is_dir():
            raise AgentError(400, "project_directory is not a directory")
        if not self._allowed(resolved):
            raise AgentError(403, "project_directory is outside the configured project roots")
        return resolved

    def _allowed(self, resolved: Path) -> bool:
        key = _norm(resolved)
        for root in self._roots:
            root_key = _norm(root)
            if key == root_key or key.startswith(root_key + os.sep):
                return True
        return False

    def _project_key(self, path: Any) -> str:
        return _norm(path)

    def _active_binding(self, project_key: str, *, exclude: Optional[str] = None) -> Optional[Dict[str, Any]]:
        for binding in self._store.all():
            if binding["id"] == exclude:
                continue
            if self._project_key(binding["project_directory"]) != project_key:
                continue
            if binding.get("status") in ACTIVE_STATUSES:
                return binding
        return None

    def _active_count(self) -> int:
        return sum(1 for binding in self._store.all() if binding.get("status") in ACTIVE_STATUSES)

    # -- status / catalog --------------------------------------------------

    def status(self, *, refresh: bool = False) -> Dict[str, Any]:
        api, error = self._try_api()
        projects: List[Dict[str, Any]] = []
        models: List[Dict[str, Any]] = []
        if api is not None:
            try:
                projects = self._projects(api, refresh=refresh)
                models = self._models(api, refresh=refresh)
            except OpenCodeAPIError as exc:
                error = exc.message
        bindings = [self._summary(binding) for binding in self._store.all()]
        agents = []
        for value in self._catalog_cache.values():
            for agent in value[1].get("agents", []):
                if agent.get("id") not in {item.get("id") for item in agents}:
                    agents.append(agent)
        return {
            "enabled": True,
            "connected": api is not None,
            "error": error or self._load_error,
            "server": {
                "url": self._server_info.get("urls", [None])[0] if isinstance(self._server_info.get("urls"), list) else None,
                "version": self._server_info.get("version"),
            } if self._server_info else None,
            "projects": projects,
            "agents": agents,
            "models": models,
            "bindings": bindings,
            "limits": {
                "max_active_agents": self._max_active,
                "max_bindings": MAX_BINDINGS,
                "max_prompt_chars": MAX_PROMPT_CHARS,
                "max_goal_chars": MAX_GOAL_CHARS,
                "max_tokens": MAX_TOKENS,
                "max_turns": MAX_TURNS,
                "max_duration_s": MAX_DURATION_S,
            },
            "token": self.token,
            "instance_id": self.instance_id,
        }

    def catalog(self, project: Any, *, refresh: bool = False) -> Dict[str, Any]:
        resolved = self._resolve_project(project)
        key = _norm(resolved)
        now = time.time()
        if not refresh:
            cached = self._catalog_cache.get(key)
            if cached is not None and now - cached[0] < 30.0:
                return cached[1]
        api = self._require_api()
        try:
            raw_agents = api.agents(str(resolved))
            raw_commands = api.commands(str(resolved))
            models = self._models(api, refresh=refresh)
            default = api.default_model()
        except OpenCodeAPIError as exc:
            raise AgentError(exc.status, exc.message) from exc
        agents = []
        for item in raw_agents:
            identity = item.get("id")
            if not isinstance(identity, str) or not identity:
                continue
            if item.get("hidden") is True or identity in {"compaction", "title", "summary"}:
                continue
            if item.get("mode") not in ("primary", "all"):
                continue
            agents.append({
                "id": identity,
                "name": _short(item.get("name") or identity, 200),
                "description": _short(item.get("description"), 500),
                "model": item.get("model") if isinstance(item.get("model"), dict) else None,
            })
        commands = []
        for item in raw_commands:
            name = item.get("name")
            if isinstance(name, str) and name:
                commands.append({
                    "name": name,
                    "description": _short(item.get("description"), 500),
                })
        result = {
            "project": {"path": str(resolved), "name": resolved.name or str(resolved)},
            "agents": agents,
            "models": models,
            "commands": commands,
            "default_model": self._model_ref(default) if isinstance(default, dict) else None,
        }
        with self._lock:
            self._catalog_cache[key] = (now, result)
        return result

    def _projects(self, api: OpenCodeAPI, *, refresh: bool = False) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            if not refresh and self._project_cache is not None and now - self._project_cache[0] < 30.0:
                return copy.deepcopy(self._project_cache[1])
        try:
            raw = api.projects()
        except OpenCodeAPIError as exc:
            raise AgentError(exc.status, exc.message) from exc
        found: Dict[str, Dict[str, Any]] = {}
        for item in raw:
            canonical = item.get("canonical")
            if not isinstance(canonical, str):
                continue
            try:
                resolved = Path(canonical).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved.is_dir() or not self._allowed(resolved):
                continue
            key = _norm(resolved)
            found[key] = {
                "id": _short(item.get("id"), 128),
                "path": str(resolved),
                "name": resolved.name or str(resolved),
                "source": "opencode",
                "vcs": _short(item.get("vcs"), 32) if item.get("vcs") else None,
            }
        for directory in self._discover_directories():
            key = _norm(directory)
            if key not in found:
                found[key] = {
                    "id": None,
                    "path": str(directory),
                    "name": directory.name or str(directory),
                    "source": "filesystem",
                    "vcs": None,
                }
        projects = sorted(found.values(), key=lambda item: (item["name"].lower(), item["path"].lower()))
        with self._lock:
            self._project_cache = (now, copy.deepcopy(projects))
        return projects

    def _discover_directories(self) -> List[Path]:
        markers = (
            ".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
            "README.md", "requirements.txt", "composer.json", "Gemfile", ".opencode",
        )
        ignored = {".git", ".opencode", ".venv", "venv", "node_modules", "__pycache__",
                   "dist", "build", ".next", ".pytest_cache"}
        found = []
        for root in self._roots:
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_dir() or entry.name.startswith(".") or entry.name in ignored:
                        continue
                    resolved = entry.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if not self._allowed(resolved):
                    continue
                if any((resolved / marker).exists() for marker in markers):
                    found.append(resolved)
        return found

    def _models(self, api: OpenCodeAPI, *, refresh: bool = False) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            if not refresh and self._model_cache is not None and now - self._model_cache[0] < 30.0:
                return copy.deepcopy(self._model_cache[1])
        try:
            raw = api.models()
        except OpenCodeAPIError as exc:
            raise AgentError(exc.status, exc.message) from exc
        models = []
        for item in raw:
            if item.get("enabled") is not True or item.get("type") not in ("chat", None):
                continue
            capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
            if capabilities.get("tools") is not True:
                continue
            provider = item.get("providerID")
            identity = item.get("id") or item.get("modelID")
            if not isinstance(provider, str) or not isinstance(identity, str):
                continue
            variants = []
            for variant in item.get("variants") or []:
                if isinstance(variant, dict) and isinstance(variant.get("id"), str):
                    variants.append(variant["id"])
            limit = item.get("limit") if isinstance(item.get("limit"), dict) else {}
            models.append({
                "providerID": provider,
                "id": identity,
                "name": _short(item.get("name") or identity, 200),
                "variants": variants,
                "limit": {
                    "context": limit.get("context"),
                    "output": limit.get("output"),
                },
                "cost": item.get("cost") if isinstance(item.get("cost"), list) else [],
            })
        models.sort(key=lambda item: (item["name"].lower(), item["providerID"], item["id"]))
        with self._lock:
            self._model_cache = (now, copy.deepcopy(models))
        return models

    @staticmethod
    def _model_ref(value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict):
            return None
        provider = value.get("providerID")
        identity = value.get("id") or value.get("modelID")
        if not isinstance(provider, str) or not isinstance(identity, str):
            return None
        ref = {"providerID": provider, "id": identity}
        if isinstance(value.get("variant"), str) and value["variant"]:
            ref["variant"] = value["variant"]
        return ref

    # -- create / detail ---------------------------------------------------

    def create(self, payload: Any) -> Dict[str, Any]:
        clean = self._validate_create(payload)
        fingerprint = json.dumps(
            {key: value for key, value in clean.items() if key != "request_id"},
            sort_keys=True, ensure_ascii=True, default=str,
        )
        api = self._require_api()
        resolved = self._resolve_project(clean["project_directory"])
        project_key = self._project_key(resolved)
        previous_id: Optional[str] = None
        with self._lock:
            if self._load_error:
                raise AgentError(503, self._load_error)
            if clean.get("request_id"):
                previous = self._request_ids.get(clean["request_id"])
                if previous is not None:
                    if previous[1] != fingerprint:
                        raise AgentError(409, "request_id was already used for a different request")
                    previous_id = previous[0]
            if previous_id is None:
                if self._active_count() >= self._max_active:
                    raise AgentError(409, "the agent control plane is at its active limit")
                if self._active_binding(project_key) is not None:
                    raise AgentError(409, "another agent is already active in this project")
        if previous_id is not None:
            if self._store.get(previous_id) is None:
                raise AgentError(409, "request_id was already used for a different request")
            return self.detail(previous_id)
        try:
            catalog = self.catalog(str(resolved))
        except AgentError:
            catalog = {"agents": [], "commands": []}
        if clean["agent_id"] not in {item["id"] for item in catalog.get("agents", [])}:
            # An explicitly supplied agent may belong to a location that has
            # not been loaded yet; let the runtime validate it if the catalog
            # is unavailable, but reject an unknown agent when it is available.
            if catalog.get("agents"):
                raise AgentError(400, "the selected agent is not available in this project")
        if (clean.get("start_with") or {}).get("kind") == "command":
            names = {item["name"] for item in catalog.get("commands", [])}
            if names and clean["start_with"]["name"] not in names:
                raise AgentError(400, "the selected command is not available in this project")

        session = None
        attached = clean.get("session_id")
        try:
            if attached:
                session = api.session(attached)
                directory = (session.get("location") or {}).get("directory")
                if not isinstance(directory, str) or _norm(Path(directory).resolve(strict=True)) != _norm(resolved):
                    raise AgentError(409, "the selected session belongs to a different project")
            else:
                session = api.create_session(
                    directory=str(resolved),
                    title=clean["title"],
                    agent=clean["agent_id"],
                    model=clean["model"],
                    session_id="ses_" + secrets.token_hex(12),
                )
        except OpenCodeAPIError as exc:
            raise AgentError(exc.status, exc.message) from exc
        except (OSError, RuntimeError):
            raise AgentError(409, "the selected session's project directory is unavailable") from None

        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise AgentError(503, "OpenCode did not return a session id")

        baseline_fresh, baseline_total, baseline_cost, _ = self._scope_metrics(api, session, resolved)
        now = time.time()
        binding = {
            "id": uuid.uuid4().hex,
            "session_id": session_id,
            "project_directory": str(resolved),
            "project_name": resolved.name or str(resolved),
            "agent_id": clean["agent_id"],
            "model": clean["model"],
            "title": clean["title"],
            "goal": clean["goal"],
            "mode": clean["mode"],
            "completion": clean["completion"],
            "max_tokens": clean["max_tokens"],
            "max_turns": clean["max_turns"],
            "max_duration_s": clean["max_duration_s"],
            "status": "paused" if not clean["start"] else "starting",
            "pause_reason": None if clean["start"] else "user",
            "turns": 0,
            "token_baseline": baseline_fresh,
            "token_baseline_total": baseline_total,
            "cost_baseline": baseline_cost,
            "tokens_used": 0,
            "tokens_total": 0,
            "cost_usd": 0.0,
            "tokens_reported": True,
            "last_message_id": None,
            "last_assistant_message_id": None,
            "last_user_message_id": None,
            "last_processed_user_message_id": None,
            "last_continuation_at": None,
            "last_continuation_inbox_id": None,
            "last_continuation_message_id": None,
            "last_activity_at": now,
            "started_at": now if clean["start"] else None,
            "created_at": now,
            "updated_at": now,
            "goal_completed_at": None,
            "completion_evidence": None,
            "stall_count": 0,
            "stall_message_id": None,
            "consecutive_errors": 0,
            "budget_wrapup_sent": False,
            "initial_sent": False,
            "start_with": clean.get("start_with"),
            "error": None,
        }
        with self._lock:
            self._store.put(binding, save=True)
            if clean.get("request_id"):
                self._remember_request(clean["request_id"], binding["id"], fingerprint)
        if clean["start"]:
            try:
                self._send_initial(api, binding)
            except (OpenCodeAPIError, AgentError) as exc:
                self._update(binding["id"], status="error", pause_reason="error",
                             error=_redact(getattr(exc, "message", str(exc))))
        return self.detail(binding["id"])

    def detail(self, binding_id: str) -> Dict[str, Any]:
        binding = self._store.get(binding_id)
        if binding is None:
            raise AgentError(404, "agent binding not found")
        api, error = self._try_api()
        pending_permissions: List[Dict[str, Any]] = []
        pending_forms: List[Dict[str, Any]] = []
        session = None
        if api is not None:
            try:
                session = api.session(binding["session_id"])
                pending_permissions = api.permissions(binding["session_id"])
                pending_forms = api.forms(binding["session_id"])
            except OpenCodeAPIError as exc:
                error = exc.message
        result = self._summary(binding)
        result["session"] = self._session_summary(session) if isinstance(session, dict) else None
        result["permissions"] = [self._permission_summary(item) for item in pending_permissions]
        result["forms"] = [self._form_summary(item) for item in pending_forms]
        result["error"] = error or binding.get("error")
        return result

    def messages(self, binding_id: str, query: Dict[str, Any]) -> Dict[str, Any]:
        binding = self._store.get(binding_id)
        if binding is None:
            raise AgentError(404, "agent binding not found")
        api = self._require_api()
        limit = query.get("limit", 50)
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            raise AgentError(400, "limit must be an integer") from None
        order = "asc" if query.get("order") == "asc" else "desc"
        cursor = query.get("cursor") if isinstance(query.get("cursor"), str) and query.get("cursor") else None
        include_office = query.get("include_office") in ("1", "true", "yes")
        try:
            response = api.messages(binding["session_id"], limit=limit, order=order, cursor=cursor)
        except OpenCodeAPIError as exc:
            raise AgentError(exc.status, exc.message) from exc
        messages = []
        for item in response.get("data", []):
            if not isinstance(item, dict):
                continue
            if not include_office and _is_office_message(item):
                continue
            messages.append(self._normalize_message(item))
        return {"messages": messages, "cursor": response.get("cursor") or {"next": None, "previous": None}}

    def _normalize_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        role = _message_role(item)
        result = {
            "id": _short(item.get("id"), 128),
            "role": role,
            "time": item.get("time") if isinstance(item.get("time"), dict) else {},
        }
        if role == "user":
            result["text"] = _short(item.get("text"), 20000)
            result["office"] = _is_office_message(item)
            if _office_metadata(item):
                result["office_kind"] = _short(_office_metadata(item).get("kind"), 32)
        elif role == "assistant":
            result["text"] = _short(_message_text(item), 20000)
            content = item.get("content") if isinstance(item.get("content"), list) else []
            result["reasoning"] = _short("\n".join(
                part.get("text") for part in content
                if isinstance(part, dict) and part.get("type") == "reasoning" and isinstance(part.get("text"), str)
            ), 8000)
            result["tools"] = _message_tools(item)
            result["finish"] = _short(item.get("finish"), 32)
            result["tokens"] = item.get("tokens") if isinstance(item.get("tokens"), dict) else None
            result["cost"] = item.get("cost") if isinstance(item.get("cost"), (int, float)) else None
            if isinstance(item.get("error"), dict):
                result["error"] = _short(
                    item["error"].get("message") or item["error"].get("name") or "assistant error",
                    300,
                )
            elif isinstance(item.get("error"), str) and item["error"]:
                result["error"] = _short(item["error"], 300)
        else:
            result["text"] = _short(item.get("text") or item.get("description"), 4000)
        return result

    # -- chat / commands ---------------------------------------------------

    def prompt(self, binding_id: str, payload: Any) -> Dict[str, Any]:
        clean = self._validate_prompt(payload)
        binding = self._store.get(binding_id)
        if binding is None:
            raise AgentError(404, "agent binding not found")
        request_id = clean.get("request_id")
        if request_id:
            with self._lock:
                previous = self._request_ids.get(request_id)
                if previous is not None:
                    if previous[0] != binding_id:
                        raise AgentError(409, "request_id was already used for a different agent")
                    if previous[1] != clean["text"]:
                        raise AgentError(409, "request_id was already used for a different message")
                    return self.detail(previous[0])
        text = clean["text"].strip()
        command = self._office_command(text)
        if command is not None:
            return self._run_office_command(binding_id, command)
        api = self._require_api()
        with self._send_lock:
            current = self._store.get(binding_id)
            if current is None:
                raise AgentError(404, "agent binding not found")
            if current.get("status") in ("complete", "budget_exhausted", "limit_reached", "error"):
                raise AgentError(409, "this agent is not running; resume it before sending a message")
            if current.get("status") in ACTIVE_STATUSES:
                self._update(binding_id, status="paused", pause_reason="user", save=True)
            try:
                project_command = self._match_project_command(api, current, text)
                if project_command is not None:
                    name, arguments = project_command
                    api.command(
                        current["session_id"], name, arguments,
                        delivery=clean.get("delivery") or "queue",
                    )
                else:
                    api.prompt(
                        current["session_id"], clean["text"],
                        metadata={"agent_office": {"binding_id": binding_id, "kind": "chat"}},
                        delivery=clean.get("delivery") or "queue",
                        message_id="msg_" + secrets.token_hex(12),
                    )
            except OpenCodeAPIError as exc:
                raise AgentError(exc.status, exc.message) from exc
            self._update(binding_id, last_activity_at=time.time(), error=None, save=True)
            if request_id:
                with self._lock:
                    self._remember_request(request_id, binding_id, clean["text"])
        return self.detail(binding_id)

    def _match_project_command(self, api: OpenCodeAPI, binding: Dict[str, Any],
                               text: str) -> Optional[tuple[str, str]]:
        if not text.startswith("/") or len(text) < 2:
            return None
        token, _, rest = text.partition(" ")
        name = token[1:]
        if not name or name in {"goal", "stop", "budget", "status", "help", "pause", "resume"}:
            return None
        try:
            commands = {item["name"] for item in self.catalog(binding["project_directory"]).get("commands", [])}
        except AgentError:
            return None
        if name not in commands:
            return None
        return name, rest.strip()

    def _office_command(self, text: str) -> Optional[Dict[str, Any]]:
        if not text.startswith("/"):
            return None
        token, _, rest = text.partition(" ")
        name = token[1:].strip().lower()
        if name == "goal":
            value = rest.strip()
            if not value:
                return {"action": "complete"}
            lowered = value.lower()
            if lowered in ("pause", "resume", "clear", "complete", "stop"):
                return {"action": "complete" if lowered in ("complete", "stop") else lowered}
            return {"action": "set", "objective": value}
        if name == "stop":
            return {"action": "stop"}
        if name == "pause":
            return {"action": "pause"}
        if name == "resume":
            return {"action": "resume"}
        if name == "budget":
            return {"action": "budget", "value": rest.strip()}
        if name == "status":
            return {"action": "status"}
        if name == "help":
            return {"action": "help"}
        return None

    def _run_office_command(self, binding_id: str, command: Dict[str, Any]) -> Dict[str, Any]:
        action = command["action"]
        if action == "status":
            return self.detail(binding_id)
        if action == "help":
            return {
                "help": [
                    "/goal <objective> — set the objective and resume",
                    "/goal — mark complete and stop",
                    "/goal pause|resume|clear — control the loop",
                    "/stop — interrupt the current turn and pause",
                    "/budget <tokens> — change the token cap",
                    "/status — show current state",
                ]
            }
        if action == "budget":
            value = command.get("value", "")
            try:
                amount = int(value.replace(",", "").replace("_", ""))
            except (TypeError, ValueError):
                raise AgentError(400, "budget requires an integer token count") from None
            if not 1 <= amount <= MAX_TOKENS:
                raise AgentError(400, f"budget must be between 1 and {MAX_TOKENS}")
            binding = self._store.get(binding_id)
            if binding is None:
                raise AgentError(404, "agent binding not found")
            self._update(binding_id, max_tokens=amount, budget_wrapup_sent=False, save=True)
            return self.detail(binding_id)
        if action == "stop":
            return self.interrupt(binding_id)
        if action == "pause":
            self._update(binding_id, status="paused", pause_reason="user", save=True)
            return self.detail(binding_id)
        if action == "resume":
            return self.goal(binding_id, {"action": "resume"})
        if action == "complete":
            return self.goal(binding_id, {"action": "complete"})
        if action == "clear":
            return self.goal(binding_id, {"action": "clear"})
        if action == "set":
            return self.goal(binding_id, {"action": "set", "objective": command["objective"]})
        raise AgentError(400, "unsupported office command")

    # -- goal / interrupt --------------------------------------------------

    def goal(self, binding_id: str, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise AgentError(400, "goal requires a JSON object")
        action = payload.get("action")
        if action not in ("set", "pause", "resume", "clear", "complete"):
            raise AgentError(400, "goal action must be set, pause, resume, clear, or complete")
        binding = self._store.get(binding_id)
        if binding is None:
            raise AgentError(404, "agent binding not found")
        if action == "set":
            objective = payload.get("objective")
            if not _text(objective, MAX_GOAL_CHARS, multiline=True):
                raise AgentError(400, f"objective must contain 1–{MAX_GOAL_CHARS} characters")
            updates: Dict[str, Any] = {"goal": objective, "updated_at": time.time()}
            mode = payload.get("mode")
            if mode is not None:
                if mode not in ("continuous", "goal"):
                    raise AgentError(400, "mode must be continuous or goal")
                updates["mode"] = mode
            completion = payload.get("completion")
            if completion is not None:
                if completion not in ("manual", "marker"):
                    raise AgentError(400, "completion must be manual or marker")
                updates["completion"] = completion
            if binding.get("status") not in ACTIVE_STATUSES:
                updates.update(status="running", pause_reason=None, started_at=binding.get("started_at") or time.time(),
                               error=None, stall_count=0)
                if binding.get("pause_reason") == "error" and not binding.get("last_error_message_id"):
                    # Bindings written before error de-duplication need the
                    # current failed turn recorded so the next tick retries it.
                    updates["last_error_message_id"] = binding.get("last_assistant_message_id")
            self._update(binding_id, save=True, **updates)
            return self.detail(binding_id)
        if action == "pause":
            self._update(binding_id, status="paused", pause_reason="user", save=True)
            return self.detail(binding_id)
        if action == "resume":
            if binding.get("status") == "complete":
                raise AgentError(409, "this agent is complete; set a new objective to continue")
            project_key = self._project_key(binding["project_directory"])
            with self._lock:
                other = self._active_binding(project_key, exclude=binding_id)
                if other is not None:
                    raise AgentError(409, "another agent is already active in this project")
                if self._active_count() >= self._max_active and binding.get("status") not in ACTIVE_STATUSES:
                    raise AgentError(409, "the agent control plane is at its active limit")
            updates = {
                "status": "running",
                "pause_reason": None,
                "started_at": binding.get("started_at") or time.time(),
                "error": None,
                "budget_wrapup_sent": False,
                "stall_count": 0,
                "stall_message_id": None,
                "consecutive_errors": 0,
                "last_continuation_at": None,
                "last_continuation_message_id": None,
            }
            if binding.get("status") == "limit_reached":
                # A resumed run gets a fresh turn and duration allowance; the
                # token budget remains cumulative for the binding.
                updates["turns"] = 0
                updates["started_at"] = time.time()
            if binding.get("pause_reason") == "error" and not binding.get("last_error_message_id"):
                # See the action=set note above: migrate older bindings.
                updates["last_error_message_id"] = binding.get("last_assistant_message_id")
            self._update(binding_id, save=True, **updates)
            return self.detail(binding_id)
        if action == "clear":
            self._update(
                binding_id,
                goal="",
                status="paused",
                pause_reason="user",
                completion_evidence=None,
                save=True,
            )
            return self.detail(binding_id)
        # complete
        self._update(
            binding_id,
            status="complete",
            pause_reason=None,
            goal_completed_at=time.time(),
            save=True,
        )
        return self.detail(binding_id)

    def interrupt(self, binding_id: str) -> Dict[str, Any]:
        binding = self._store.get(binding_id)
        if binding is None:
            raise AgentError(404, "agent binding not found")
        api = self._require_api()
        try:
            api.interrupt(binding["session_id"])
        except OpenCodeAPIError as exc:
            raise AgentError(exc.status, exc.message) from exc
        self._update(binding_id, status="paused", pause_reason="user", save=True)
        return self.detail(binding_id)

    def reply_permission(self, binding_id: str, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise AgentError(400, "permission reply requires a JSON object")
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        message = payload.get("message")
        if not isinstance(request_id, str) or not request_id or "/" in request_id:
            raise AgentError(400, "request_id is required")
        if decision not in ("once", "always", "reject"):
            raise AgentError(400, "decision must be once, always, or reject")
        if message is not None and (not isinstance(message, str) or len(message) > 2000):
            raise AgentError(400, "message must be a string of at most 2000 characters")
        binding = self._store.get(binding_id)
        if binding is None:
            raise AgentError(404, "agent binding not found")
        api = self._require_api()
        try:
            api.reply_permission(binding["session_id"], request_id, decision, message)
        except OpenCodeAPIError as exc:
            raise AgentError(exc.status, exc.message) from exc
        self._resume_after_input(binding_id)
        return self.detail(binding_id)

    def reply_form(self, binding_id: str, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise AgentError(400, "form reply requires a JSON object")
        form_id = payload.get("form_id")
        answer = payload.get("answer")
        if not isinstance(form_id, str) or not form_id or "/" in form_id:
            raise AgentError(400, "form_id is required")
        if not isinstance(answer, dict):
            raise AgentError(400, "answer must be an object")
        binding = self._store.get(binding_id)
        if binding is None:
            raise AgentError(404, "agent binding not found")
        api = self._require_api()
        try:
            api.reply_form(binding["session_id"], form_id, answer)
        except OpenCodeAPIError as exc:
            raise AgentError(exc.status, exc.message) from exc
        self._resume_after_input(binding_id)
        return self.detail(binding_id)

    def _resume_after_input(self, binding_id: str) -> None:
        binding = self._store.get(binding_id)
        if binding is not None and binding.get("status") == "waiting_input" \
                and binding.get("pause_reason") == "input":
            self._update(binding_id, status="running", pause_reason=None, error=None, save=True)

    def unbind(self, binding_id: str) -> Dict[str, Any]:
        binding = self._store.remove(binding_id)
        if binding is None:
            raise AgentError(404, "agent binding not found")
        return {"ok": True, "unbound": binding_id, "session_retained": binding["session_id"]}

    def reconnect(self) -> Dict[str, Any]:
        with self._lock:
            self._api = None
            self._connection_error = None
            self._last_discovery_at = 0.0
        self._require_api()
        return self.status(refresh=True)

    # -- loop --------------------------------------------------------------

    def _tick(self, api: OpenCodeAPI, binding_id: str) -> None:
        binding = self._store.get(binding_id)
        if binding is None or binding.get("status") not in ("starting", "running", "waiting_input"):
            return
        session_id = binding["session_id"]
        try:
            session = api.session(session_id)
            active = api.active_sessions()
            permissions = api.permissions(session_id)
            forms = api.forms(session_id)
            inbox = api.inbox(session_id)
            recent = api.messages(session_id, limit=RECENT_MESSAGE_LIMIT, order="desc")
        except OpenCodeAPIError as exc:
            self._record_api_error(binding_id, exc)
            return

        directory = (session.get("location") or {}).get("directory")
        try:
            session_dir = Path(directory).resolve(strict=True) if isinstance(directory, str) else None
        except (OSError, RuntimeError):
            session_dir = None
        if session_dir is None or _norm(session_dir) != _norm(Path(binding["project_directory"])):
            self._update(binding_id, status="error", pause_reason="error",
                         error="the OpenCode session no longer matches its project", save=True)
            return

        fresh, total, cost, reported = self._scope_metrics(api, session, Path(binding["project_directory"]))
        used = max(0, fresh - int(binding.get("token_baseline") or 0))
        total_used = max(0, total - int(binding.get("token_baseline_total") or 0))
        cost_used = max(0.0, cost - float(binding.get("cost_baseline") or 0.0))
        updates: Dict[str, Any] = {
            "tokens_used": used,
            "tokens_total": total_used,
            "cost_usd": round(cost_used, 6),
            "tokens_reported": reported,
            "last_activity_at": time.time(),
            "updated_at": time.time(),
            "consecutive_errors": 0,
        }
        messages = recent.get("data") if isinstance(recent.get("data"), list) else []
        ascending = list(reversed(messages))
        latest = ascending[-1] if ascending else None
        latest_assistant = next(
            (item for item in reversed(ascending)
             if isinstance(item, dict) and _message_role(item) == "assistant"),
            None,
        )
        if isinstance(latest, dict):
            updates["last_message_id"] = _short(latest.get("id"), 128)
            if _message_role(latest) == "user":
                updates["last_user_message_id"] = _short(latest.get("id"), 128)
        if isinstance(latest_assistant, dict):
            updates["last_assistant_message_id"] = _short(latest_assistant.get("id"), 128)

        # A /goal typed in the OpenCode TUI is a normal user message.  Treat it
        # as an office control message so both interfaces share the stop rule.
        control = self._scan_control_messages(binding, ascending)
        if control is not None:
            self._update(binding_id, save=True, **updates)
            self._apply_tui_control(binding_id, control)
            return

        if permissions or forms:
            self._update(
                binding_id,
                status="waiting_input",
                pause_reason="input",
                error=None,
                pending_permissions=len(permissions),
                pending_forms=len(forms),
                save=True,
                **updates,
            )
            return
        if binding.get("status") == "waiting_input" and binding.get("pause_reason") == "input":
            self._update(binding_id, status="running", pause_reason=None,
                         pending_permissions=0, pending_forms=0, save=False, **updates)
            binding = self._store.get(binding_id) or binding

        if binding.get("status") != "running":
            self._update(binding_id, save=False, **updates)
            return

        if self._is_active(session_id, active):
            self._update(binding_id, save=False, **updates)
            return
        if self._pending_user_inbox(inbox):
            self._update(binding_id, save=False, **updates)
            return

        # Do not continue while the newest visible message is a user prompt
        # that has not produced an assistant turn yet.
        latest = ascending[-1] if ascending else None
        if isinstance(latest, dict) and _message_role(latest) == "user":
            self._update(binding_id, save=False, **updates)
            return

        if isinstance(latest_assistant, dict):
            assistant_error = latest_assistant.get("error")
            if latest_assistant.get("finish") == "error" or assistant_error:
                message_id = _short(latest_assistant.get("id"), 128)
                if message_id and message_id == binding.get("last_error_message_id"):
                    # This error was already surfaced.  A resume retries the
                    # turn instead of pausing on the same message forever.
                    pass
                else:
                    if isinstance(assistant_error, dict):
                        reason = assistant_error.get("message") or assistant_error.get("name")
                    elif isinstance(assistant_error, str):
                        reason = assistant_error
                    else:
                        reason = "the model returned an error"
                    self._update(
                        binding_id, status="error", pause_reason="error",
                        error=_redact(reason), last_error_message_id=message_id,
                        save=True, **updates,
                    )
                    return

        if binding.get("completion") == "marker" and isinstance(latest_assistant, dict):
            marker = _marker(_message_text(latest_assistant))
            if marker is not None:
                if marker["kind"] == "complete":
                    self._update(
                        binding_id, status="complete", pause_reason=None,
                        goal_completed_at=time.time(), completion_evidence=marker["reason"],
                        save=True, **updates,
                    )
                else:
                    self._update(
                        binding_id, status="paused", pause_reason="stalled",
                        error=f"agent reported blocked: {marker['reason']}",
                        save=True, **updates,
                    )
                return

        if self._stalled(binding, latest_assistant):
            self._update(
                binding_id, status="paused", pause_reason="stalled",
                error="no progress across repeated turns", save=True, **updates,
            )
            return

        # Budget is a soft stop: let the current turn finish, send one wrap-up
        # prompt, then stop issuing continuations.
        if used >= int(binding["max_tokens"]):
            if not binding.get("budget_wrapup_sent"):
                sent = self._send_continuation(
                    api, binding,
                    kind="budget_wrapup",
                    text=self._budget_wrapup_text(binding, used),
                    after_message_id=_short(latest_assistant.get("id"), 128) if isinstance(latest_assistant, dict) else None,
                )
                self._update(binding_id, budget_wrapup_sent=bool(sent), save=bool(sent), **updates)
                return
            if not self._is_active(session_id, active):
                self._update(binding_id, status="budget_exhausted", pause_reason="budget", save=True, **updates)
            else:
                self._update(binding_id, save=False, **updates)
            return

        max_turns = int(binding.get("max_turns") or 0)
        if max_turns and int(binding.get("turns") or 0) >= max_turns:
            self._update(binding_id, status="limit_reached", pause_reason="limit",
                         error=f"turn limit reached ({max_turns})", save=True, **updates)
            return
        max_duration = int(binding.get("max_duration_s") or 0)
        started = binding.get("started_at")
        if max_duration and isinstance(started, (int, float)) and time.time() - started >= max_duration:
            self._update(binding_id, status="limit_reached", pause_reason="limit",
                         error="duration limit reached", save=True, **updates)
            return

        last_continuation = binding.get("last_continuation_at")
        if isinstance(last_continuation, (int, float)) and time.time() - last_continuation < CONTINUATION_COOLDOWN:
            self._update(binding_id, save=False, **updates)
            return
        latest_id = _short(latest_assistant.get("id"), 128) if isinstance(latest_assistant, dict) else None
        if latest_id and latest_id == binding.get("last_continuation_message_id"):
            # The same completed turn has already received its continuation.
            # Wait for a new assistant message instead of stacking prompts.
            self._update(binding_id, save=False, **updates)
            return

        if not binding.get("initial_sent"):
            self._send_initial(api, binding)
        else:
            self._send_continuation(api, binding, kind="continuation",
                                    text=self._continuation_text(binding, used),
                                    after_message_id=latest_id)
        self._update(binding_id, save=True, **updates)

    def _is_active(self, session_id: str, active: Dict[str, Any]) -> bool:
        return isinstance(active, dict) and session_id in active

    def _pending_user_inbox(self, inbox: List[Dict[str, Any]]) -> bool:
        for item in inbox:
            if not isinstance(item, dict) or item.get("type") != "user":
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            office = metadata.get("agent_office") if isinstance(metadata.get("agent_office"), dict) else {}
            if office.get("kind") not in OFFICE_KINDS:
                return True
        return False

    def _office_inbox_has(self, api: OpenCodeAPI, session_id: str,
                          kind: str, text: str) -> bool:
        """Return True when an identical office prompt is already queued.

        A lost response to a prompt POST can leave the request in OpenCode's
        inbox while the control plane still believes it was not sent.  Checking
        the inbox before a retry prevents duplicate continuation prompts.
        """
        try:
            inbox = api.inbox(session_id)
        except OpenCodeAPIError:
            return False
        for item in inbox:
            if not isinstance(item, dict) or item.get("type") != "user":
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            office = metadata.get("agent_office") if isinstance(metadata.get("agent_office"), dict) else {}
            if office.get("kind") == kind and payload.get("text") == text:
                return True
        return False

    def _scan_control_messages(self, binding: Dict[str, Any],
                               messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        last = binding.get("last_processed_user_message_id")
        start = 0
        if last:
            for index, item in enumerate(messages):
                if isinstance(item, dict) and item.get("id") == last:
                    start = index + 1
                    break
        control = None
        latest_user = None
        for item in messages[start:]:
            if not isinstance(item, dict) or _message_role(item) != "user" or _is_office_message(item):
                continue
            latest_user = _short(item.get("id"), 128)
            text = item.get("text") if isinstance(item.get("text"), str) else ""
            if text.strip().startswith("/goal"):
                control = {"id": latest_user, "text": text.strip()}
        if latest_user is not None:
            control = control or {"id": latest_user, "text": ""}
        return control

    def _apply_tui_control(self, binding_id: str, control: Dict[str, Any]) -> None:
        text = control.get("text") or ""
        self._update(binding_id, last_processed_user_message_id=control.get("id"), save=True)
        if not text:
            return
        value = text[5:].strip()
        if not value:
            self.goal(binding_id, {"action": "complete"})
            return
        lowered = value.lower()
        if lowered in ("pause", "resume", "clear", "complete", "stop"):
            action = "complete" if lowered in ("complete", "stop") else lowered
            self.goal(binding_id, {"action": action})
            return
        self.goal(binding_id, {"action": "set", "objective": value})

    def _stalled(self, binding: Dict[str, Any], latest: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(latest, dict) or _message_role(latest) != "assistant":
            return False
        message_id = _short(latest.get("id"), 128)
        if message_id and message_id == binding.get("stall_message_id"):
            # Do not count the same completed turn once per polling interval.
            return int(binding.get("stall_count") or 0) >= STALL_LIMIT
        tools = _message_tools(latest)
        text = _message_text(latest).strip()
        # A turn with tool calls or a substantive answer is progress.  A short
        # "continue" style answer with no tools increments the stall counter.
        if tools or len(text) >= 400:
            if binding.get("stall_count") or binding.get("stall_message_id") != message_id:
                self._update(binding["id"], stall_count=0, stall_message_id=message_id, save=False)
            return False
        count = int(binding.get("stall_count") or 0) + 1
        self._update(binding["id"], stall_count=count, stall_message_id=message_id, save=False)
        return count >= STALL_LIMIT

    def _send_initial(self, api: OpenCodeAPI, binding: Dict[str, Any]) -> None:
        with self._send_lock:
            current = self._store.get(binding["id"])
            if current is None or current.get("status") not in ("starting", "running", "paused"):
                return
            if current.get("initial_sent"):
                return
            start = current.get("start_with")
            try:
                if isinstance(start, dict) and start.get("kind") == "command":
                    api.command(
                        current["session_id"], start["name"], start.get("text") or "",
                        delivery="queue",
                    )
                elif isinstance(start, dict) and start.get("kind") == "prompt":
                    if not self._office_inbox_has(
                            api, current["session_id"], "goal", start["text"]):
                        api.prompt(
                            current["session_id"], start["text"],
                            metadata={"agent_office": {"binding_id": current["id"], "kind": "goal"}},
                            delivery="queue",
                            message_id="msg_" + secrets.token_hex(12),
                        )
                else:
                    text = self._initial_text(current)
                    if not self._office_inbox_has(
                            api, current["session_id"], "goal", text):
                        api.prompt(
                            current["session_id"], text,
                            metadata={"agent_office": {"binding_id": current["id"], "kind": "goal"}},
                            delivery="queue",
                            message_id="msg_" + secrets.token_hex(12),
                        )
            except OpenCodeTransportError as exc:
                # The prompt may already be queued even though the response was
                # lost.  Leave the binding active and let the next tick's inbox
                # check decide whether to send it again.
                self._update(current["id"], error=_redact(exc.message), save=True)
                return
            except OpenCodeAPIError as exc:
                self._update(current["id"], status="error", pause_reason="error",
                             error=_redact(exc.message), save=True)
                raise
            self._update(
                current["id"], initial_sent=True, status="running",
                pause_reason=None, last_continuation_at=time.time(),
                last_continuation_message_id=None,
                started_at=current.get("started_at") or time.time(), save=True,
            )

    def _send_continuation(self, api: OpenCodeAPI, binding: Dict[str, Any], *,
                           kind: str, text: str,
                           after_message_id: Optional[str] = None) -> bool:
        with self._send_lock:
            current = self._store.get(binding["id"])
            if current is None or current.get("status") != "running":
                return False
            if self._office_inbox_has(api, current["session_id"], kind, text):
                # The previous attempt reached OpenCode but its response was
                # lost.  Record the turn instead of queueing a duplicate.
                turns = int(current.get("turns") or 0) + (1 if kind == "continuation" else 0)
                self._update(
                    current["id"], turns=turns, last_continuation_at=time.time(),
                    last_continuation_message_id=after_message_id,
                    last_activity_at=time.time(), save=True,
                )
                return True
            try:
                response = api.prompt(
                    current["session_id"], text,
                    metadata={"agent_office": {"binding_id": current["id"], "kind": kind}},
                    delivery="queue",
                    message_id="msg_" + secrets.token_hex(12),
                )
            except OpenCodeTransportError as exc:
                self._update(current["id"], error=_redact(exc.message), save=True)
                return False
            except OpenCodeAPIError as exc:
                self._record_api_error(current["id"], exc)
                return False
            turns = int(current.get("turns") or 0) + (1 if kind == "continuation" else 0)
            self._update(
                current["id"],
                turns=turns,
                last_continuation_at=time.time(),
                last_continuation_inbox_id=_short(response.get("id"), 128),
                last_continuation_message_id=after_message_id,
                last_activity_at=time.time(),
                save=True,
            )
            return True

    def _initial_text(self, binding: Dict[str, Any]) -> str:
        mode = binding.get("mode")
        completion = binding.get("completion")
        if mode == "continuous":
            instructions = (
                "Work continuously on this project until the operator stops the run. "
                "Do not declare the overall objective complete; keep finding the next "
                "valuable, verifiable step, execute it, and verify the result. Maintain "
                "a short backlog of remaining work."
            )
        else:
            instructions = (
                "Work autonomously until the objective is fully met. Verify your work "
                "before declaring success."
            )
        marker = ""
        if completion == "marker":
            marker = (
                "\nWhen the objective is fully met, end your final message with a line:\n"
                "GOAL_COMPLETE: <short evidence>\n"
                "If you are blocked and need the operator, end with:\n"
                "GOAL_BLOCKED: <reason>"
            )
        return (
            f"You are working autonomously in this project as {binding['agent_id']}.\n\n"
            f"Objective:\n{binding['goal']}\n\n"
            f"Mode: {mode}. {instructions}{marker}\n\n"
            f"Token budget for this run: {binding['max_tokens']:,} fresh tokens. "
            "Prefer concrete progress, inspect the current state before editing, and "
            "do not repeat completed work."
        )

    def _continuation_text(self, binding: Dict[str, Any], used: int) -> str:
        remaining = max(0, int(binding["max_tokens"]) - used)
        completion = ""
        if binding.get("completion") == "marker":
            completion = (
                "\nIf the objective is fully met, end your final message with "
                "`GOAL_COMPLETE: <short evidence>`. If blocked, use "
                "`GOAL_BLOCKED: <reason>`."
            )
        return (
            "<agent_office_continuation>\n"
            f"Objective: {binding['goal']}\n"
            f"Mode: {binding.get('mode')}\n"
            f"Budget remaining: {remaining:,} fresh tokens\n"
            f"Turns completed: {binding.get('turns', 0)}\n\n"
            "Continue from the current project state. Do not repeat completed work. "
            "Inspect the relevant files or running state, choose the next concrete step, "
            "execute it, and verify the result. If you are waiting on a permission or "
            "a decision, ask clearly and stop."
            f"{completion}\n"
            "</agent_office_continuation>"
        )

    def _budget_wrapup_text(self, binding: Dict[str, Any], used: int) -> str:
        return (
            "<agent_office_budget>\n"
            f"The token budget has been reached ({used:,} / {binding['max_tokens']:,}).\n"
            "Do not start new work. Finish the current step if it is already in progress, "
            "then summarize what is complete, what remains, and the exact next step. "
            "End with `GOAL_BUDGET_REACHED` on its own line.\n"
            "</agent_office_budget>"
        )

    # -- scope accounting --------------------------------------------------

    def _scope_sessions(self, api: OpenCodeAPI, root: Dict[str, Any],
                        directory: Path) -> List[Dict[str, Any]]:
        try:
            sessions = api.sessions(str(directory), limit=500)
        except OpenCodeAPIError:
            return [root]
        by_id = {item.get("id"): item for item in sessions if isinstance(item.get("id"), str)}
        by_id[root.get("id")] = root
        children: Dict[str, List[str]] = {}
        for identity, item in by_id.items():
            parent = item.get("parentID")
            if isinstance(parent, str) and parent in by_id:
                children.setdefault(parent, []).append(identity)
        result = []
        stack = [root.get("id")]
        seen = set()
        while stack:
            identity = stack.pop()
            if not isinstance(identity, str) or identity in seen:
                continue
            seen.add(identity)
            item = by_id.get(identity)
            if isinstance(item, dict):
                result.append(item)
            stack.extend(children.get(identity, []))
        return result

    def _scope_tokens(self, api: OpenCodeAPI, root: Dict[str, Any],
                      directory: Path) -> tuple[int, int, bool]:
        fresh, total, _cost, reported = self._scope_metrics(api, root, directory)
        return fresh, total, reported

    def _scope_cost(self, api: OpenCodeAPI, root: Dict[str, Any], directory: Path) -> float:
        return self._scope_metrics(api, root, directory)[2]

    def _scope_metrics(self, api: OpenCodeAPI, root: Dict[str, Any],
                       directory: Path) -> tuple[int, int, float, bool]:
        fresh = total = 0
        cost = 0.0
        reported = False
        for item in self._scope_sessions(api, root, directory):
            item_fresh, item_total, item_reported = _session_tokens(item)
            fresh += item_fresh
            total += item_total
            cost += _session_cost(item)
            reported = reported or item_reported
        return fresh, total, cost, reported

    # -- persistence helpers ----------------------------------------------

    def _update(self, binding_id: str, *, save: bool = True, **changes: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            binding = self._store.get(binding_id)
            if binding is None:
                return None
            binding.update(changes)
            binding["updated_at"] = time.time()
            self._store.put(binding, save=save)
            if save:
                self._last_persist_at = time.time()
            return copy.deepcopy(binding)

    def _remember_request(self, request_id: str, binding_id: str, fingerprint: str) -> None:
        self._request_ids[request_id] = (binding_id, fingerprint, {"at": time.time()})
        while len(self._request_ids) > MAX_REQUEST_IDS:
            self._request_ids.pop(next(iter(self._request_ids)))

    # -- payload validation ------------------------------------------------

    def _validate_create(self, payload: Any) -> Dict[str, Any]:
        allowed = {
            "request_id", "project_directory", "agent_id", "model", "title",
            "goal", "mode", "completion", "max_tokens", "max_turns",
            "max_duration_s", "start_with", "session_id", "start",
        }
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise AgentError(400, "agent request contains unsupported fields")
        request_id = payload.get("request_id")
        if request_id is not None and not _canonical_uuid(request_id):
            raise AgentError(400, "request_id must be a canonical UUID")
        project = payload.get("project_directory")
        if not isinstance(project, str) or not project.strip() or len(project) > MAX_PROJECT_PATH_CHARS:
            raise AgentError(400, "project_directory must be a non-empty absolute path")
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip() or len(agent_id) > MAX_AGENT_ID_CHARS:
            raise AgentError(400, "agent_id is required")
        model = payload.get("model")
        if not isinstance(model, dict) or set(model) - {"providerID", "id", "variant"}:
            raise AgentError(400, "model must contain providerID and id")
        provider = model.get("providerID")
        identity = model.get("id")
        if not isinstance(provider, str) or not provider or not isinstance(identity, str) or not identity:
            raise AgentError(400, "model.providerID and model.id are required")
        variant = model.get("variant")
        if variant is not None and (not isinstance(variant, str) or len(variant) > 64):
            raise AgentError(400, "model.variant must be a short string")
        clean_model = {"providerID": provider, "id": identity}
        if variant:
            clean_model["variant"] = variant
        goal = payload.get("goal")
        if not _text(goal, MAX_GOAL_CHARS, multiline=True):
            raise AgentError(400, f"goal must contain 1–{MAX_GOAL_CHARS} characters")
        mode = payload.get("mode", "continuous")
        if mode not in ("continuous", "goal"):
            raise AgentError(400, "mode must be continuous or goal")
        completion = payload.get("completion")
        if completion is None:
            completion = "marker" if mode == "goal" else "manual"
        if completion not in ("manual", "marker"):
            raise AgentError(400, "completion must be manual or marker")
        max_tokens = payload.get("max_tokens", DEFAULT_MAX_TOKENS)
        if not _integer(max_tokens, 1, MAX_TOKENS):
            raise AgentError(400, f"max_tokens must be between 1 and {MAX_TOKENS}")
        max_turns = payload.get("max_turns", DEFAULT_MAX_TURNS)
        if not _integer(max_turns, 0, MAX_TURNS):
            raise AgentError(400, f"max_turns must be between 0 and {MAX_TURNS}")
        max_duration = payload.get("max_duration_s", DEFAULT_MAX_DURATION_S)
        if not _integer(max_duration, 0, MAX_DURATION_S):
            raise AgentError(400, f"max_duration_s must be between 0 and {MAX_DURATION_S}")
        title = payload.get("title")
        if title is not None and not _text(title, MAX_TITLE_CHARS):
            raise AgentError(400, f"title must contain 1–{MAX_TITLE_CHARS} characters")
        start = payload.get("start", True)
        if not isinstance(start, bool):
            raise AgentError(400, "start must be a boolean")
        session_id = payload.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not session_id or "/" in session_id):
            raise AgentError(400, "session_id is invalid")
        start_with = payload.get("start_with")
        if start_with is not None:
            if not isinstance(start_with, dict) or start_with.get("kind") not in ("prompt", "command"):
                raise AgentError(400, "start_with.kind must be prompt or command")
            if set(start_with) - {"kind", "text", "name"}:
                raise AgentError(400, "start_with contains unsupported fields")
            if start_with["kind"] == "prompt":
                if not _text(start_with.get("text"), MAX_PROMPT_CHARS, multiline=True):
                    raise AgentError(400, f"start_with.text must contain 1–{MAX_PROMPT_CHARS} characters")
                start_with = {"kind": "prompt", "text": start_with["text"]}
            else:
                name = start_with.get("name")
                if not isinstance(name, str) or not name or len(name) > MAX_AGENT_ID_CHARS:
                    raise AgentError(400, "start_with.name is required for a command")
                text = start_with.get("text") or ""
                if not _optional_text(text, MAX_PROMPT_CHARS, multiline=True):
                    raise AgentError(400, "start_with.text is too long")
                start_with = {"kind": "command", "name": name, "text": text}
        project_name = Path(project).name or project
        return {
            "request_id": request_id,
            "project_directory": project,
            "agent_id": agent_id,
            "model": clean_model,
            "title": title or _short(f"{project_name} · {agent_id}", MAX_TITLE_CHARS),
            "goal": goal,
            "mode": mode,
            "completion": completion,
            "max_tokens": max_tokens,
            "max_turns": max_turns,
            "max_duration_s": max_duration,
            "start": start,
            "session_id": session_id,
            "start_with": start_with,
        }

    def _validate_prompt(self, payload: Any) -> Dict[str, Any]:
        allowed = {"text", "request_id", "delivery"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise AgentError(400, "message request contains unsupported fields")
        text = payload.get("text")
        if not _text(text, MAX_PROMPT_CHARS, multiline=True):
            raise AgentError(400, f"text must contain 1–{MAX_PROMPT_CHARS} characters")
        request_id = payload.get("request_id")
        if request_id is not None and not _canonical_uuid(request_id):
            raise AgentError(400, "request_id must be a canonical UUID")
        delivery = payload.get("delivery", "queue")
        if delivery not in ("queue", "steer"):
            raise AgentError(400, "delivery must be queue or steer")
        return {"text": text, "request_id": request_id, "delivery": delivery}

    # -- presentation ------------------------------------------------------

    def _summary(self, binding: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "id", "session_id", "project_directory", "project_name", "agent_id",
            "model", "title", "goal", "mode", "completion", "status",
            "pause_reason", "turns", "max_tokens", "max_turns", "max_duration_s",
            "tokens_used", "tokens_total", "cost_usd", "tokens_reported",
            "started_at", "created_at", "updated_at", "last_activity_at",
            "goal_completed_at", "completion_evidence", "error",
            "pending_permissions", "pending_forms", "budget_wrapup_sent",
            "initial_sent",
        )
        result = {key: copy.deepcopy(binding.get(key)) for key in keys}
        result["remaining_tokens"] = max(
            0, int(binding.get("max_tokens") or 0) - int(binding.get("tokens_used") or 0)
        )
        result["can_resume"] = binding.get("status") not in ("complete",)
        return result

    @staticmethod
    def _session_summary(session: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": _short(session.get("id"), 128),
            "title": _short(session.get("title"), 200),
            "agent": _short(session.get("agent"), 128),
            "model": session.get("model") if isinstance(session.get("model"), dict) else None,
            "cost": session.get("cost") if isinstance(session.get("cost"), (int, float)) else None,
            "tokens": session.get("tokens") if isinstance(session.get("tokens"), dict) else None,
            "time": session.get("time") if isinstance(session.get("time"), dict) else {},
            "location": session.get("location") if isinstance(session.get("location"), dict) else {},
        }

    @staticmethod
    def _permission_summary(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": _short(item.get("id"), 128),
            "action": _short(item.get("action"), 200),
            "resources": [_short(value, 500) for value in item.get("resources", []) if isinstance(value, str)],
            "save": [_short(value, 500) for value in item.get("save", []) if isinstance(value, str)],
            "message": _short(item.get("message"), 500),
            "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        }

    @staticmethod
    def _form_summary(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": _short(item.get("id"), 128),
            "title": _short(item.get("title"), 300),
            "fields": item.get("fields") if isinstance(item.get("fields"), list) else [],
            "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        }
