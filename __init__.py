"""pixel-office plugin — a pixel-art virtual office for Hermes agents.

Every Hermes agent (main sessions AND delegate_task subagents) shows up as
an animated pixel character sitting at a desk in a tiny office, rendered in
your browser at http://127.0.0.1:8113 (port configurable).

Design:

* Hooks are pure observers — they never veto or transform anything.
  Each hook atomically publishes one immutable record into the local inbox.
  Publishing and server startup are wrapped in try/except so observer errors
  never propagate into the agent loop.

* A daemon HTTP server thread is started lazily on the first event. It
  serves the office page and ``/state`` from committed observations.
  Background ingestion continues without an open browser. State is checkpointed
  in shared SQLite storage, so agents from OTHER Hermes processes (gateway +
  CLI at once, cron sessions) appear in the same office. If the port is
  already bound, a verified office process may serve the shared data while
  hooks keep publishing immutable inbox records.

* Processed input files are removed only after their state and XP commit.
  Recent history has configurable count/age/byte retention. Pending approvals
  survive compaction and restart. Old events.jsonl integrations remain readable.

Configuration (all optional, config.yaml):

    plugins:
      entries:
        pixel-office:
          port: 8113        # HTTP port for the office page
          enabled: true

Nothing here touches the conversation, the prompt cache, or tool results.
"""

from __future__ import annotations

import json
import copy
import contextvars
import logging
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from hermes_constants import get_hermes_home
except ImportError:
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

logger = logging.getLogger(__name__)

try:
    from .settings_store import SettingsStore, SettingsConflict, SettingsUnavailable, parse_settings_json
except ImportError:
    from settings_store import SettingsStore, SettingsConflict, SettingsUnavailable, parse_settings_json

DEFAULT_PORT = 8113
_lock = threading.RLock()
_event_cache: Optional[tuple[tuple, List[Dict[str, Any]]]] = None
_server_start_lock = threading.Lock()
_server_thread: Optional[threading.Thread] = None
# Warning counters must not wait behind state ingestion or settings writes.
_publish_warning_lock = threading.Lock()
_publish_warning_at: Optional[float] = None
_publish_warnings_suppressed = 0
_port: int = DEFAULT_PORT
# Current Hermes versions provide session/tool ids on approvals. Keep a
# context-local fallback for older hooks; one gateway process runs concurrent
# sessions, so a process-global "last session" can attribute the wrong agent.
_current_session_id = contextvars.ContextVar("office_session_id", default="")
_current_call_id = contextvars.ContextVar("office_call_id", default="")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _office_dir() -> Path:
    d = get_hermes_home() / "pixel-office"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _events_path() -> Path:
    return _office_dir() / "events.jsonl"


def _settings_path() -> Path:
    return _office_dir() / "settings.json"


_DEFAULTS = {
    "layout": "open",
    "theme": "default",
    "desk_style": "walnut",
    "subagent_style": "robot",
    "sound": False,
    "music_track": "window-seat",
    "music_volume": 0.12,
    "max_chars": 4,
    "ambience": "auto",
    "room_lights": True,
    "show_labels": True,
    "show_pets": True,
    "pets_roam": True,
    "decorations": True,
    "furniture": [],
    "room_name": "",
    "agent_names": {},
    "agent_preferences": {},
    "history_limit": 1000,
    "history_days": 7,
    "history_max_bytes": 5 * 1024 * 1024,
    "sound_needs_input": True,
    "sound_error": True,
    "sound_reward": True,
    "pet_names": {},
    "budget_usd": 0,
    "aquarium_name": "",
    "aquarium_light": True,
    "aquarium_species": ["ember"],
}


def _valid_agent_preference_id(agent_id: Any) -> bool:
    return (isinstance(agent_id, str) and bool(agent_id.strip()) and len(agent_id) <= 512
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in agent_id))


def _valid_agent_preferences(value: Any) -> Optional[Dict[str, Any]]:
    """Validate the whole replacement map; never truncate canonical identities."""
    if not isinstance(value, dict) or len(value) > 128:
        return None
    appearances = {"default", "char0", "char1", "char2", "char3", "char4", "char5", "studio-assistant", "moss-engineer", "orbit-courier"}
    out = {}
    for agent_id, preference in value.items():
        if (not _valid_agent_preference_id(agent_id)
                or not isinstance(preference, dict) or not set(preference) <= {"seat", "appearance"}):
            return None
        clean = {}
        if "seat" in preference and preference["seat"] is not None:
            seat = preference["seat"]
            if type(seat) not in (int, float) or not 0 <= seat < 128 or int(seat) != seat:
                return None
            clean["seat"] = int(seat)
        if "appearance" in preference:
            appearance = preference["appearance"]
            if not isinstance(appearance, str) or appearance not in appearances:
                return None
            if appearance != "default":
                clean["appearance"] = appearance
        if clean:
            out[agent_id] = clean
    return out


def _valid_settings(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    out = {}
    choices = {"layout": {"open", "bullpen"}, "theme": {"default", "midnight", "amber", "juniper"},
               "desk_style": {"classic", "walnut", "slate"},
               "subagent_style": {"robot", "people"},
               "ambience": {"auto", "day", "night"},
               "music_track": {"window-seat", "night-shift", "rainy-break"}}
    for key, value in data.items():
        if key in choices:
            if isinstance(value, str) and value in choices[key]:
                out[key] = value
        elif key in ("sound", "sound_needs_input", "sound_error", "sound_reward",
                     "show_labels", "show_pets", "pets_roam",
                     "decorations", "room_lights", "aquarium_light") and isinstance(value, bool):
            out[key] = value
        elif key == "max_chars" and type(value) is int and 2 <= value <= 8:
            out[key] = value
        elif key in ("room_name", "aquarium_name") and isinstance(value, str):
            out[key] = " ".join(value.split())[:48]
        elif (key == "agent_names" and isinstance(value, dict) and len(value) <= 128
              and all(_valid_agent_preference_id(k) and isinstance(v, str) for k, v in value.items())):
            out[key] = {k: " ".join(v.split())[:48] for k, v in value.items() if v.strip()}
        elif key == "agent_preferences":
            preferences = _valid_agent_preferences(value)
            if preferences is not None:
                out[key] = preferences
        elif key == "history_limit" and type(value) is int and value in (250, 1000, 5000):
            out[key] = value
        elif key == "history_days" and type(value) is int and value in (1, 7, 30):
            out[key] = value
        elif key == "history_max_bytes" and type(value) is int and value in (1024 * 1024, 5 * 1024 * 1024, 20 * 1024 * 1024):
            out[key] = value
        elif key == "budget_usd" and type(value) in (int, float) and 0 <= value <= 1000000000:
            out[key] = value
        elif key == "music_volume" and type(value) in (int, float) and 0 <= value <= 0.5:
            out[key] = value
        elif key == "pet_names" and isinstance(value, dict):
            out[key] = {k: " ".join(v.split())[:32] for k, v in value.items()
                        if k in ("cat1", "cat2") and isinstance(v, str) and v.strip()}
        elif key == "aquarium_species" and isinstance(value, list):
            out[key] = list(dict.fromkeys(v for v in value[:4]
                                          if isinstance(v, str) and v in ("ember", "mint", "violet", "pearl"))) or ["ember"]
        elif key == "furniture" and isinstance(value, list) and len(value) <= 24:
            # Layouts are full replacements. Reject a malformed whole field,
            # matching the browser validator; never save a truncated layout.
            kinds = ("sofa", "server", "shelf", "monstera", "coffee", "cooler", "lamp",
                     "roundtable", "stool", "succulent", "planter", "whiteboard",
                     "printer", "cart", "coatrack", "arcade", "recordplayer", "robot", "terrarium", "jukebox",
                     "focusbooth", "filingcabinet", "taskterminal", "statusbeacon", "petbed", "fern")
            if all(isinstance(item, dict) and item.get("kind") in kinds
                   and all(type(item.get(k)) in (int, float) and 0 <= item[k] <= 1 for k in ("x", "y"))
                   for item in value):
                out[key] = [{k: item[k] for k in ("kind", "x", "y")} for item in value]
    return out


def _load_settings() -> Dict[str, Any]:
    return _settings_store().read().settings


def _settings_store() -> SettingsStore:
    return SettingsStore(_settings_path(), _DEFAULTS, _valid_settings)


def _save_settings(payload: Dict[str, Any], *, expected_revision: Optional[str] = None):
    with _lock:
        return _settings_store().save(payload, expected_revision)


def _asset_manifest() -> Dict[str, Any]:
    """List bundled + user-added SVG assets. External dirs are read at startup."""
    bundled: List[Dict[str, str]] = []
    base = Path(__file__).resolve().parent / "web" / "assets"
    for f in sorted(base.glob("*.svg")):
        bundled.append({"id": f.stem, "src": f"/assets/{f.name}", "kind": "logo"})
    user: List[Dict[str, str]] = []
    user_dir = _office_dir() / "assets"
    if user_dir.is_dir():
        for f in sorted(user_dir.glob("*.svg")):
            user.append({"id": f"user:{f.stem}", "src": f"/user/{f.name}", "kind": "logo"})
    return {"bundled": bundled, "user": user}


# ---------------------------------------------------------------------------
# Event publishing (hook side — must be cheap and never raise)
# ---------------------------------------------------------------------------

def _warn_publish_failure(exc: Exception) -> None:
    """Bound diagnostics, not publication attempts; logging also stays fail-open."""
    global _publish_warning_at, _publish_warnings_suppressed
    try:
        with _publish_warning_lock:
            now = time.monotonic()
            if _publish_warning_at is not None and now - _publish_warning_at < 60.0:
                _publish_warnings_suppressed = min(_publish_warnings_suppressed + 1, 1_000_000_000)
                return
            suppressed = _publish_warnings_suppressed
            _publish_warnings_suppressed = 0
            _publish_warning_at = now
        # Raw exceptions can contain paths or source payloads. A failure after
        # publication also does not prove the event itself was lost.
        logger.warning(
            "pixel-office: observer event handling failed (%s); %d similar warnings suppressed. "
            "Check local storage and server availability.", type(exc).__name__, suppressed)
    except Exception:
        # A broken logging destination must not turn an observer into a veto.
        pass


def _publish(event: Dict[str, Any]) -> None:
    try:
        event.setdefault("ts", time.time())
        event.setdefault("pid", os.getpid())
        try:
            from .event_inbox import publish
        except ImportError:
            from event_inbox import publish
        publish(_office_dir(), event)
        _ensure_server()
    except Exception as exc:  # observers must never break the loop
        _warn_publish_failure(exc)


# ---------------------------------------------------------------------------
# State folding (server side)
# ---------------------------------------------------------------------------

def _read_events() -> List[Dict[str, Any]]:
    """Return independent events for callers that may modify their result."""
    with _lock:
        return copy.deepcopy(_read_event_snapshot())


def _event_signature(path: Path, stat: os.stat_result) -> tuple:
    return (str(path), stat.st_dev, stat.st_ino, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns)


def _read_event_snapshot() -> List[Dict[str, Any]]:
    """Internal read-only snapshot; callers hold _lock while using it.

    Keep one stable parsed log. Any file change forces a full read so quiet
    waiting sessions and repeated same-timestamp events remain in the fold.
    """
    global _event_cache
    try:
        from .event_store import valid_event
    except ImportError:
        from event_store import valid_event
    path = _events_path().resolve()
    out: List[Dict[str, Any]] = []
    try:
        before = _event_signature(path, path.stat())
    except FileNotFoundError:
        _event_cache = None
        return []
    try:
        if _event_cache is not None and _event_cache[0] == before:
            return _event_cache[1]
        _event_cache = None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            opened = _event_signature(path, os.fstat(fh.fileno()))
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = valid_event(json.loads(line))
                    if event is not None:
                        out.append(event)
                except Exception:
                    continue
            finished = _event_signature(path, os.fstat(fh.fileno()))
        after = _event_signature(path, path.stat())
        if before == opened == finished == after:
            _event_cache = (after, out)
    except OSError:
        _event_cache = None
        # A failed/partial read is not an empty or shorter legacy rewrite.
        # Let the store preserve its cursor while other inbox writers drain.
        # Reset likewise needs a complete snapshot before it can set a fence.
        raise
    return out


def _short(text: Any, n: int = 60) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s[: n - 1] + "…" if len(s) > n else s


def build_state() -> Dict[str, Any]:
    with _lock:
        return _build_state_locked()


def _store():
    try:
        from .event_store import EventStore
    except ImportError:
        from event_store import EventStore
    return EventStore(_office_dir())


def _build_state_locked() -> Dict[str, Any]:
    """Apply new receipts and render the durable live checkpoint."""
    now = time.time()
    preferences = _settings_store().read()
    settings = preferences.settings
    state = _store().consume(_read_event_snapshot, now,
                             history_limit=settings["history_limit"],
                             history_days=settings["history_days"],
                             history_max_bytes=settings["history_max_bytes"],
                             retention_enabled=preferences.error is None)
    for agent in state["agents"]:
        agent["observed_label"] = agent["label"]
        agent["label"] = settings["agent_names"].get(agent["id"], agent["label"])
    return {**state, "ts": now, **preferences.public(),
            "mode": "demo" if os.environ.get("AGENT_OFFICE_DEMO") == "1" else "live"}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ObserverStartupError(RuntimeError):
    """The observer could not safely start its local HTTP server."""


class TaskRunnerStartupError(ObserverStartupError):
    """The explicitly requested task runner could not bind its local server."""


def _resolve_port() -> int:
    if os.environ.get("AGENT_OFFICE_PORT"):
        return int(os.environ["AGENT_OFFICE_PORT"])
    try:
        from hermes_cli.config import cfg_get, load_config

        p = cfg_get(load_config(), "plugins", "entries", "pixel-office", "port")
        if p:
            return int(p)
    except Exception:
        pass
    return DEFAULT_PORT


def _reset_server_startup_after_fork() -> None:
    # A child cannot own its parent's thread or inherit a lock held by a
    # vanished thread. Its next publication makes a fresh listener attempt.
    global _server_start_lock, _server_thread
    _server_start_lock = threading.Lock()
    _server_thread = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_server_startup_after_fork)


def _ensure_server() -> None:
    global _server_thread
    # Listener ownership must not wait behind state ingestion. A pending bind
    # is still an active attempt, not a reason for every hook to start another.
    with _server_start_lock:
        if _server_thread is not None:
            if _server_thread.is_alive() or _server_bound():
                return
        worker = threading.Thread(target=_serve, name="pixel-office-http", daemon=True)
        worker.start()
        # Retain only a successfully started worker. Allocation/start failures
        # propagate to the existing redacted diagnostic and remain retryable.
        _server_thread = worker


def _server_bound() -> bool:
    """True if a TCP listener remains on _port after a completed bind attempt."""
    try:
        with socket.create_connection(("127.0.0.1", _port), timeout=1):
            return True
    except OSError:
        return False


_STATIC_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
    ".woff2": "font/woff2",
    ".mp3": "audio/mpeg",
}


def _safe_web_file(web_dir: Path, url_path: str) -> Optional[Path]:
    rel = url_path.split("?")[0].lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None
    path = (web_dir / rel).resolve()
    try:
        path.relative_to(web_dir.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _trusted_observer_origins(values=()) -> frozenset[str]:
    """Read explicit forwarding origins; never infer trust from proxy headers."""
    from urllib.parse import urlsplit

    configured = os.environ.get("AGENT_OFFICE_OBSERVER_ORIGINS", "")
    origins = set()
    for value in [*(configured.split(",") if configured else []), *values]:
        try:
            if not isinstance(value, str) or not value or re.search(r"\s", value):
                raise ValueError
            parsed = urlsplit(value)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or "?" in value or "#" in value):
                raise ValueError
            hostname = parsed.hostname.lower()
            if ":" in hostname:
                import ipaddress
                ipaddress.IPv6Address(hostname)
                hostname = f"[{hostname}]"
            elif not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname):
                raise ValueError
            port = parsed.port
            if port is not None and not 1 <= port <= 65535:
                raise ValueError
            # Match the browser's serialized Origin, including default ports.
            suffix = f":{port}" if port is not None and port != {"http": 80, "https": 443}[parsed.scheme] else ""
            origins.add(f"{parsed.scheme}://{hostname}{suffix}")
        except (ValueError, UnicodeError):
            # Do not include configured URLs: they may contain accidental credentials.
            raise ObserverStartupError(
                "observer origins must be explicit HTTP(S) origins without credentials, paths, queries, or fragments"
            ) from None
    return frozenset(origins)


def _serve(task_runner=None, on_ready=None, observer_origins=(), opencode_handoff=None,
           opencode_agents=None) -> None:
    global _port
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    _port = _resolve_port()
    web_dir = Path(__file__).resolve().parent / "web"
    html_path = web_dir / "template.html"
    try:
        trusted_origins = _trusted_observer_origins(observer_origins)
    except ObserverStartupError:
        if task_runner is not None:
            task_runner.shutdown()
        if opencode_agents is not None:
            opencode_agents.shutdown()
        raise

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence stdout
            pass

        def do_GET(self) -> None:
            try:
                route = self.path.split("?")[0]
                if route == "/agents" or route.startswith("/agents/"):
                    if not self._authorize_agent_request(mutation=False):
                        return
                    if opencode_agents is None:
                        if route == "/agents":
                            self.respond(200, {
                                "enabled": False,
                                "token": None,
                                "connected": False,
                                "projects": [],
                                "agents": [],
                                "models": [],
                                "bindings": [],
                                "limits": {
                                    "max_active_agents": 0,
                                    "max_bindings": 0,
                                    "max_prompt_chars": 8192,
                                    "max_goal_chars": 4000,
                                    "max_tokens": 100000000,
                                    "max_turns": 10000,
                                    "max_duration_s": 604800,
                                },
                            })
                        else:
                            self.respond(404, {"error": "managed OpenCode agents are disabled"})
                        return
                    try:
                        if route == "/agents":
                            self.respond(200, opencode_agents.status())
                            return
                        if route == "/agents/catalog":
                            from urllib.parse import parse_qs, urlsplit
                            query = parse_qs(urlsplit(self.path).query)
                            project = query.get("project", [None])[0]
                            refresh = query.get("refresh", ["0"])[0] in ("1", "true", "yes")
                            self.respond(200, opencode_agents.catalog(project, refresh=refresh))
                            return
                        remainder = route[len("/agents/"):]
                        parts = remainder.split("/")
                        if len(parts) == 1 and parts[0]:
                            self.respond(200, opencode_agents.detail(parts[0]))
                            return
                        if len(parts) == 2 and parts[0] and parts[1] == "messages":
                            from urllib.parse import parse_qs, urlsplit
                            query = parse_qs(urlsplit(self.path).query)
                            flat = {key: values[-1] for key, values in query.items() if values}
                            self.respond(200, opencode_agents.messages(parts[0], flat))
                            return
                        self.respond(404, {"error": "agent route not found"})
                    except Exception as exc:
                        self._respond_agent_error(exc)
                    return
                if route == "/opencode-handoff" or route.startswith("/opencode-handoff/"):
                    if not self._authorize_handoff_request(mutation=False):
                        return
                    if route == "/opencode-handoff":
                        if opencode_handoff is None:
                            self.respond(200, {
                                "enabled": False,
                                "token": None,
                                "paired": False,
                                "pairing": None,
                                "operations": [],
                                "max_prompt_chars": 8192,
                                "max_active_operations": 2,
                                "max_receipts": 200,
                            })
                        else:
                            self.respond(200, opencode_handoff.status())
                        return
                    operation_id = route[len("/opencode-handoff/"):]
                    if not operation_id or "/" in operation_id or opencode_handoff is None:
                        self.respond(404, {"error": "handoff operation not found"})
                        return
                    try:
                        self.respond(200, opencode_handoff.detail(operation_id))
                    except Exception as exc:
                        self._respond_handoff_error(exc)
                    return
                if route == "/task-runs" or route.startswith("/task-runs/"):
                    if not self._authorize_task_request(mutation=False):
                        return
                    if route == "/task-runs":
                        if task_runner is None:
                            self.respond(200, {
                                "enabled": False,
                                "token": None,
                                "workspaces": [],
                                "runtimes": [],
                                "runs": [],
                                "max_prompt_chars": 8192,
                                "max_running": 2,
                            })
                        else:
                            self.respond(200, task_runner.capabilities())
                        return
                    run_id = route[len("/task-runs/"):]
                    if not run_id or "/" in run_id or task_runner is None:
                        self.respond(404, {"error": "task run not found"})
                        return
                    try:
                        self.respond(200, task_runner.detail(run_id))
                    except Exception as exc:
                        try:
                            from .task_runner import TaskRunnerError
                        except ImportError:
                            from task_runner import TaskRunnerError
                        if isinstance(exc, TaskRunnerError):
                            self.respond(exc.status, {"error": exc.message})
                        else:
                            raise
                    return
                private_route = route in {"/state", "/history", "/settings", "/assets-manifest"} or route.startswith("/user/")
                if not self._authorize_observer_request(check_site=private_route):
                    return
                if route == "/":
                    body = html_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                elif route == "/state":
                    body = json.dumps(build_state()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                elif route == "/history":
                    from urllib.parse import parse_qs, urlsplit
                    query = parse_qs(urlsplit(self.path).query)
                    try:
                        limit = int(query.get("limit", ["1000"])[0])
                    except ValueError:
                        self.respond(400, {"error": "history limit must be an integer"})
                        return
                    with _lock:
                        build_state()
                        rows = _store().history(limit)
                    self.respond(200, {"events": rows})
                    return
                elif route == "/settings":
                    preferences = _settings_store().read()
                    if preferences.error:
                        self.respond(503, {"error": preferences.error, **preferences.public()})
                    else:
                        self.respond(200, preferences.settings, {"ETag": f'"{preferences.revision}"'})
                    return
                elif route == "/assets-manifest":
                    body = json.dumps(_asset_manifest()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                elif route.startswith("/user/aquarium/"):
                    name = route.rsplit("/", 1)[-1]
                    allowed = {"ember.png", "mint.png", "violet.png", "pearl.png", "manifest.json"}
                    root = (_office_dir() / "assets" / "aquarium").resolve()
                    asset = (root / name).resolve()
                    if name in allowed and asset.parent == root and asset.is_file():
                        body = asset.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json" if name.endswith(".json") else "image/png")
                        self.send_header("Cache-Control", "no-store")
                    else:
                        self.respond(404, {"error": "optional aquarium pack not installed"})
                        return
                elif route.startswith("/user/"):
                    name = Path(route).name
                    asset = _office_dir() / "assets" / name
                    if asset.is_file() and asset.suffix == ".svg":
                        body = asset.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/svg+xml")
                    else:
                        self.send_response(404)
                        body = b"not found"
                        self.send_header("Content-Type", "text/plain")
                else:
                    static_path = _safe_web_file(web_dir, route)
                    ctype = _STATIC_TYPES.get((static_path.suffix if static_path else ""), "")
                    if static_path and ctype:
                        body = static_path.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", ctype)
                        if static_path.suffix in (".js", ".css", ".html"):
                            self.send_header("Cache-Control", "no-store")
                        elif static_path.suffix == ".png":
                            self.send_header("Cache-Control", "max-age=86400")
                    else:
                        self.send_response(404)
                        body = b"not found"
                        self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logger.debug("pixel-office request failed", exc_info=True)
                try:
                    self.respond(500, {"error": "could not read office state"})
                except OSError:
                    pass

        def respond(self, code, data, headers=None):
            body = json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _authorize_task_request(self, *, mutation: bool) -> bool:
            """Enforce the task runner's loopback browser boundary."""
            import hmac

            expected_host = f"127.0.0.1:{self.server.server_port}"
            hosts = self.headers.get_all("Host", failobj=[])
            if hosts != [expected_host]:
                self.respond(403, {"error": "task runner requires its exact loopback Host"})
                return False
            sites = self.headers.get_all("Sec-Fetch-Site", failobj=[])
            if any(value.strip().lower() == "cross-site" for value in sites):
                self.respond(403, {"error": "cross-site task runner requests are forbidden"})
                return False
            origins = self.headers.get_all("Origin", failobj=[])
            if len(origins) > 1 or (origins and origins[0] != f"http://{expected_host}"):
                self.respond(403, {"error": "task runner request origin is not allowed"})
                return False
            if mutation:
                tokens = self.headers.get_all("X-Agent-Office-Token", failobj=[])
                expected_token = task_runner.token if task_runner is not None else ""
                supplied = tokens[0].encode("utf-8") if len(tokens) == 1 else b""
                expected = expected_token.encode("utf-8")
                if len(tokens) != 1 or not expected_token or not hmac.compare_digest(supplied, expected):
                    self.respond(403, {"error": "task runner token is missing or invalid"})
                    return False
            return True

        def _authorize_handoff_request(self, *, mutation: bool) -> bool:
            """Enforce the session handoff's loopback browser boundary.

            Mirrors the task runner boundary exactly, with the handoff
            adapter's own token: pairing and dispatch stay loopback-only.
            """
            import hmac

            expected_host = f"127.0.0.1:{self.server.server_port}"
            hosts = self.headers.get_all("Host", failobj=[])
            if hosts != [expected_host]:
                self.respond(403, {"error": "session handoff requires its exact loopback Host"})
                return False
            sites = self.headers.get_all("Sec-Fetch-Site", failobj=[])
            if any(value.strip().lower() == "cross-site" for value in sites):
                self.respond(403, {"error": "cross-site session handoff requests are forbidden"})
                return False
            origins = self.headers.get_all("Origin", failobj=[])
            if len(origins) > 1 or (origins and origins[0] != f"http://{expected_host}"):
                self.respond(403, {"error": "session handoff request origin is not allowed"})
                return False
            if mutation:
                tokens = self.headers.get_all("X-Agent-Office-Token", failobj=[])
                expected_token = opencode_handoff.token if opencode_handoff is not None else ""
                supplied = tokens[0].encode("utf-8") if len(tokens) == 1 else b""
                expected = expected_token.encode("utf-8")
                if len(tokens) != 1 or not expected_token or not hmac.compare_digest(supplied, expected):
                    self.respond(403, {"error": "session handoff token is missing or invalid"})
                    return False
            return True

        def _authorize_agent_request(self, *, mutation: bool) -> bool:
            """Enforce the managed OpenCode agent control boundary."""
            import hmac

            expected_host = f"127.0.0.1:{self.server.server_port}"
            hosts = self.headers.get_all("Host", failobj=[])
            if hosts != [expected_host]:
                self.respond(403, {"error": "managed OpenCode agents require their exact loopback Host"})
                return False
            sites = self.headers.get_all("Sec-Fetch-Site", failobj=[])
            if any(value.strip().lower() == "cross-site" for value in sites):
                self.respond(403, {"error": "cross-site managed OpenCode agent requests are forbidden"})
                return False
            origins = self.headers.get_all("Origin", failobj=[])
            if len(origins) > 1 or (origins and origins[0] != f"http://{expected_host}"):
                self.respond(403, {"error": "managed OpenCode agent request origin is not allowed"})
                return False
            if mutation:
                tokens = self.headers.get_all("X-Agent-Office-Token", failobj=[])
                expected_token = opencode_agents.token if opencode_agents is not None else ""
                supplied = tokens[0].encode("utf-8") if len(tokens) == 1 else b""
                expected = expected_token.encode("utf-8")
                if len(tokens) != 1 or not expected_token or not hmac.compare_digest(supplied, expected):
                    self.respond(403, {"error": "managed OpenCode agent token is missing or invalid"})
                    return False
            return True

        def _respond_handoff_error(self, exc: Exception) -> None:
            try:
                from .opencode_handoff import HandoffError
            except ImportError:
                from opencode_handoff import HandoffError
            if isinstance(exc, HandoffError):
                self.respond(exc.status, {"error": exc.message})
            else:
                raise exc

        def _respond_agent_error(self, exc: Exception) -> None:
            try:
                from .opencode_agents import AgentError
            except ImportError:
                from opencode_agents import AgentError
            if isinstance(exc, AgentError):
                self.respond(exc.status, {"error": exc.message})
                return
            try:
                from .opencode_api import OpenCodeAPIError
            except ImportError:
                from opencode_api import OpenCodeAPIError
            if isinstance(exc, OpenCodeAPIError):
                self.respond(exc.status, {"error": exc.message})
                return
            raise exc

        def _authorize_observer_request(self, *, check_site=True) -> bool:
            """Reject DNS rebinding and cross-origin access to local observations."""
            port = self.server.server_port
            loopback_hosts = {f"{host}:{port}" for host in ("127.0.0.1", "localhost", "[::1]")}
            forwarded_hosts = {origin.split("://", 1)[1] for origin in trusted_origins}
            hosts = self.headers.get_all("Host", failobj=[])
            if len(hosts) != 1 or hosts[0] not in loopback_hosts | forwarded_hosts:
                self.respond(403, {"error": "observer request Host is not allowed"})
                return False
            host = hosts[0]
            allowed_origins = {origin for origin in trusted_origins if origin.split("://", 1)[1] == host}
            if host in loopback_hosts:
                # Forwarders may preserve the public Host or rewrite it to loopback.
                allowed_origins.update(trusted_origins)
                allowed_origins.add(f"http://{host}")
            origins = self.headers.get_all("Origin", failobj=[])
            if len(origins) > 1 or (origins and origins[0] not in allowed_origins):
                self.respond(403, {"error": "observer request origin is not allowed"})
                return False
            sites = self.headers.get_all("Sec-Fetch-Site", failobj=[])
            if check_site and (len(sites) > 1 or (sites and sites[0] not in {"same-origin", "none"})):
                self.respond(403, {"error": "cross-site observer requests are forbidden"})
                return False
            return True

        def _read_task_json(self):
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self.respond(415, {"error": "use application/json"})
                return None
            if self.headers.get("Transfer-Encoding"):
                self.respond(400, {"error": "chunked request bodies are not supported"})
                return None
            lengths = self.headers.get_all("Content-Length", failobj=[])
            try:
                length = int(lengths[0]) if len(lengths) == 1 else 0
            except ValueError:
                length = 0
            if not 0 < length <= 65536:
                self.respond(413, {"error": "task request must be 1–65536 bytes"})
                return None
            try:
                self.connection.settimeout(5)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete body")
                def unique_object(pairs):
                    value = {}
                    for key, item in pairs:
                        if key in value:
                            raise ValueError("duplicate JSON property")
                        value[key] = item
                    return value

                payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
                if payload is None:
                    self.respond(400, {"error": "expected a JSON object"})
                    return None
                return payload
            except OSError:
                self.respond(408, {"error": "task request body timed out"})
                return None
            except (ValueError, UnicodeError, RecursionError):
                self.respond(400, {"error": "expected a valid UTF-8 JSON value"})
                return None

        def do_DELETE(self) -> None:
            global _event_cache
            route = self.path.split("?")[0]
            if route not in ("/state", "/history"):
                self.respond(404, {"error": "not found"})
                return
            if not self._authorize_observer_request():
                return
            try:
                with _lock:
                    if route == "/history":
                        removed = _store().prune_history()
                        self.respond(200, {"ok": True, "removed_events": removed})
                        return
                    _store().reset(_read_event_snapshot)
                    _event_cache = None
                self.respond(200, {"ok": True, "cleared": ["progress", "history", "usage"],
                                   "legacy_log_retained": _events_path().exists()})
            except Exception:
                logger.debug("pixel-office reset failed", exc_info=True)
                self.respond(500, {"error": "could not reset state"})

        def do_POST(self) -> None:
            route = self.path.split("?")[0]
            if route == "/agents" or route.startswith("/agents/"):
                if not self._authorize_agent_request(mutation=True):
                    return
                payload = self._read_task_json()
                if payload is None:
                    return
                if opencode_agents is None:
                    self.respond(404, {"error": "managed OpenCode agents are disabled"})
                    return
                try:
                    if route == "/agents":
                        self.respond(201, opencode_agents.create(payload))
                        return
                    if route == "/agents/reconnect":
                        if payload != {}:
                            self.respond(400, {"error": "reconnect requires an empty JSON object"})
                            return
                        self.respond(200, opencode_agents.reconnect())
                        return
                    remainder = route[len("/agents/"):]
                    parts = remainder.split("/")
                    if len(parts) != 2 or not parts[0] or not parts[1]:
                        self.respond(404, {"error": "agent route not found"})
                        return
                    binding_id, action = parts
                    if action == "prompt":
                        self.respond(200, opencode_agents.prompt(binding_id, payload))
                    elif action == "goal":
                        self.respond(200, opencode_agents.goal(binding_id, payload))
                    elif action == "interrupt":
                        if payload != {}:
                            self.respond(400, {"error": "interrupt requires an empty JSON object"})
                            return
                        self.respond(200, opencode_agents.interrupt(binding_id))
                    elif action == "permission":
                        self.respond(200, opencode_agents.reply_permission(binding_id, payload))
                    elif action == "form":
                        self.respond(200, opencode_agents.reply_form(binding_id, payload))
                    elif action == "unbind":
                        if payload != {}:
                            self.respond(400, {"error": "unbind requires an empty JSON object"})
                            return
                        self.respond(200, opencode_agents.unbind(binding_id))
                    else:
                        self.respond(404, {"error": "agent route not found"})
                except Exception as exc:
                    self._respond_agent_error(exc)
                return
            if route.startswith("/opencode-handoff"):
                if not self._authorize_handoff_request(mutation=True):
                    return
                payload = self._read_task_json()
                if payload is None:
                    return
                if opencode_handoff is None:
                    self.respond(404, {"error": "session handoff is disabled"})
                    return
                try:
                    if route == "/opencode-handoff/pair":
                        self.respond(200, opencode_handoff.pair(payload))
                    elif route == "/opencode-handoff/unpair":
                        self.respond(200, opencode_handoff.unpair(payload))
                    elif route == "/opencode-handoff/submit":
                        result, _created = opencode_handoff.submit(payload)
                        self.respond(201, result)
                    elif route.startswith("/opencode-handoff/") and (
                            route.endswith("/refresh") or route.endswith("/cancel")):
                        action = "refresh" if route.endswith("/refresh") else "cancel"
                        operation_id = route[len("/opencode-handoff/"):-(len(action) + 1)]
                        if not operation_id or "/" in operation_id or payload != {}:
                            self.respond(400, {"error": f"{action} requires an empty JSON object"})
                            return
                        if action == "refresh":
                            self.respond(200, opencode_handoff.refresh(operation_id))
                        else:
                            self.respond(200, opencode_handoff.cancel(operation_id))
                    else:
                        self.respond(404, {"error": "not found"})
                except Exception as exc:
                    self._respond_handoff_error(exc)
                return
            if route in ("/task-runs", "/task-runs/refresh") or (route.startswith("/task-runs/") and route.endswith("/cancel")):
                if not self._authorize_task_request(mutation=True):
                    return
                payload = self._read_task_json()
                if payload is None:
                    return
                if task_runner is None:
                    self.respond(404, {"error": "task runner is disabled"})
                    return
                try:
                    if route == "/task-runs/refresh":
                        if payload != {}:
                            self.respond(400, {"error": "refresh requires an empty JSON object"})
                            return
                        self.respond(200, task_runner.refresh_runtimes())
                    elif route == "/task-runs":
                        result, _created = task_runner.create(payload)
                        self.respond(201, result)
                    else:
                        prefix = "/task-runs/"
                        run_id = route[len(prefix):-len("/cancel")]
                        if not run_id or "/" in run_id or payload != {}:
                            self.respond(400, {"error": "cancel requires an empty JSON object"})
                            return
                        self.respond(200, task_runner.cancel(run_id))
                except Exception as exc:
                    try:
                        from .task_runner import TaskRunnerError
                    except ImportError:
                        from task_runner import TaskRunnerError
                    if isinstance(exc, TaskRunnerError):
                        self.respond(exc.status, {"error": exc.message})
                    else:
                        raise
                return
            if route != "/settings":
                self.respond(404, {"error": "not found"})
                return
            if not self._authorize_observer_request():
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.respond(415, {"error": "use application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 65536:
                    self.respond(413, {"error": "settings must be 1–65536 bytes"})
                    return
                self.connection.settimeout(5)
                payload = parse_settings_json(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("object required")
            except (ValueError, UnicodeError, RecursionError):
                self.respond(400, {"error": "expected a JSON object"})
                return
            condition = self.headers.get("If-Match")
            if condition is None:
                self.respond(428, {"error": "Read current settings and send their ETag in If-Match before saving."})
                return
            match = re.fullmatch(r'"(missing|[0-9a-f]{64})"', condition)
            if not match:
                self.respond(400, {"error": "If-Match must contain the quoted revision from the current settings response."})
                return
            try:
                preferences = _save_settings(payload, expected_revision=match[1])
                self.respond(200, preferences.settings, {"ETag": f'"{preferences.revision}"'})
            except SettingsConflict as exc:
                self.respond(409, {"error": str(exc), **exc.snapshot.public()})
            except SettingsUnavailable as exc:
                self.respond(503, {"error": str(exc), **exc.snapshot.public()})
            except ValueError:
                self.respond(400, {"error": "Settings exceed the supported format or size. Review the values before saving."})
            except (OSError, sqlite3.Error):
                self.respond(503, {"error": "Settings could not be saved. Check office storage access and try again."})

    class OfficeServer(ThreadingHTTPServer):
        daemon_threads = True
        # Browsers open many asset connections together on a cold load. The
        # small stdlib default drops queued connections on macOS.
        request_queue_size = 128

        def __init__(self, *args, **kwargs):
            # Initialize before binding: TCPServer may call server_close if
            # construction fails, before there is a maintenance worker.
            self._maintenance_stop = threading.Event()
            self._maintenance_thread = None
            self._next_maintenance_start = 0.0
            self._next_maintenance_warning = 0.0
            super().__init__(*args, **kwargs)

        def service_actions(self):
            # This callback runs on the accept loop, not a request thread.
            # Ingestion can wait on SQLite/filesystem/state locks; never make
            # loading bootstrap assets or stopping the listener wait for it.
            if self._maintenance_thread is not None or self._maintenance_stop.is_set():
                return
            now = time.monotonic()
            if now < self._next_maintenance_start:
                return
            self._next_maintenance_start = now + 1.0
            try:
                worker = threading.Thread(
                    target=self._maintain, name="agent-office-maintenance", daemon=True,
                )
                worker.start()
            except Exception as exc:
                # Resource exhaustion must not close the HTTP listener. Retry
                # on a later loop, without retaining an unstarted thread that
                # server_close would be unable to join.
                self._warn_maintenance_failure(exc)
                return
            self._maintenance_thread = worker

        def _warn_maintenance_failure(self, exc):
            now = time.monotonic()
            if now < self._next_maintenance_warning:
                return
            self._next_maintenance_warning = now + 60.0
            try:
                logger.warning(
                    "pixel-office background maintenance failed (%s); inputs retained",
                    type(exc).__name__,
                )
            except Exception:
                # A broken log destination must not stop ingestion or retries.
                pass

        def _maintain(self):
            # One worker consumes offline activity even with no browser open.
            # A slow pass never queues more workers or overlapping passes.
            while not self._maintenance_stop.is_set():
                try:
                    build_state()
                    self._next_maintenance_warning = 0.0
                except Exception as exc:
                    self._warn_maintenance_failure(exc)
                if self._maintenance_stop.wait(1.0):
                    break

        def server_close(self):
            self._maintenance_stop.set()
            super().server_close()
            worker = self._maintenance_thread
            if worker is not None and worker is not threading.current_thread():
                # Python cannot cancel a blocked filesystem read. The daemon
                # exits after its current pass; shutdown must remain bounded.
                worker.join(timeout=1.0)

    try:
        srv = OfficeServer(("127.0.0.1", _port), Handler)
        _port = srv.server_port
    except OSError as exc:
        if task_runner is not None:
            task_runner.shutdown()
        if opencode_agents is not None:
            opencode_agents.shutdown()
            raise TaskRunnerStartupError(
                f"managed OpenCode agents could not bind http://127.0.0.1:{_port}: {exc}. "
                "Stop the existing Agent Office or other listener, or choose an unused --port."
            ) from exc
        # Port already bound. Probe it: a healthy pixel-office answers /state
        # with JSON containing "agents". Anything else is a foreign squatter
        # (another app, or a ghost VS Code port-forward) — say so LOUDLY,
        # because to the user it looks like "office unreachable".
        verdict = _probe_port(_port)
        if verdict == "office":
            logger.info(
                "pixel-office: port %s already serving a healthy office "
                "(another Hermes process) — this process will feed events only",
                _port,
            )
        elif verdict.startswith("dead/"):
            # Port looked bound but nothing answers — a predecessor's socket
            # raced us. Leave this worker finished so a later event can start
            # a replacement once the listener is truly gone.
            logger.warning(
                "pixel-office: bind on %s failed (%s) and probe says %s — "
                "will retry on next event",
                _port, exc, verdict,
            )
            return
        else:
            logger.warning(
                "pixel-office: could NOT bind 127.0.0.1:%s (%s) and the "
                "current listener does not answer like a pixel-office "
                "(probe: %s). Another app or a stale VS Code port-forward is "
                "squatting the port. Fix: free the port, or set "
                "plugins.entries.pixel-office.port in config.yaml and update "
                "the extension's hermesPixelOffice.stateUrl to match.",
                _port, exc, verdict,
            )
        return
    logger.info("pixel-office serving at http://127.0.0.1:%s", _port)
    try:
        if on_ready is not None:
            on_ready(_port)
        srv.serve_forever()
    except Exception:
        logger.debug("pixel-office server exited", exc_info=True)
    finally:
        srv.server_close()
        if task_runner is not None:
            task_runner.shutdown()
        if opencode_agents is not None:
            opencode_agents.shutdown()


def _probe_port(port: int) -> str:
    """Classify whatever is listening on *port*: 'office', 'foreign', or 'dead'."""
    try:
        import urllib.request

        req = urllib.request.Request(f"http://127.0.0.1:{port}/state")
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
        return "office" if '"agents"' in body else "foreign"
    except Exception as exc:
        return f"dead/{type(exc).__name__}"


# ---------------------------------------------------------------------------
# Hook callbacks — all **kwargs so core payload changes never break us
# ---------------------------------------------------------------------------

# Tool name → activity shown in the office (drives the character animation).
_ACTIVITY = {
    "write_file": "typing", "patch": "typing", "skill_manage": "typing",
    "read_file": "reading", "search_files": "reading", "skill_view": "reading",
    "web_search": "browsing", "web_extract": "browsing",
    "browser_navigate": "browsing", "browser_click": "browsing",
    "browser_snapshot": "browsing", "browser_vision": "browsing",
    "terminal": "running", "execute_code": "running", "process": "running",
    "delegate_task": "delegating",
}


def _activity_for(tool: str) -> str:
    return _ACTIVITY.get(str(tool or ""), "working")


def _on_session_start(**kw: Any) -> None:
    _publish({
        "event": "session_start",
        "session_id": kw.get("session_id"),
        "platform": kw.get("platform"),
    })


def _on_session_end(**kw: Any) -> None:
    _publish({"event": "session_end", "session_id": kw.get("session_id")})


def _pre_tool_call(**kw: Any) -> None:
    sid = kw.get("session_id") or ""
    _current_session_id.set(str(sid))
    _current_call_id.set(str(kw.get("tool_call_id") or kw.get("call_id") or ""))
    args = kw.get("args") or {}
    preview = ""
    if isinstance(args, dict):
        for k in ("command", "path", "query", "url", "goal", "pattern", "prompt"):
            if args.get(k):
                preview = str(args[k])
                break
    tool = kw.get("tool_name")
    _publish({
        "event": "tool_start",
        "session_id": sid,
        "call_id": _current_call_id.get(),
        "tool_name": tool,
        "activity": _activity_for(tool),
        "preview": _short(preview),
    })
    return None  # observer — never blocks


def _post_tool_call(**kw: Any) -> None:
    _publish({
        "event": "tool_end",
        "session_id": kw.get("session_id"),
        "call_id": kw.get("tool_call_id") or kw.get("call_id") or "",
        "tool_name": kw.get("tool_name"),
        "status": kw.get("status") or "ok",
        "error_message": kw.get("error_message"),
        "duration_ms": kw.get("duration_ms"),
    })
    completed_call = str(kw.get("tool_call_id") or kw.get("call_id") or "")
    if (str(kw.get("session_id") or "") == _current_session_id.get()
        and (not completed_call or completed_call == _current_call_id.get())):
        _current_session_id.set("")
        _current_call_id.set("")


def _subagent_start(**kw: Any) -> None:
    _publish({
        "event": "subagent_start",
        "parent_session_id": kw.get("parent_session_id"),
        "child_session_id": kw.get("child_session_id"),
        "child_role": kw.get("child_role"),
        "child_goal": kw.get("child_goal"),
    })


def _subagent_stop(**kw: Any) -> None:
    _publish({
        "event": "subagent_stop",
        "child_session_id": kw.get("child_session_id"),
    })


def _approval_identity(kw: Dict[str, Any]) -> tuple:
    sid = kw.get("session_id") or _current_session_id.get()
    call = kw.get("tool_call_id") or kw.get("call_id") or ""
    if not call and str(sid) == _current_session_id.get():
        call = _current_call_id.get()
    return sid, call


def _pre_approval_request(**kw: Any) -> None:
    sid, call = _approval_identity(kw)
    _publish({
        "event": "approval_request",
        "session_id": sid,
        "call_id": call,
        "request_id": kw.get("request_id") or "",
        "command": kw.get("command") or kw.get("description"),
        "surface": kw.get("surface"),
    })


def _post_approval_response(**kw: Any) -> None:
    sid, call = _approval_identity(kw)
    _publish({
        "event": "approval_response",
        "session_id": sid,
        "call_id": call,
        "request_id": kw.get("request_id") or "",
        "choice": kw.get("choice"),
    })


def _pre_llm_call(**kw: Any) -> None:
    _publish({"event": "session_busy", "session_id": kw.get("session_id"),
              "platform": kw.get("platform") or "hermes"})


def _post_llm_call(**kw: Any) -> None:
    _publish({"event": "session_idle", "session_id": kw.get("session_id"),
              "platform": kw.get("platform") or "hermes"})


def _post_api_request(**kw: Any) -> None:
    """Normalize one stable Hermes API attempt; reasoning is within output."""
    source = kw.get("usage")
    sid, request_id = kw.get("session_id"), kw.get("api_request_id")
    if not isinstance(source, dict) or not isinstance(sid, str) or not sid or not isinstance(request_id, str) or not request_id:
        return
    event = {"event": "usage", "platform": "hermes", "session_id": sid,
             "usage_id": request_id, "usage_scope": "api_request",
             "model": kw.get("response_model") or kw.get("model"), "provider": kw.get("provider")}
    fields = {"prompt_tokens": "input_tokens", "output_tokens": "output_tokens",
              "cache_read_tokens": "cached_input_tokens", "cache_write_tokens": "cache_write_tokens",
              "reasoning_tokens": "reasoning_output_tokens", "total_tokens": "total_tokens"}
    for source_key, target_key in fields.items():
        value = source.get(source_key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= (1 << 53) - 1:
            event[target_key] = value
    _publish(event)


def register(ctx: Any) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("subagent_start", _subagent_start)
    ctx.register_hook("subagent_stop", _subagent_stop)
    ctx.register_hook("pre_approval_request", _pre_approval_request)
    ctx.register_hook("post_approval_response", _post_approval_response)
    # Old Hermes versions reject or warn about unknown hook names. Richer
    # telemetry must never prevent the established tool/session hooks loading.
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except (ImportError, AttributeError):
        VALID_HOOKS = set()
    for name, callback in (("pre_llm_call", _pre_llm_call),
                           ("post_llm_call", _post_llm_call),
                           ("post_api_request", _post_api_request)):
        if name in VALID_HOOKS:
            ctx.register_hook(name, callback)
    logger.info(
        "pixel-office registered — office at http://127.0.0.1:%s once events flow",
        _resolve_port(),
    )
